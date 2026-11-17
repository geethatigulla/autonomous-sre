"""Tests for the Fix Author + Verifier flow.

Covers four kinds of behaviour:

1. The Fix Author emits a syntactically-valid unified diff and persists
   it on the incident (with explanation + confidence).
2. The Verifier applies a clean diff in a tempdir sandbox and pytest
   passes — verdict "verified".
3. The Verifier applies a diff that compiles but the resulting tests
   fail — verdict "tests_failed".
4. The Verifier rejects a malformed diff via ``git apply --check`` —
   verdict "patch_invalid".

And one end-to-end test:

5. With Triager + Investigator + Fix Author + Verifier all wired up
   through the state machine and fakes for OpenAI / GitHub, the
   incident transitions classified → investigated → fix_drafted →
   verified → pending_release and all four AgentRun rows are in
   ``status="success"``.

The tests deliberately don't depend on the real ``victim_app`` source
tree: each Verifier test seeds its own tmp_path-based mini repo so the
sandbox has something to copy + run pytest against. This keeps them
hermetic and quick.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Shared seed assets
# ---------------------------------------------------------------------------


_BUGGY_APP_PY = textwrap.dedent(
    '''
    """Tiny seed app for sandbox tests.

    Has a deliberate null-deref bug: /shorten doesn't None-check `url`
    before calling .strip(). The seeded pytest asserts the *correct*
    behaviour, so the test fails until the bug is fixed.
    """

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


    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "healthy"}
    '''
).lstrip()


_SEED_TEST_APP = textwrap.dedent(
    '''
    import os
    import sys
    from pathlib import Path

    from fastapi.testclient import TestClient

    HERE = Path(__file__).resolve().parent.parent
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))

    if "app" in sys.modules:
        del sys.modules["app"]
    import app as victim_app  # noqa: E402

    client = TestClient(victim_app.app, raise_server_exceptions=False)


    def test_healthz():
        resp = client.get("/healthz")
        assert resp.status_code == 200


    def test_shorten_null_returns_422():
        # This test fails against the buggy app (the handler raises
        # AttributeError, which Starlette converts to 500) and passes
        # against the patched version that raises HTTPException(422).
        resp = client.post("/shorten", json={"url": None})
        assert resp.status_code == 422, resp.text
    '''
).lstrip()


# A clean patch that fixes the null_deref bug above. The diff is keyed
# to the exact content of _BUGGY_APP_PY (the leading """...""" docstring
# adds 6 lines of context, then imports + app=FastAPI() + class, etc.).
# Rather than hand-craft the diff and risk drifting context lines, we
# build it from the buggy + fixed bodies at fixture time. See
# `_make_clean_patch` below.


def _make_clean_patch() -> str:
    """Construct a unified diff that fixes the null deref bug."""
    return textwrap.dedent(
        """\
        --- a/app.py
        +++ b/app.py
        @@ -17,6 +17,8 @@ class ShortenRequest(BaseModel):
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


def _make_failing_patch() -> str:
    """A patch that applies cleanly but DOES NOT fix the bug.

    Adds a no-op comment so the tree changes but `test_shorten_null`
    still fails — the perfect probe for the "tests_failed" verdict.
    """
    return textwrap.dedent(
        """\
        --- a/app.py
        +++ b/app.py
        @@ -17,6 +17,7 @@ class ShortenRequest(BaseModel):
         @app.post("/shorten")
         async def shorten(payload: ShortenRequest) -> dict:
             url = payload.url
        +    # noop: deliberately does NOT fix the bug
             # BUG: no null guard before .strip()
             normalized = url.strip().lower()
             return {"short": normalized[:6], "original": normalized}
        """
    )


def _seed_sandbox_source(tmp_path: Path) -> Path:
    """Create a ``victim_app/`` skeleton inside ``tmp_path`` and return its path."""
    src = tmp_path / "victim_app"
    src.mkdir()
    (src / "app.py").write_text(_BUGGY_APP_PY, encoding="utf-8")
    (src / "__init__.py").write_text("", encoding="utf-8")
    tests_dir = src / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_app.py").write_text(_SEED_TEST_APP, encoding="utf-8")
    return src


# ---------------------------------------------------------------------------
# Lightweight fakes (reused across tests)
# ---------------------------------------------------------------------------


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = type("Msg", (), {"content": content, "tool_calls": None})()
        self.finish_reason = "stop"


class _FakeUsage:
    total_tokens = 99


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeChatCompletions:
    def __init__(self, owner: "FakeOpenAIForFixAuthor") -> None:
        self._owner = owner

    async def create(self, **kwargs):
        return _FakeResponse(self._owner.next_payload())


class _FakeChat:
    def __init__(self, owner: "FakeOpenAIForFixAuthor") -> None:
        self.completions = _FakeChatCompletions(owner)


class FakeOpenAIForFixAuthor:
    """Returns scripted JSON payloads on each ``chat.completions.create`` call."""

    def __init__(self, payloads: list[str]) -> None:
        self._payloads = list(payloads)
        self.call_count = 0
        self.chat = _FakeChat(self)

    def next_payload(self) -> str:
        if not self._payloads:
            return "{}"
        if self.call_count >= len(self._payloads):
            return self._payloads[-1]
        payload = self._payloads[self.call_count]
        self.call_count += 1
        return payload


class FakeGitHubForFixAuthor:
    """Tiny GitHubTool stand-in that always returns a fixed file body."""

    def __init__(self, file_body: str) -> None:
        self._body = file_body
        self.disabled = False
        self.calls: list[tuple[str, str]] = []

    async def read_file(self, path: str, ref: str = "main", span: Any = None) -> str:
        self.calls.append((path, ref))
        return self._body

    async def read_file_at_lines(
        self,
        path: str,
        start: int,
        end: int,
        ref: str = "main",
        span: Any = None,
    ) -> str:
        body = self._body
        lines = body.splitlines()
        return "\n".join(lines[max(0, start - 1) : end])

    async def list_recent_commits(
        self,
        path: str | None = None,
        limit: int = 10,
        span: Any = None,
    ) -> list[dict]:
        return []


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


def _make_incident(session, **overrides) -> Any:
    """Insert + return a fresh Incident row with sensible defaults."""
    from orchestrator.models import Incident

    base = dict(
        source_service="victim_app",
        endpoint="POST /shorten",
        exception_class="AttributeError",
        exception_message="'NoneType' object has no attribute 'strip'",
        traceback=(
            "Traceback (most recent call last):\n"
            '  File "app.py", line 20, in shorten\n'
            "    normalized = url.strip().lower()\n"
            "AttributeError: 'NoneType' object has no attribute 'strip'"
        ),
        request_body='{"url": null}',
        raw_payload='{"endpoint": "POST /shorten"}',
        status="investigated",
        classification="null_deref",
        severity="P1",
        suspect_file="app.py",
        suspect_line=20,
        root_cause="missing None guard on payload.url before .strip()",
        investigation_result=json.dumps(
            {
                "suspect_file": "app.py",
                "suspect_line": 20,
                "root_cause": "missing None guard on payload.url before .strip()",
                "hypotheses": [
                    {
                        "description": "url is None",
                        "evidence": "traceback shows NoneType",
                        "fix_strategy": "raise HTTPException(422) when url is None",
                        "confidence": 0.9,
                    }
                ],
                "investigation_log": ["read app.py", "confirmed missing guard"],
            }
        ),
    )
    base.update(overrides)
    incident = Incident(**base)
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident


# ---------------------------------------------------------------------------
# Span / tracer helpers
# ---------------------------------------------------------------------------


def _make_span():
    """Build a workflow + root span we can hand into agents in unit tests.

    Tests that call agents directly (not through the state machine) need
    a real :class:`SpanContext` so the agent's ``span.event(...)`` /
    ``span.child_span(...)`` calls work. We open a workflow + one span
    and yield it; the caller decides when to close.
    """
    from orchestrator.tracing import get_tracer

    tracer = get_tracer()
    wf = tracer.start_workflow("test_workflow", metadata={})
    span_cm = wf.span("root")
    span = span_cm.__enter__()
    return wf, span_cm, span


# ---------------------------------------------------------------------------
# Fix Author helpers
# ---------------------------------------------------------------------------


def test_normalize_diff_paths_strips_victim_app_prefix():
    from orchestrator.agents.fix_author import _normalize_diff_paths

    raw = (
        "--- a/victim_app/app.py\n"
        "+++ b/victim_app/app.py\n"
        "@@ -70,6 +70,8 @@\n"
    )
    out = _normalize_diff_paths(raw)
    assert out.startswith("--- a/app.py\n")
    assert "+++ b/app.py\n" in out


def test_build_null_deref_patch_targets_strip_line():
    from orchestrator.agents.fix_author import _build_null_deref_patch

    app_py = (ROOT / "victim_app" / "app.py").read_text(encoding="utf-8")
    diff = _build_null_deref_patch(
        app_py, rel_path="app.py", insert_before_line=76
    )
    assert "if url is None" in diff
    assert "--- a/app.py" in diff
    assert "HTTPException(status_code=422" in diff


# ---------------------------------------------------------------------------
# Test 1: Fix Author produces a valid diff
# ---------------------------------------------------------------------------


def test_fix_author_produces_valid_diff(isolated_env):
    from orchestrator.agents.fix_author import FixAuthorAgent
    from orchestrator.db import get_engine
    from sqlmodel import Session

    payload = {
        "file_path": "victim_app/app.py",
        "diff": _make_clean_patch().replace(
            "--- a/app.py", "--- a/victim_app/app.py"
        ).replace("+++ b/app.py", "+++ b/victim_app/app.py"),
        "explanation": "Add a None guard so missing url returns 422 instead of crashing.",
        "test_should_pass": "test_shorten_null_returns_422",
        "risk_level": "low",
        "confidence": 0.92,
    }

    fake_openai = FakeOpenAIForFixAuthor(payloads=[json.dumps(payload)])
    fake_github = FakeGitHubForFixAuthor(file_body=_BUGGY_APP_PY)

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(session)

        wf, span_cm, span = _make_span()
        try:
            agent = FixAuthorAgent(
                fake_openai, span.tracer, github_tool=fake_github
            )
            result = asyncio.run(
                agent.run(incident=incident, span=span, session=session)
            )
        finally:
            span_cm.__exit__(None, None, None)
            wf.finish(status="success")

        # Re-read to confirm persistence.
        session.refresh(incident)

    assert result["file_path"] == "app.py"
    assert "@@" in result["diff"]
    assert result["confidence"] > 0.5
    assert result["risk_level"] in {"low", "medium", "high"}

    assert incident.proposed_patch, "proposed_patch should be persisted"
    diff_on_row = incident.proposed_patch
    assert "---" in diff_on_row and "+++" in diff_on_row and "@@" in diff_on_row
    assert incident.fix_explanation
    assert incident.fix_confidence is not None and incident.fix_confidence > 0.5
    assert fake_github.calls, "FixAuthor should re-read the suspect file via GitHubTool"


# ---------------------------------------------------------------------------
# Test 2: Verifier applies clean patch and passes
# ---------------------------------------------------------------------------


def test_verifier_applies_clean_patch_and_passes(isolated_env, tmp_path):
    from orchestrator.agents.verifier import VerifierAgent
    from orchestrator.db import get_engine
    from sqlmodel import Session

    src = _seed_sandbox_source(tmp_path)

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(
            session,
            suspect_file="app.py",
            status="fix_drafted",
            proposed_patch=_make_clean_patch(),
            fix_explanation="add 422 guard",
            fix_risk_level="low",
            fix_confidence=0.9,
        )

        wf, span_cm, span = _make_span()
        try:
            agent = VerifierAgent(
                openai_client=None,
                tracer=span.tracer,
                source_dir=str(src),
            )
            result = asyncio.run(
                agent.run(incident=incident, span=span, session=session)
            )
        finally:
            span_cm.__exit__(None, None, None)
            wf.finish(status="success")

        session.refresh(incident)

    assert result["verdict"] == "verified", result
    assert result["tests_passed"] is True, result
    assert result["applied"] is True
    assert incident.tests_passed is True
    assert incident.verification_result
    parsed = json.loads(incident.verification_result)
    assert parsed["verdict"] == "verified"


# ---------------------------------------------------------------------------
# Test 3: Verifier rejects a failing patch
# ---------------------------------------------------------------------------


def test_verifier_rejects_failing_patch(isolated_env, tmp_path):
    from orchestrator.agents.verifier import VerifierAgent
    from orchestrator.db import get_engine
    from sqlmodel import Session

    src = _seed_sandbox_source(tmp_path)

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(
            session,
            status="fix_drafted",
            proposed_patch=_make_failing_patch(),
            fix_risk_level="medium",
            fix_confidence=0.4,
        )

        wf, span_cm, span = _make_span()
        try:
            agent = VerifierAgent(
                openai_client=None,
                tracer=span.tracer,
                source_dir=str(src),
            )
            result = asyncio.run(
                agent.run(incident=incident, span=span, session=session)
            )
        finally:
            span_cm.__exit__(None, None, None)
            wf.finish(status="success")

        session.refresh(incident)

    assert result["verdict"] == "tests_failed", result
    assert result["applied"] is True
    assert result["tests_passed"] is False
    assert incident.tests_passed is False
    assert incident.verification_result


# ---------------------------------------------------------------------------
# Test 4: Verifier rejects an invalid patch
# ---------------------------------------------------------------------------


def test_verifier_rejects_invalid_patch(isolated_env, tmp_path):
    from orchestrator.agents.verifier import VerifierAgent
    from orchestrator.db import get_engine
    from sqlmodel import Session

    src = _seed_sandbox_source(tmp_path)
    malformed = "this is not a unified diff at all"

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(
            session,
            status="fix_drafted",
            proposed_patch=malformed,
            fix_risk_level="high",
            fix_confidence=0.1,
        )

        wf, span_cm, span = _make_span()
        try:
            agent = VerifierAgent(
                openai_client=None,
                tracer=span.tracer,
                source_dir=str(src),
            )
            result = asyncio.run(
                agent.run(incident=incident, span=span, session=session)
            )
        finally:
            span_cm.__exit__(None, None, None)
            wf.finish(status="success")

        session.refresh(incident)

    assert result["verdict"] == "patch_invalid", result
    assert result["applied"] is False
    assert result["tests_ran"] is False
    assert result["tests_passed"] is False
    assert incident.tests_passed is False


# ---------------------------------------------------------------------------
# Test 5: Full state-machine null_deref path
# ---------------------------------------------------------------------------


# Scripted OpenAI fake that returns different responses based on the
# requesting agent (Triager / Investigator / Fix Author). We dispatch on
# model + presence of `tools` because:
#   - Triager:  gpt-4o-mini, response_format=json_object, no tools
#   - Investigator: gpt-4o,    tool-calling mode (kwargs has "tools")
#   - Fix Author:   gpt-4o,    response_format=json_object, no tools
class _ScriptedToolCallFn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ScriptedToolCall:
    def __init__(self, name: str, arguments: dict) -> None:
        self.id = f"call_{name}"
        self.type = "function"
        self.function = _ScriptedToolCallFn(name, json.dumps(arguments))


class _ScriptedMessage:
    def __init__(self, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or None


class _ScriptedChoice:
    def __init__(self, msg: _ScriptedMessage):
        self.message = msg
        self.finish_reason = "stop"


class _ScriptedResp:
    def __init__(self, msg: _ScriptedMessage):
        self.choices = [_ScriptedChoice(msg)]
        self.usage = _FakeUsage()


class _ScriptedClientChat:
    def __init__(self, owner: "ScriptedClient"):
        self._owner = owner

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        return self._owner._dispatch(kwargs)


class ScriptedClient:
    """Scripted OpenAI client for the full-pipeline test.

    Dispatches by (model, has-tools) so all four LLM-using agents
    (Triager, Investigator, Fix Author, Releaser) see appropriate
    scripted responses. Triager and Releaser both use ``gpt-4o-mini``
    in JSON mode, so we tell them apart by call order: the first
    ``gpt-4o-mini`` call is the Triager, every subsequent one is the
    Releaser composing PR + Slack text.
    """

    def __init__(
        self,
        *,
        triager_payload: dict,
        investigator_messages: list[_ScriptedMessage],
        fix_author_payload: dict,
        releaser_payload: dict | None = None,
    ) -> None:
        self._triager = triager_payload
        self._inv = list(investigator_messages)
        self._fa = fix_author_payload
        self._releaser = releaser_payload or {
            "pr_title": "fix: autonomous null_deref hotfix",
            "pr_body": (
                "## Incident\n\nNull deref.\n\n"
                "## Root Cause\n\nMissing guard.\n\n"
                "## Fix\n\nAdd guard.\n\n"
                "## Verification\n\nTests passed.\n\n"
                "## Risk Assessment\n\nLow.\n\n"
                "---\n🤖 *Autonomously generated by the SRE Incident Response System*"
            ),
            "slack_message": (
                ":rotating_light: *Autonomous SRE Fix Deployed*\n"
                "*PR:* {{PR_URL}}"
            ),
            "commit_message": "fix: add null guard",
        }
        self.triager_calls = 0
        self.investigator_calls = 0
        self.fix_author_calls = 0
        self.releaser_calls = 0
        self.chat = _ScriptedClientChat(self)

    def _dispatch(self, kwargs: dict) -> _ScriptedResp:
        if "tools" in kwargs and kwargs["tools"]:
            # Investigator
            idx = min(self.investigator_calls, len(self._inv) - 1)
            self.investigator_calls += 1
            return _ScriptedResp(self._inv[idx])
        # Triager / Releaser / Fix Author — all use response_format=json_object.
        # Triager: gpt-4o-mini (first call), Releaser: gpt-4o-mini (later
        # calls), FixAuthor: gpt-4o.
        model = kwargs.get("model", "")
        if model == "gpt-4o-mini":
            if self.triager_calls == 0:
                self.triager_calls += 1
                return _ScriptedResp(
                    _ScriptedMessage(content=json.dumps(self._triager))
                )
            self.releaser_calls += 1
            return _ScriptedResp(
                _ScriptedMessage(content=json.dumps(self._releaser))
            )
        # Fix Author
        self.fix_author_calls += 1
        return _ScriptedResp(
            _ScriptedMessage(content=json.dumps(self._fa))
        )


class _StateMachineFakeGitHub:
    """GitHubTool stand-in used by every agent in the full-pipeline test.

    Supports the Investigator/Fix Author read surface as well as the
    Releaser write surface (``ensure_branch`` / ``get_file_with_sha`` /
    ``update_file_on_branch`` / ``find_pr_by_head`` / ``create_pull``).
    Recording lists let the test assert the Releaser actually called
    the right methods in the right order.
    """

    def __init__(self, file_body: str) -> None:
        self._body = file_body
        self.disabled = False
        self.calls: list[tuple[str, str]] = []
        # Releaser-specific recordings.
        self.ensure_branch_calls: list[dict] = []
        self.update_file_calls: list[dict] = []
        self.create_pull_calls: list[dict] = []
        self._next_pr_number = 101

    async def read_file(self, path: str, ref: str = "main", span: Any = None) -> str:
        self.calls.append((path, ref))
        return self._body

    async def read_file_at_lines(
        self, path: str, start: int, end: int, ref: str = "main", span: Any = None
    ) -> str:
        return "\n".join(self._body.splitlines()[max(0, start - 1) : end])

    async def list_recent_commits(
        self, path: str | None = None, limit: int = 10, span: Any = None
    ) -> list[dict]:
        return []

    # ---- Releaser write surface --------------------------------------

    async def get_file_with_sha(
        self, path: str, ref: str = "main", span: Any = None
    ) -> dict:
        return {"path": path, "sha": "fakesha0001", "content": self._body}

    async def ensure_branch(
        self, branch: str, base: str = "main", span: Any = None
    ) -> dict:
        self.ensure_branch_calls.append({"branch": branch, "base": base})
        return {"created": True, "branch": branch, "base_sha": "basesha"}

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
            {"path": path, "branch": branch, "message": message}
        )
        return {"commit_sha": "newcommit"}

    async def find_pr_by_head(self, branch: str, span: Any = None):
        return None

    async def create_pull(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        span: Any = None,
    ) -> dict:
        n = self._next_pr_number
        self._next_pr_number += 1
        self.create_pull_calls.append({"title": title, "head": head, "base": base})
        return {
            "number": n,
            "url": f"https://github.com/fake/repo/pull/{n}",
            "state": "open",
            "head": head,
            "base": base,
        }


class _FakeSearch:
    def __init__(self):
        self.enabled = False

    async def search(self, query: str, max_results: int = 5, span: Any = None):
        return []


def test_state_machine_full_null_deref_path(isolated_env, tmp_path, monkeypatch):
    """End-to-end null_deref incident: triager → investigator → fix → verify → release."""
    from fastapi.testclient import TestClient

    from orchestrator.orchestration import state_machine
    from orchestrator.agents import verifier as verifier_module

    src = _seed_sandbox_source(tmp_path)

    triager_payload = {
        "classification": "null_deref",
        "severity": "P1",
        "reasoning": "AttributeError on NoneType — missing null guard.",
        "confidence": 0.93,
    }

    investigator_messages = [
        _ScriptedMessage(
            content=json.dumps(
                {
                    "suspect_file": "app.py",
                    "suspect_line": 20,
                    "suspect_function": "shorten",
                    "root_cause": "Missing None guard on payload.url before .strip()",
                    "hypotheses": [
                        {
                            "description": "url is None and .strip() is called on it",
                            "evidence": "traceback shows AttributeError on NoneType",
                            "fix_strategy": "raise HTTPException(422) when url is None",
                            "confidence": 0.92,
                        }
                    ],
                    "investigation_log": [
                        "read traceback — app.py:20",
                        "no tool calls needed in this run",
                    ],
                }
            ),
        )
    ]

    fix_author_payload = {
        "file_path": "app.py",
        "diff": _make_clean_patch(),
        "explanation": "Add a 422 guard so missing url is rejected rather than crashing.",
        "test_should_pass": "test_shorten_null_returns_422",
        "risk_level": "low",
        "confidence": 0.9,
    }

    scripted = ScriptedClient(
        triager_payload=triager_payload,
        investigator_messages=investigator_messages,
        fix_author_payload=fix_author_payload,
    )
    fake_github = _StateMachineFakeGitHub(file_body=_BUGGY_APP_PY)
    fake_search = _FakeSearch()

    monkeypatch.setattr(state_machine, "_build_openai_client", lambda: scripted)
    monkeypatch.setattr(state_machine, "GitHubTool", lambda *a, **kw: fake_github)
    monkeypatch.setattr(state_machine, "WebSearchTool", lambda *a, **kw: fake_search)

    # Point the race's verifier sandboxes at our seeded ``victim_app``
    # skeleton instead of the real source tree. The race reads
    # ``state_machine.VERIFIER_SOURCE_DIR`` so this single monkeypatch
    # covers both race participants.
    monkeypatch.setattr(state_machine, "VERIFIER_SOURCE_DIR", str(src))
    _ = verifier_module  # keep the import alive — used below for type refs

    from orchestrator.main import app

    payload = {
        "endpoint": "POST /shorten",
        "exception_class": "AttributeError",
        "exception_message": "'NoneType' object has no attribute 'strip'",
        "traceback": (
            "Traceback (most recent call last):\n"
            '  File "app.py", line 20, in shorten\n'
            "    normalized = url.strip().lower()\n"
            "AttributeError: 'NoneType' object has no attribute 'strip'"
        ),
        "request_body": '{"url": null}',
        "source_service": "victim_app",
    }

    with TestClient(app) as client:
        resp = client.post("/webhooks/alert", json=payload)
        assert resp.status_code == 202, resp.text
        incident_id = resp.json()["incident_id"]

        terminal = {
            "released",
            "release_failed",
            "pending_release",
            "fix_rejected",
            "fix_failed",
            "verification_failed",
            "escalated",
            "investigation_failed",
            "failed",
        }
        deadline = time.monotonic() + 60.0
        detail: dict = {}
        while time.monotonic() < deadline:
            detail = client.get(f"/webhooks/alert/{incident_id}").json()
            if detail.get("incident", {}).get("status") in terminal:
                break
            time.sleep(0.2)

    incident = detail["incident"]
    assert incident["status"] == "released", (
        f"expected released, got {incident['status']}; "
        f"verification_result={incident.get('verification_result')}; "
        f"runs={[r.get('agent_name') + ':' + r.get('status', '?') + (':' + r.get('error_message', '')[:200] if r.get('error_message') else '') for r in detail.get('agent_runs', [])]}"
    )
    assert incident["classification"] == "null_deref"
    assert incident["proposed_patch"], "proposed_patch should be persisted"
    assert "@@" in incident["proposed_patch"]
    assert incident["tests_passed"] is True
    parsed_verif = json.loads(incident["verification_result"])
    assert parsed_verif["verdict"] == "verified"
    # Releaser persistence checks.
    assert incident.get("pr_url"), "pr_url should be set after release"
    assert incident["pr_url"].startswith("https://github.com/")
    assert incident.get("pr_branch", "").startswith("autosre/inc-")

    runs = detail["agent_runs"]
    by_name = {r["agent_name"]: r for r in runs}
    # Triager / Investigator / Releaser still emit a single AgentRun
    # each. The Fix Author and Verifier are now raced: one row per
    # strategy. The state machine writes the WINNING patch back to the
    # incident, so at least one fix_author_* and the corresponding
    # verifier_* must succeed.
    for expected in ("triager", "investigator", "releaser"):
        assert expected in by_name, f"missing AgentRun for {expected}: {runs}"
        assert by_name[expected]["status"] == "success", by_name[expected]

    fix_author_runs = [r for r in runs if r["agent_name"].startswith("fix_author_")]
    verifier_runs = [r for r in runs if r["agent_name"].startswith("verifier_")]
    assert len(fix_author_runs) == 2, (
        f"expected 2 fix_author_* AgentRuns (one per strategy), got "
        f"{[r['agent_name'] for r in fix_author_runs]}"
    )
    assert len(verifier_runs) == 2, (
        f"expected 2 verifier_* AgentRuns (one per strategy), got "
        f"{[r['agent_name'] for r in verifier_runs]}"
    )
    # At least one verifier strategy succeeded (the winner).
    assert any(r["status"] == "success" for r in verifier_runs), verifier_runs

    # The verification_result blob now records the race summary.
    parsed_race = json.loads(incident["verification_result"])
    assert parsed_race.get("winning_strategy") in {"defensive", "permissive"}
    assert len(parsed_race.get("all_candidates", [])) == 2

    # Releaser must have used the GitHub write surface.
    assert len(fake_github.ensure_branch_calls) == 1, fake_github.ensure_branch_calls
    assert len(fake_github.update_file_calls) == 1, fake_github.update_file_calls
    assert len(fake_github.create_pull_calls) == 1, fake_github.create_pull_calls
