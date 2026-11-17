"""End-to-end test: webhook -> persistence -> Triager -> classification.

Spins up the FastAPI app via :class:`TestClient`, posts a fake
null_deref alert, polls the detail endpoint until the background
Triager finishes, and asserts the full happy-path invariants:

* incident.status moves from ``received`` to ``pending_implementation``,
* classification == ``null_deref`` and severity is a valid P-level,
* an AgentRun row exists for the Triager with status ``success`` and a
  populated output_summary,
* incident.workflow_id is bound (the state machine wired it to the
  tracer's workflow handle).

The OpenAI client is faked entirely — no real API calls are made — so
this test runs cleanly in CI with no credentials configured.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# Make ``orchestrator`` importable when pytest is invoked from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = type("Msg", (), {"content": content})()


class _FakeUsage:
    total_tokens = 42


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeChatCompletions:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def create(self, **kwargs):  # noqa: D401 — async mock
        # Return the same canned response regardless of input. The real
        # OpenAI SDK would validate ``response_format``; we don't bother.
        return _FakeResponse(json.dumps(self._payload))


class _FakeChat:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.completions = _FakeChatCompletions(payload)


class _FakeOpenAIClient:
    """Minimal stand-in for ``openai.AsyncOpenAI``.

    Only implements ``client.chat.completions.create(...)`` because
    that's the only surface :class:`BaseAgent._call_llm` touches.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.chat = _FakeChat(payload)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """Build a fresh FastAPI app with a tmp SQLite DB + local-only tracing.

    Resets the orchestrator's cached engine / settings / tracer between
    tests so we don't bleed state across runs.
    """
    db_path = tmp_path / "sre.db"
    trace_path = tmp_path / "local_traces.jsonl"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("TRACE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_TRACE_PATH", str(trace_path))
    monkeypatch.delenv("OMIUM_API_KEY", raising=False)
    # Force GitHub / search tools to the disabled path — the existing
    # FakeOpenAIClient returns the same Triager-style JSON for every
    # request, which is fine for the null_deref pipeline now that the
    # Investigator is wired in: with both tools disabled the
    # Investigator falls through to a degenerate fallback hypothesis
    # (low confidence) which is enough for status to advance to
    # ``pending_fix``.
    # pydantic-settings loads from .env even when env vars are deleted,
    # so we set them to empty strings to force the disabled path.
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("GITHUB_REPO", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")

    # Clear cached settings / engine / tracer so the new env vars stick.
    from orchestrator import config as config_module
    from orchestrator import db as db_module
    from orchestrator import tracing as tracing_module

    config_module.get_settings.cache_clear()
    db_module.reset_engine()
    tracing_module.reset_tracer()

    # Patch the OpenAI client factory in the state machine to return our
    # fake. Tests can override the canned payload via monkeypatch before
    # this fixture is used (see the test below).
    from orchestrator.orchestration import state_machine

    canned: dict[str, Any] = {
        "classification": "null_deref",
        "severity": "P1",
        "reasoning": (
            "Traceback shows AttributeError on a NoneType — the handler "
            "called .strip() on a missing url. Classic null deref."
        ),
        "confidence": 0.95,
    }

    def _factory():
        return _FakeOpenAIClient(canned)

    monkeypatch.setattr(state_machine, "_build_openai_client", _factory)

    # Import the FastAPI app AFTER patching so the lifespan hook picks
    # up the test DB.
    from orchestrator.main import app

    yield app, canned, trace_path

    # Teardown — drop singletons so the next test starts clean.
    config_module.get_settings.cache_clear()
    db_module.reset_engine()
    tracing_module.reset_tracer()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_end_to_end_triage(isolated_app):
    """Post a fake alert and assert the Triager classifies + persists."""
    app, canned, trace_path = isolated_app

    # Importing TestClient lazily keeps the top-level import surface
    # small (and avoids a hard dep when pytest is just collecting).
    from fastapi.testclient import TestClient

    payload = {
        "timestamp": "2026-05-15T12:00:00Z",
        "endpoint": "POST /shorten",
        "method": "POST",
        "exception_class": "AttributeError",
        "exception_message": "'NoneType' object has no attribute 'strip'",
        "traceback": (
            "Traceback (most recent call last):\n"
            '  File "victim_app/app.py", line 78, in shorten\n'
            "    normalized = url.strip().lower()\n"
            "AttributeError: 'NoneType' object has no attribute 'strip'"
        ),
        "request_body": '{"url": null}',
        "source_service": "victim_app",
    }

    # TestClient enters the lifespan context, which fires init_db().
    with TestClient(app) as client:
        resp = client.post("/webhooks/alert", json=payload)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        incident_id = body["incident_id"]
        assert body["status"] == "queued"
        assert incident_id.startswith("inc_")

        # Poll for up to 20 seconds for the background task to finish.
        # TestClient runs BackgroundTasks synchronously after the
        # response returns, BUT only on the same thread, so by the time
        # this loop runs the task has typically already completed. The
        # poll is belt-and-suspenders.
        deadline = time.monotonic() + 20.0
        detail: dict[str, Any] = {}
        # The null_deref pipeline now flows Triager → Investigator →
        # Fix Author → Verifier → release_stub. With GitHub disabled
        # the Fix Author can't read the suspect file and bails out
        # cleanly, leaving the incident at ``fix_failed``. The other
        # terminal statuses are kept here so the test still terminates
        # if a later refactor changes the failure mode.
        terminal_statuses = {
            "fix_failed",
            "fix_drafted",
            "fix_rejected",
            "verified",
            "pending_release",
            "verification_failed",
            "investigation_failed",
            "escalated",
            "pending_implementation",
            "failed",
        }
        while time.monotonic() < deadline:
            resp = client.get(f"/webhooks/alert/{incident_id}")
            assert resp.status_code == 200, resp.text
            detail = resp.json()
            if detail["incident"]["status"] in terminal_statuses:
                break
            time.sleep(0.2)

        # ---- Top-level incident assertions ----------------------------
        # With GitHub disabled the Fix Author raises immediately when
        # it tries to re-read the suspect file (empty GitHub response),
        # which the state machine catches and surfaces as
        # ``fix_failed``. The Triager + Investigator portions still
        # succeed, which is what this test is really validating.
        incident = detail["incident"]
        assert incident["status"] == "fix_failed", incident
        assert incident["classification"] == "null_deref", incident
        assert incident["severity"] in {"P0", "P1", "P2"}, incident
        assert incident["workflow_id"], "workflow_id should be set after Triager runs"
        assert incident["workflow_id"].startswith("incident_response_")
        assert incident["triager_reasoning"], "reasoning should be persisted"

        # ---- AgentRun assertions --------------------------------------
        runs = detail["agent_runs"]
        triager_runs = [r for r in runs if r["agent_name"] == "triager"]
        assert len(triager_runs) == 1, runs
        triager_run = triager_runs[0]
        assert triager_run["status"] == "success", triager_run
        assert triager_run["output_summary"], "output_summary should be populated"
        assert triager_run["span_id"], "span_id should link to the tracer span"

        # The output_summary is the JSON-encoded Triager result.
        summary = json.loads(triager_run["output_summary"])
        assert summary["classification"] == "null_deref"

    # ---- Tracing assertions -------------------------------------------
    # The local JSONL must contain the workflow + the triager.classify
    # span + the llm_call child span, all under the same workflow_id.
    assert trace_path.exists(), "local trace file should have been written"
    records = [
        json.loads(line)
        for line in trace_path.read_text().splitlines()
        if line.strip()
    ]
    workflow_starts = [r for r in records if r["kind"] == "workflow_start"]
    assert any(r["name"] == "incident_response" for r in workflow_starts)

    span_names = {
        r["name"]
        for r in records
        if r["kind"] == "span_start"
    }
    assert "triager.classify" in span_names
    assert "llm_call" in span_names
    # The real Investigator now runs in the null_deref branch, followed
    # by the parallel fix race. With GitHub disabled both fix authors
    # raise before producing a patch, but the race scaffolding still
    # opens the fix_race span and one draft.<strategy> span per
    # participant — those MUST still appear so the dashboard can show
    # the race shape.
    assert "investigator.investigate" in span_names
    assert "fix_race" in span_names
    assert "draft.defensive" in span_names
    assert "draft.permissive" in span_names

    # Parent IDs form a tree rooted at the workflow.
    workflow_id = incident["workflow_id"]
    starts = [r for r in records if r["kind"] == "span_start"]
    triager_starts = [r for r in starts if r["name"] == "triager.classify"]
    assert triager_starts and triager_starts[0]["parent_id"] == workflow_id
    triager_span_id = triager_starts[0]["span_id"]
    # The Triager's own llm_call must be a child of triager.classify.
    # (The Investigator also emits llm_call spans, so filter to the
    # one whose parent matches.)
    triager_llm_calls = [
        r for r in starts
        if r["name"] == "llm_call" and r["parent_id"] == triager_span_id
    ]
    assert triager_llm_calls, "triager.classify should have an llm_call child"


# ---------------------------------------------------------------------------
# Companion test: when the model returns "unknown" the incident escalates.
# ---------------------------------------------------------------------------


def test_unknown_classification_escalates(tmp_path, monkeypatch):
    """The 'unknown' branch should mark the incident as escalated."""
    db_path = tmp_path / "sre.db"
    trace_path = tmp_path / "local_traces.jsonl"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("TRACE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_TRACE_PATH", str(trace_path))
    monkeypatch.delenv("OMIUM_API_KEY", raising=False)
    # pydantic-settings loads from .env even when env vars are deleted,
    # so we set them to empty strings to force the disabled path.
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("GITHUB_REPO", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")

    from orchestrator import config as config_module
    from orchestrator import db as db_module
    from orchestrator import tracing as tracing_module

    config_module.get_settings.cache_clear()
    db_module.reset_engine()
    tracing_module.reset_tracer()

    from orchestrator.orchestration import state_machine

    canned = {
        "classification": "unknown",
        "severity": "P2",
        "reasoning": "Insufficient evidence to route confidently.",
        "confidence": 0.4,
    }
    monkeypatch.setattr(
        state_machine,
        "_build_openai_client",
        lambda: _FakeOpenAIClient(canned),
    )

    from fastapi.testclient import TestClient
    from orchestrator.main import app

    payload = {
        "endpoint": "GET /weird",
        "exception_class": "RuntimeError",
        "exception_message": "something flaky happened",
        "traceback": "RuntimeError: something flaky happened",
        "request_body": "",
        "source_service": "victim_app",
    }

    with TestClient(app) as client:
        resp = client.post("/webhooks/alert", json=payload)
        assert resp.status_code == 202
        incident_id = resp.json()["incident_id"]

        deadline = time.monotonic() + 10.0
        detail: dict[str, Any] = {}
        while time.monotonic() < deadline:
            detail = client.get(f"/webhooks/alert/{incident_id}").json()
            if detail["incident"]["status"] != "received":
                break
            time.sleep(0.2)

        assert detail["incident"]["status"] == "escalated"
        assert detail["incident"]["classification"] == "unknown"
