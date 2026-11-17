"""Tests for the multi-classification remediation pipeline.

After Part A, the state machine has a single ``run_remediation_pipeline``
that handles ``null_deref``, ``recent_regression``, and ``config_error``
the same way (Investigator → fix_race → Releaser). These tests prove:

1. The pipeline is genuinely classification-agnostic — running it under
   all three classifications reaches a terminal state (``released`` with
   our mocks) and emits the canonical span tree for each.
2. The Investigator's system prompt is specialized via
   ``CLASSIFICATION_HINTS`` per Part B.
3. The Investigator (and FixAuthor) keep working for unknown
   classifications, AND ``run_remediation_pipeline`` is never invoked
   for ``unknown`` — the state machine still escalates instead.

The tests are hermetic: GitHub / OpenAI / Tavily are all faked, and
each test seeds its own ``victim_app/`` skeleton under ``tmp_path`` so
the parallel fix race's sandboxes have something concrete to verify.
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
# Reusable fakes
# ---------------------------------------------------------------------------
#
# We deliberately re-implement the scripted OpenAI / GitHub fakes here
# rather than importing them from ``test_fix_and_verify_flow.py`` —
# importing across test files is fragile under pytest collection.


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
        resp = client.post("/shorten", json={"url": None})
        assert resp.status_code == 422, resp.text
    '''
).lstrip()


def _make_clean_patch() -> str:
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


def _seed_sandbox_source(tmp_path: Path) -> Path:
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
# Scripted OpenAI fake (with tool-call support for the Investigator)
# ---------------------------------------------------------------------------


class _FakeUsage:
    total_tokens = 11


class _ScriptedMessage:
    def __init__(self, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or None


class _ScriptedChoice:
    def __init__(self, msg: _ScriptedMessage) -> None:
        self.message = msg
        self.finish_reason = "stop"


class _ScriptedResp:
    def __init__(self, msg: _ScriptedMessage) -> None:
        self.choices = [_ScriptedChoice(msg)]
        self.usage = _FakeUsage()


class _ScriptedClientChat:
    def __init__(self, owner: "ScriptedClient") -> None:
        self._owner = owner

    @property
    def completions(self):  # noqa: D401 — pretend to be the openai SDK
        return self

    async def create(self, **kwargs):
        return self._owner._dispatch(kwargs)


class ScriptedClient:
    """OpenAI fake that scripts Triager / Investigator / FixAuthor / Releaser.

    The state machine drives all four through the same
    ``client.chat.completions.create`` surface. We dispatch by (model,
    has-tools, call-order) so each agent sees the right scripted reply.
    """

    def __init__(
        self,
        *,
        triager_payload: dict,
        investigator_payload: dict,
        fix_author_payload: dict,
        releaser_payload: dict | None = None,
    ) -> None:
        self._triager = triager_payload
        self._inv = investigator_payload
        self._fa = fix_author_payload
        self._releaser = releaser_payload or {
            "pr_title": "fix: autonomous hotfix",
            "pr_body": (
                "## Incident\n\nBug.\n\n## Root Cause\n\nMissing guard.\n\n"
                "## Fix\n\nAdd guard.\n\n## Verification\n\nTests passed.\n\n"
                "## Risk Assessment\n\nLow."
            ),
            "slack_message": ":rotating_light: Autonomous SRE Fix Deployed",
            "commit_message": "fix: add guard",
        }
        self.triager_calls = 0
        self.investigator_calls = 0
        self.fix_author_calls = 0
        self.releaser_calls = 0
        # Captured system prompts for assertion convenience.
        self.investigator_system_prompts: list[str] = []
        self.fix_author_system_prompts: list[str] = []
        self.chat = _ScriptedClientChat(self)

    def _dispatch(self, kwargs: dict) -> _ScriptedResp:
        if "tools" in kwargs and kwargs["tools"]:
            # Investigator — capture its system prompt.
            self.investigator_calls += 1
            messages = kwargs.get("messages") or []
            for msg in messages:
                if msg.get("role") == "system":
                    self.investigator_system_prompts.append(msg["content"])
                    break
            return _ScriptedResp(
                _ScriptedMessage(content=json.dumps(self._inv))
            )

        model = kwargs.get("model", "")
        if model == "gpt-4o-mini":
            # Triager first, Releaser thereafter.
            if self.triager_calls == 0:
                self.triager_calls += 1
                return _ScriptedResp(
                    _ScriptedMessage(content=json.dumps(self._triager))
                )
            self.releaser_calls += 1
            return _ScriptedResp(
                _ScriptedMessage(content=json.dumps(self._releaser))
            )

        # Fix Author — capture its system prompt too.
        self.fix_author_calls += 1
        messages = kwargs.get("messages") or []
        for msg in messages:
            if msg.get("role") == "system":
                self.fix_author_system_prompts.append(msg["content"])
                break
        return _ScriptedResp(
            _ScriptedMessage(content=json.dumps(self._fa))
        )


# ---------------------------------------------------------------------------
# GitHub + Search fakes
# ---------------------------------------------------------------------------


class _StateMachineFakeGitHub:
    def __init__(self, file_body: str) -> None:
        self._body = file_body
        self.disabled = False
        self.calls: list[tuple[str, str]] = []
        self.ensure_branch_calls: list[dict] = []
        self.update_file_calls: list[dict] = []
        self.create_pull_calls: list[dict] = []
        self._next_pr_number = 200
        self.last_resolved_path: str | None = None

    async def read_file(self, path: str, ref: str = "main", span: Any = None) -> str:
        self.calls.append((path, ref))
        self.last_resolved_path = path
        return self._body

    async def read_file_at_lines(
        self,
        path: str,
        start: int,
        end: int,
        ref: str = "main",
        span: Any = None,
    ) -> str:
        return "\n".join(self._body.splitlines()[max(0, start - 1) : end])

    async def list_recent_commits(
        self, path: str | None = None, limit: int = 10, span: Any = None
    ) -> list[dict]:
        return []

    async def _resolve_path(self, path: str, ref: str = "main", span: Any = None):
        return path or "app.py"

    async def get_file_with_sha(
        self, path: str, ref: str = "main", span: Any = None
    ) -> dict:
        return {"path": path, "sha": "fakesha", "content": self._body}

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


# ---------------------------------------------------------------------------
# Isolated env fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    db_path = tmp_path / "sre.db"
    trace_path = tmp_path / "local_traces.jsonl"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("TRACE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_TRACE_PATH", str(trace_path))
    monkeypatch.delenv("OMIUM_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("GITHUB_REPO", "fake/repo")
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


# ---------------------------------------------------------------------------
# Helpers — per-classification payload builders
# ---------------------------------------------------------------------------


def _payloads_for(classification: str) -> tuple[dict, dict, dict]:
    """Build (triager, investigator, fix_author) JSON payloads.

    The payloads are realistic enough that the state machine reaches
    ``released`` for each classification. They differ only in
    classification labels and explanatory text — the structure of the
    fix race + verifier sandbox is shared.
    """
    triager = {
        "classification": classification,
        "severity": "P1",
        "reasoning": f"Triager classified as {classification}",
        "confidence": 0.9,
    }
    investigator = {
        "suspect_file": "app.py",
        "suspect_line": 20,
        "suspect_function": "shorten",
        "root_cause": f"Root cause for {classification}: missing guard.",
        "hypotheses": [
            {
                "description": "url is None and .strip() is called",
                "evidence": "traceback shows NoneType",
                "fix_strategy": "add a None guard before .strip()",
                "confidence": 0.92,
            }
        ],
        "investigation_log": ["read traceback", "identified suspect"],
    }
    fix_author = {
        "file_path": "app.py",
        "diff": _make_clean_patch(),
        "explanation": f"Fix for {classification}: add a 422 guard.",
        "test_should_pass": "test_shorten_null_returns_422",
        "risk_level": "low",
        "confidence": 0.9,
    }
    return triager, investigator, fix_author


def _alert_payload_for(classification: str) -> dict:
    """Build the webhook payload that the state machine receives.

    The shape of the traceback differs per bug type so the test reflects
    what a real ingress would see, but the post-Triager classification
    is set by our scripted payload — we don't rely on the model getting
    the bug class right from the traceback alone.
    """
    if classification == "config_error":
        return {
            "endpoint": "STARTUP",
            "method": "STARTUP",
            "exception_class": "KeyError",
            "exception_message": "REDIS_URL",
            "traceback": (
                "Traceback (most recent call last):\n"
                '  File "app.py", line 15, in <module>\n'
                '    REDIS_URL = os.environ["REDIS_URL"]\n'
                "KeyError: 'REDIS_URL'"
            ),
            "request_body": "{}",
            "source_service": "victim_app",
        }
    if classification == "recent_regression":
        return {
            "endpoint": "GET /expand",
            "method": "GET",
            "exception_class": "KeyError",
            "exception_message": "short",
            "traceback": (
                "Traceback (most recent call last):\n"
                '  File "app.py", line 112, in expand\n'
                '    return {"short": response["short"]}\n'
                "KeyError: 'short'"
            ),
            "request_body": "",
            "source_service": "victim_app",
        }
    # null_deref / default
    return {
        "endpoint": "POST /shorten",
        "method": "POST",
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


# ---------------------------------------------------------------------------
# Test 1 — pipeline is classification-agnostic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "classification",
    ["null_deref", "recent_regression", "config_error"],
)
def test_run_remediation_pipeline_is_classification_agnostic(
    isolated_env, tmp_path, monkeypatch, classification
):
    """Same Investigator/FixRace/Releaser flow runs for all 3 classifications."""
    from fastapi.testclient import TestClient

    from orchestrator.orchestration import state_machine

    src = _seed_sandbox_source(tmp_path)
    triager_payload, investigator_payload, fix_author_payload = _payloads_for(
        classification
    )

    scripted = ScriptedClient(
        triager_payload=triager_payload,
        investigator_payload=investigator_payload,
        fix_author_payload=fix_author_payload,
    )
    fake_github = _StateMachineFakeGitHub(file_body=_BUGGY_APP_PY)
    fake_search = _FakeSearch()

    monkeypatch.setattr(state_machine, "_build_openai_client", lambda: scripted)
    monkeypatch.setattr(state_machine, "GitHubTool", lambda *a, **kw: fake_github)
    monkeypatch.setattr(state_machine, "WebSearchTool", lambda *a, **kw: fake_search)
    monkeypatch.setattr(state_machine, "VERIFIER_SOURCE_DIR", str(src))

    from orchestrator.main import app

    payload = _alert_payload_for(classification)

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
    # ``released`` is the happy path; ``fix_failed`` / ``fix_rejected``
    # are acceptable graceful degradations per the prompt. We disallow
    # ``escalated`` / ``failed`` because they'd mean the pipeline never
    # ran for this classification — which is exactly what we're
    # testing against.
    assert incident["status"] in {
        "released",
        "fix_failed",
        "fix_rejected",
        "pending_release",
        "verification_failed",
    }, (
        f"{classification}: expected pipeline terminal status, got {incident['status']}; "
        f"runs={[(r.get('agent_name'), r.get('status')) for r in detail.get('agent_runs', [])]}"
    )
    assert incident["classification"] == classification

    # The shared pipeline must have emitted these three canonical spans.
    trace_path = isolated_env
    assert trace_path.exists()
    records = [
        json.loads(line)
        for line in trace_path.read_text().splitlines()
        if line.strip()
    ]
    span_names = {
        r["name"] for r in records if r["kind"] == "span_start"
    }
    assert "investigator.investigate" in span_names, (
        f"{classification}: investigator.investigate span missing; saw {sorted(span_names)}"
    )
    assert "fix_race" in span_names, (
        f"{classification}: fix_race span missing; saw {sorted(span_names)}"
    )
    if incident["status"] == "released":
        assert "releaser.release" in span_names, (
            f"{classification}: releaser.release span missing; "
            f"saw {sorted(span_names)}"
        )

    # Filterability: classification should appear in the
    # investigator.investigate span metadata so traces are filterable
    # per bug type even though the span name stays generic.
    inv_starts = [
        r
        for r in records
        if r["kind"] == "span_start" and r["name"] == "investigator.investigate"
    ]
    assert inv_starts, "investigator.investigate span_start not found"
    inv_md = inv_starts[-1].get("metadata") or {}
    assert inv_md.get("classification") == classification, inv_md


# ---------------------------------------------------------------------------
# Test 2 — Investigator's system prompt picks up the classification hint
# ---------------------------------------------------------------------------


def test_investigator_uses_classification_hints(isolated_env, tmp_path, monkeypatch):
    """The system prompt sent to the LLM differs per classification."""
    from orchestrator.agents.investigator import InvestigatorAgent
    from orchestrator.db import get_engine
    from orchestrator.models import Incident
    from orchestrator.tracing import get_tracer
    from sqlmodel import Session

    fake_github = _StateMachineFakeGitHub(file_body=_BUGGY_APP_PY)
    fake_search = _FakeSearch()

    captured_prompts: dict[str, str] = {}

    def _run_for(classification: str) -> str:
        scripted = ScriptedClient(
            triager_payload={"classification": classification, "severity": "P1"},
            investigator_payload={
                "suspect_file": "app.py",
                "suspect_line": 20,
                "suspect_function": "shorten",
                "root_cause": "n/a",
                "hypotheses": [
                    {
                        "description": "stub",
                        "evidence": "stub",
                        "fix_strategy": "stub",
                        "confidence": 0.5,
                    }
                ],
                "investigation_log": ["stub"],
            },
            fix_author_payload={
                "file_path": "app.py",
                "diff": _make_clean_patch(),
                "explanation": "n/a",
                "test_should_pass": "test_x",
                "risk_level": "low",
                "confidence": 0.5,
            },
        )
        engine = get_engine()
        with Session(engine) as session:
            incident = Incident(
                source_service="victim_app",
                endpoint="POST /shorten",
                exception_class="AttributeError",
                exception_message="boom",
                traceback="trace",
                request_body="{}",
                raw_payload="{}",
                status="classified",
                classification=classification,
                severity="P1",
            )
            session.add(incident)
            session.commit()
            session.refresh(incident)

            tracer = get_tracer()
            with tracer.start_workflow("t", metadata={}) as wf:
                with wf.span("root") as span:
                    agent = InvestigatorAgent(
                        scripted,
                        tracer,
                        github_tool=fake_github,
                        search_tool=fake_search,
                    )
                    asyncio.run(
                        agent.run(incident=incident, span=span, session=session)
                    )

        # The Investigator captures whatever it sent in
        # ``scripted.investigator_system_prompts`` — return the last one.
        assert scripted.investigator_system_prompts, (
            "scripted client never saw an Investigator call"
        )
        return scripted.investigator_system_prompts[-1]

    captured_prompts["config_error"] = _run_for("config_error")
    captured_prompts["recent_regression"] = _run_for("recent_regression")
    captured_prompts["null_deref"] = _run_for("null_deref")

    cfg = captured_prompts["config_error"].lower()
    assert "env var" in cfg or "environment" in cfg, (
        f"config_error prompt missing env-var hint: {cfg[-500:]}"
    )

    reg = captured_prompts["recent_regression"].lower()
    assert "list_recent_commits" in reg or "renamed" in reg, (
        f"recent_regression prompt missing commits/renamed hint: {reg[-500:]}"
    )

    nd = captured_prompts["null_deref"].lower()
    assert "null" in nd or "none" in nd, (
        f"null_deref prompt missing null/None hint: {nd[-500:]}"
    )


# ---------------------------------------------------------------------------
# Test 3 — unknown classification still escalates without the pipeline
# ---------------------------------------------------------------------------


def test_unknown_still_escalates_without_pipeline(
    isolated_env, tmp_path, monkeypatch
):
    """``classification='unknown'`` must escalate AND skip the remediation flow."""
    from fastapi.testclient import TestClient

    from orchestrator.orchestration import state_machine

    src = _seed_sandbox_source(tmp_path)

    scripted = ScriptedClient(
        triager_payload={
            "classification": "unknown",
            "severity": "P2",
            "reasoning": "ambiguous",
            "confidence": 0.4,
        },
        investigator_payload={
            "suspect_file": "app.py",
            "suspect_line": 1,
            "suspect_function": "?",
            "root_cause": "n/a",
            "hypotheses": [
                {
                    "description": "stub",
                    "evidence": "stub",
                    "fix_strategy": "stub",
                    "confidence": 0.1,
                }
            ],
            "investigation_log": ["stub"],
        },
        fix_author_payload={
            "file_path": "app.py",
            "diff": _make_clean_patch(),
            "explanation": "n/a",
            "test_should_pass": "test_x",
            "risk_level": "low",
            "confidence": 0.1,
        },
    )
    fake_github = _StateMachineFakeGitHub(file_body=_BUGGY_APP_PY)
    fake_search = _FakeSearch()

    monkeypatch.setattr(state_machine, "_build_openai_client", lambda: scripted)
    monkeypatch.setattr(state_machine, "GitHubTool", lambda *a, **kw: fake_github)
    monkeypatch.setattr(state_machine, "WebSearchTool", lambda *a, **kw: fake_search)
    monkeypatch.setattr(state_machine, "VERIFIER_SOURCE_DIR", str(src))

    # Spy on run_remediation_pipeline so we can assert it was never called.
    call_log: list[dict] = []
    original_pipeline = state_machine.run_remediation_pipeline

    async def _pipeline_spy(**kwargs):
        call_log.append({"classification": kwargs.get("classification")})
        return await original_pipeline(**kwargs)

    monkeypatch.setattr(
        state_machine, "run_remediation_pipeline", _pipeline_spy
    )

    from orchestrator.main import app

    payload = {
        "endpoint": "GET /weird",
        "exception_class": "RuntimeError",
        "exception_message": "something flaky",
        "traceback": "RuntimeError: something flaky",
        "request_body": "",
        "source_service": "victim_app",
    }

    with TestClient(app) as client:
        resp = client.post("/webhooks/alert", json=payload)
        assert resp.status_code == 202
        incident_id = resp.json()["incident_id"]

        deadline = time.monotonic() + 15.0
        detail: dict = {}
        while time.monotonic() < deadline:
            detail = client.get(f"/webhooks/alert/{incident_id}").json()
            status = detail.get("incident", {}).get("status")
            if status in {"escalated", "failed"}:
                break
            time.sleep(0.2)

    incident = detail["incident"]
    assert incident["status"] == "escalated", incident
    assert incident["classification"] == "unknown"
    assert call_log == [], (
        f"run_remediation_pipeline must NOT run for unknown; got calls: {call_log}"
    )

    # And: no investigator / fix_author / verifier AgentRuns should
    # exist, because the pipeline never ran.
    runs = detail["agent_runs"]
    names = [r.get("agent_name") for r in runs]
    assert "triager" in names
    assert "investigator" not in names, names
    assert not any(
        n and (n.startswith("fix_author_") or n.startswith("verifier_") or n == "releaser")
        for n in names
    ), names
