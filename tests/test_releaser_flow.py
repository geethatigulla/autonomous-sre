"""Tests for the Releaser agent.

The Releaser orchestrates the real-world side effects that happen
after a fix has been verified — branch creation, patch commit, PR
open, and Slack notification. These tests pin down four behaviours:

1. With a working GitHub fake, the Releaser creates the branch,
   commits the patched blob, opens a PR, and persists ``pr_url`` etc.
   on the incident.
2. When the Slack webhook returns non-2xx the release still succeeds;
   ``slack_posted`` is False on the incident.
3. When the unified diff doesn't apply against the file content the
   GitHub fake serves, the agent raises (and the state-machine wiring,
   exercised in the corresponding test, marks the incident
   ``release_failed`` with ``PatchApplyError`` in the error_message).
4. When the Slack webhook URL is empty, no HTTP request is attempted
   and ``slack_posted`` is False, but the release still succeeds.

All tests use lightweight in-process fakes — no real network, no real
PyGithub. The Releaser only depends on a small set of async methods on
the GitHubTool surface, so the fakes can mimic them faithfully.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path
from typing import Any, Optional

import pytest


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Shared seed assets
# ---------------------------------------------------------------------------


_BUGGY_APP_PY = textwrap.dedent(
    '''
    """Tiny seed app for releaser tests."""

    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
    from typing import Optional

    app = FastAPI()


    class ShortenRequest(BaseModel):
        url: Optional[str] = None


    @app.post("/shorten")
    async def shorten(payload: ShortenRequest) -> dict:
        url = payload.url
        # BUG: no null guard before .strip()
        normalized = url.strip().lower()
        return {"short": normalized[:6], "original": normalized}
    '''
).lstrip()


_CLEAN_PATCH = textwrap.dedent(
    """\
    --- a/app.py
    +++ b/app.py
    @@ -14,6 +14,8 @@ class ShortenRequest(BaseModel):
     @app.post("/shorten")
     async def shorten(payload: ShortenRequest) -> dict:
         url = payload.url
    +    if url is None:
    +        raise HTTPException(status_code=422, detail="url is required")
         # BUG: no null guard before .strip()
         normalized = url.strip().lower()
         return {"short": normalized[:6], "original": normalized}
    """
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = type(
            "Msg", (), {"content": content, "tool_calls": None}
        )()
        self.finish_reason = "stop"


class _FakeUsage:
    total_tokens = 50


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, owner: "FakeOpenAI") -> None:
        self._owner = owner

    async def create(self, **kwargs):
        return _FakeResponse(self._owner.next_payload())


class _FakeChat:
    def __init__(self, owner: "FakeOpenAI") -> None:
        self.completions = _FakeCompletions(owner)


class FakeOpenAI:
    """Returns scripted JSON payloads on each ``chat.completions.create``."""

    def __init__(self, payloads: list[str]) -> None:
        self._payloads = list(payloads)
        self.call_count = 0
        self.chat = _FakeChat(self)

    def next_payload(self) -> str:
        if not self._payloads:
            return "{}"
        idx = min(self.call_count, len(self._payloads) - 1)
        self.call_count += 1
        return self._payloads[idx]


class FakeGitHub:
    """Minimal in-process stand-in for the bits of GitHubTool the Releaser uses.

    Records every mutating call (``ensure_branch``, ``update_file_on_branch``,
    ``create_pull``) so tests can assert against the sequence.
    """

    def __init__(
        self,
        *,
        file_content: str = _BUGGY_APP_PY,
        existing_pr: Optional[dict] = None,
        file_sha: str = "deadbeef",
        branch_exists: bool = False,
    ) -> None:
        self.disabled = False
        self.file_content = file_content
        self.file_sha = file_sha
        self.existing_pr = existing_pr
        self.branch_exists = branch_exists

        # Call recordings.
        self.ensure_branch_calls: list[dict] = []
        self.update_file_calls: list[dict] = []
        self.create_pull_calls: list[dict] = []
        self.find_pr_calls: list[dict] = []

        # Counter for issuing PR numbers.
        self._next_pr_number = 42

    async def get_file_with_sha(
        self, path: str, ref: str = "main", span: Any = None
    ) -> dict:
        return {"path": path, "sha": self.file_sha, "content": self.file_content}

    async def ensure_branch(
        self, branch: str, base: str = "main", span: Any = None
    ) -> dict:
        self.ensure_branch_calls.append({"branch": branch, "base": base})
        created = not self.branch_exists
        self.branch_exists = True
        return {"created": created, "branch": branch, "base_sha": "basesha123"}

    async def update_file_on_branch(
        self,
        path: str,
        message: str,
        content: str,
        sha: str,
        branch: str,
        span: Any = None,
    ) -> dict:
        self.update_file_calls.append(
            {
                "path": path,
                "message": message,
                "content": content,
                "sha": sha,
                "branch": branch,
            }
        )
        return {"commit_sha": "newcommit123"}

    async def find_pr_by_head(
        self, branch: str, span: Any = None
    ) -> Optional[dict]:
        self.find_pr_calls.append({"branch": branch})
        return self.existing_pr

    async def create_pull(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        span: Any = None,
    ) -> dict:
        self.create_pull_calls.append(
            {"title": title, "body": body, "head": head, "base": base}
        )
        number = self._next_pr_number
        self._next_pr_number += 1
        return {
            "number": number,
            "url": f"https://github.com/fake/repo/pull/{number}",
            "state": "open",
            "head": head,
            "base": base,
        }


class FakeGitHubBadDiff(FakeGitHub):
    """GitHub fake that serves file content the diff cannot apply to."""

    def __init__(self) -> None:
        super().__init__(
            file_content="totally different file content\nwith nothing in common\n",
        )


# ---------------------------------------------------------------------------
# Isolated env fixture (DB + local-only tracing)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    db_path = tmp_path / "sre.db"
    trace_path = tmp_path / "local_traces.jsonl"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("TRACE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_TRACE_PATH", str(trace_path))
    monkeypatch.delenv("OMIUM_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("GITHUB_REPO", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")

    from orchestrator import config as config_module
    from orchestrator import db as db_module
    from orchestrator import tracing as tracing_module

    config_module.get_settings.cache_clear()
    db_module.reset_engine()
    tracing_module.reset_tracer()
    db_module.init_db()

    yield trace_path

    config_module.get_settings.cache_clear()
    db_module.reset_engine()
    tracing_module.reset_tracer()


def _make_span():
    """Construct a workflow + root span for direct agent invocation."""
    from orchestrator.tracing import get_tracer

    tracer = get_tracer()
    wf = tracer.start_workflow("test_workflow", metadata={})
    span_cm = wf.span("root")
    span = span_cm.__enter__()
    return wf, span_cm, span


def _make_incident(session, **overrides):
    from orchestrator.models import Incident

    base = dict(
        source_service="victim_app",
        endpoint="POST /shorten",
        exception_class="AttributeError",
        exception_message="'NoneType' object has no attribute 'strip'",
        traceback="Traceback...",
        request_body='{"url": null}',
        raw_payload="{}",
        status="verified",
        classification="null_deref",
        severity="P1",
        suspect_file="app.py",
        suspect_line=17,
        root_cause="missing None guard on payload.url before .strip()",
        proposed_patch=_CLEAN_PATCH,
        fix_explanation="Add a 422 guard so missing url is rejected rather than crashing.",
        fix_risk_level="low",
        fix_confidence=0.92,
        tests_passed=True,
        verification_result=json.dumps(
            {
                "verdict": "verified",
                "tests_passed": True,
                "duration_seconds": 3.5,
            }
        ),
        workflow_id="wf_test123",
    )
    base.update(overrides)
    incident = Incident(**base)
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident


def _compose_payload() -> str:
    """A canonical Releaser LLM payload — what we'd expect from gpt-4o-mini."""
    return json.dumps(
        {
            "pr_title": "fix: add 422 guard for null url",
            "pr_body": (
                "## Incident\n\nNull deref on POST /shorten.\n\n"
                "## Root Cause\n\nMissing None guard.\n\n"
                "## Fix\n\nRaise HTTPException(422) when url is None.\n\n"
                "## Verification\n\nTests passed in sandbox.\n\n"
                "## Risk Assessment\n\nLow.\n\n"
                "---\n🤖 *Autonomously generated by the SRE Incident Response System*\n"
                "Incident ID: `{incident_id}` | Workflow: `{workflow_id}` | "
                "Risk: `{risk}` | Confidence: `{confidence}`"
            ),
            "slack_message": (
                ":rotating_light: *Autonomous SRE Fix Deployed*\n"
                "*Incident:* `inc_test` | *Class:* `null_deref` | *Risk:* `low`\n"
                "*Summary:* Add 422 guard for null url\n"
                "*PR:* {{PR_URL}}"
            ),
            "commit_message": "fix: add 422 guard for null url",
        }
    )


# ---------------------------------------------------------------------------
# 1. Happy path — Releaser creates branch + commits + opens PR
# ---------------------------------------------------------------------------


def test_releaser_creates_pr_branch_and_applies_patch(isolated_env):
    from sqlmodel import Session

    from orchestrator.agents.releaser import ReleaserAgent
    from orchestrator.db import get_engine

    fake_openai = FakeOpenAI(payloads=[_compose_payload()])
    fake_github = FakeGitHub()

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(session)

        wf, span_cm, span = _make_span()
        try:
            agent = ReleaserAgent(
                fake_openai,
                span.tracer,
                github_tool=fake_github,
                slack_webhook_url=None,
            )
            result = asyncio.run(
                agent.run(incident=incident, span=span, session=session)
            )
        finally:
            span_cm.__exit__(None, None, None)
            wf.finish(status="success")

        session.refresh(incident)

    # Branch creation happened on main.
    assert len(fake_github.ensure_branch_calls) == 1
    ensure_call = fake_github.ensure_branch_calls[0]
    assert ensure_call["branch"].startswith("autosre/inc-")
    assert ensure_call["base"] == "main"

    # File update went to the right branch and contained the inserted guard.
    assert len(fake_github.update_file_calls) == 1
    upd = fake_github.update_file_calls[0]
    assert upd["branch"] == ensure_call["branch"]
    assert upd["path"] == "app.py"
    assert "if url is None" in upd["content"]
    assert 'raise HTTPException(status_code=422' in upd["content"]
    # Make sure we didn't accidentally upload the diff itself.
    assert "--- a/" not in upd["content"]

    # We looked for an existing PR before opening one.
    assert len(fake_github.find_pr_calls) == 1

    # PR creation happened with the right base/head.
    assert len(fake_github.create_pull_calls) == 1
    pr_call = fake_github.create_pull_calls[0]
    assert pr_call["head"] == ensure_call["branch"]
    assert pr_call["base"] == "main"

    # Result + incident persistence.
    assert result["pr_url"].startswith("https://github.com/")
    assert result["pr_number"] == 42
    assert result["pr_branch"].startswith("autosre/inc-")
    assert incident.status == "verified"  # Releaser doesn't flip status
    assert incident.pr_url and incident.pr_url.startswith("https://github.com/")
    assert incident.pr_number == 42
    assert incident.pr_branch and incident.pr_branch.startswith("autosre/inc-")
    # Slack webhook disabled — slack_posted should be False.
    assert incident.slack_posted is False


# ---------------------------------------------------------------------------
# 2. Slack failure is graceful
# ---------------------------------------------------------------------------


def test_releaser_handles_slack_failure_gracefully(isolated_env, monkeypatch):
    from sqlmodel import Session

    from orchestrator.agents.releaser import ReleaserAgent
    from orchestrator.db import get_engine

    fake_openai = FakeOpenAI(payloads=[_compose_payload()])
    fake_github = FakeGitHub()

    # Patch httpx.AsyncClient so we never touch the network and return 500.
    import orchestrator.agents.releaser as releaser_mod

    class _FakeResp:
        def __init__(self, code: int, text: str = "boom"):
            self.status_code = code
            self.text = text

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _FakeResp(500)

    monkeypatch.setattr(
        releaser_mod.httpx, "AsyncClient", _FakeAsyncClient
    )

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(session)

        wf, span_cm, span = _make_span()
        try:
            agent = ReleaserAgent(
                fake_openai,
                span.tracer,
                github_tool=fake_github,
                slack_webhook_url="https://hooks.slack.com/services/X/Y/Z",
            )
            result = asyncio.run(
                agent.run(incident=incident, span=span, session=session)
            )
        finally:
            span_cm.__exit__(None, None, None)
            wf.finish(status="success")

        session.refresh(incident)

    # Release still succeeds: PR was opened, branch was created.
    assert result["pr_url"].startswith("https://github.com/")
    assert result["slack_posted"] is False
    assert incident.pr_url and incident.pr_url.startswith("https://github.com/")
    assert incident.slack_posted is False


# ---------------------------------------------------------------------------
# 3. Patch application failure surfaces PatchApplyError
# ---------------------------------------------------------------------------


def test_releaser_handles_patch_apply_failure(isolated_env):
    from sqlmodel import Session

    from orchestrator.agents.releaser import ReleaserAgent
    from orchestrator.db import get_engine
    from orchestrator.tools.patch_tool import PatchApplyError

    fake_openai = FakeOpenAI(payloads=[_compose_payload()])
    fake_github = FakeGitHubBadDiff()

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(session)

        wf, span_cm, span = _make_span()
        try:
            agent = ReleaserAgent(
                fake_openai,
                span.tracer,
                github_tool=fake_github,
                slack_webhook_url=None,
            )
            with pytest.raises(PatchApplyError) as exc_info:
                asyncio.run(
                    agent.run(incident=incident, span=span, session=session)
                )
        finally:
            span_cm.__exit__(None, None, None)
            wf.finish(status="error")

        session.refresh(incident)

    # Hunk context mismatch should be in the error message — the state
    # machine logs this verbatim onto AgentRun.error_message.
    msg = str(exc_info.value)
    assert "hunk" in msg.lower() or "context" in msg.lower() or "removal" in msg.lower()

    # We failed BEFORE opening a PR — the GitHub fake should never have
    # been asked to commit.
    assert len(fake_github.update_file_calls) == 0
    assert len(fake_github.create_pull_calls) == 0
    # Incident should NOT have a pr_url since the release failed before
    # opening one.
    assert incident.pr_url is None or incident.pr_url == ""


# ---------------------------------------------------------------------------
# 4. Disabled Slack webhook — no HTTP attempt
# ---------------------------------------------------------------------------


def test_releaser_disabled_slack_webhook(isolated_env, monkeypatch):
    from sqlmodel import Session

    from orchestrator.agents.releaser import ReleaserAgent
    from orchestrator.db import get_engine

    fake_openai = FakeOpenAI(payloads=[_compose_payload()])
    fake_github = FakeGitHub()

    # Track whether httpx.AsyncClient was instantiated at all.
    import orchestrator.agents.releaser as releaser_mod

    http_calls: list[str] = []

    class _BoomClient:
        def __init__(self, *args, **kwargs):
            http_calls.append("init")
            raise AssertionError("httpx must not be instantiated when slack disabled")

    monkeypatch.setattr(
        releaser_mod.httpx, "AsyncClient", _BoomClient
    )

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(session)

        wf, span_cm, span = _make_span()
        try:
            agent = ReleaserAgent(
                fake_openai,
                span.tracer,
                github_tool=fake_github,
                slack_webhook_url=None,
            )
            result = asyncio.run(
                agent.run(incident=incident, span=span, session=session)
            )
        finally:
            span_cm.__exit__(None, None, None)
            wf.finish(status="success")

        session.refresh(incident)

    # httpx was never touched.
    assert http_calls == []

    # Release succeeded — PR opened, branch created, status still
    # "verified" because Releaser doesn't change status (state machine
    # does after a successful run).
    assert result["pr_url"].startswith("https://github.com/")
    assert result["slack_posted"] is False
    assert incident.pr_url and incident.pr_url.startswith("https://github.com/")
    assert incident.slack_posted is False
