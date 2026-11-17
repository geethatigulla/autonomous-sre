"""Tests for ``orchestrator.tracing``.

The tracing module must:

* degrade silently to a local JSONL log when ``OMIUM_API_KEY`` is missing,
* not crash the calling code when the real Omium backend is in play, and
* gracefully fall back when the Omium endpoint is unreachable.

These tests cover all three guarantees. They mutate environment variables
freely; the ``_clean_env`` autouse fixture restores state and resets the
tracer singleton between runs so we never bleed state across tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# Make ``orchestrator`` importable when pytest is invoked from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from orchestrator import tracing as tracing_module  # noqa: E402


OMIUM_ENV_VARS = (
    "OMIUM_API_KEY",
    "OMIUM_API_BASE",
    "OMIUM_API_URL",
    "OMIUM_PROJECT",
    "TRACE_BACKEND",
    "LOCAL_TRACE_PATH",
)


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """Snapshot/restore env vars and point JSONL output at a temp file."""
    # Clear any prior tracer singleton.
    tracing_module.reset_tracer()

    # Make every run write to a fresh JSONL file by default.
    jsonl_path = tmp_path / "local_traces.jsonl"
    monkeypatch.setenv("LOCAL_TRACE_PATH", str(jsonl_path))

    # Make sure pytest isolation actually isolates tracing env vars.
    for var in ("OMIUM_API_KEY", "OMIUM_API_BASE", "OMIUM_API_URL", "TRACE_BACKEND"):
        monkeypatch.delenv(var, raising=False)

    yield jsonl_path

    tracing_module.reset_tracer()


def _read_jsonl(path: Path) -> list[dict]:
    assert path.exists(), f"expected local trace file at {path}"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Case 1 — local fallback when no Omium key is present
# ---------------------------------------------------------------------------


def test_local_fallback_when_no_key(_clean_env, monkeypatch):
    jsonl_path: Path = _clean_env

    monkeypatch.delenv("OMIUM_API_KEY", raising=False)
    monkeypatch.setenv("TRACE_BACKEND", "local")

    tracer = tracing_module.Tracer(service_name="test")
    handle = tracer.start_workflow("incident_response", metadata={"sev": "1"})

    with handle.span("outer") as outer:
        assert outer.parent_id == handle.workflow_id
        with outer.child_span("middle") as middle:
            assert middle.parent_id == outer.span_id
            with middle.child_span("inner") as inner:
                assert inner.parent_id == middle.span_id

    handle.finish(status="success", output={"resolved": True})

    records = _read_jsonl(jsonl_path)
    kinds = [r["kind"] for r in records]

    # workflow_start + 3 span_start + 3 span_end + workflow_end
    assert kinds.count("workflow_start") == 1
    assert kinds.count("workflow_end") == 1
    assert kinds.count("span_start") == 3
    assert kinds.count("span_end") == 3
    # Total: 8 lines (5+ is the floor the spec required — we exceed it).
    assert len(records) == 8

    # Causal chain: every span_start must reference its parent.
    starts = [r for r in records if r["kind"] == "span_start"]
    assert starts[0]["parent_id"] == handle.workflow_id
    assert starts[1]["parent_id"] == starts[0]["span_id"]
    assert starts[2]["parent_id"] == starts[1]["span_id"]

    # parent_id is also stamped on the metadata so JSONL consumers can
    # reconstruct the tree even without referring to the dedicated field.
    for record in starts:
        assert record["metadata"]["parent_id"] == record["parent_id"]


# ---------------------------------------------------------------------------
# Case 2 — Omium backend should never crash the caller
# ---------------------------------------------------------------------------


def test_omium_backend_does_not_crash(_clean_env, monkeypatch):
    """With a (possibly fake) key in 'auto' mode, the wrapper must not raise.

    We deliberately use a syntactically-valid but unreachable endpoint so the
    test passes without real Omium credentials in CI. The wrapper should
    silently downgrade to local mode on the first failure.
    """

    jsonl_path: Path = _clean_env

    monkeypatch.setenv("OMIUM_API_KEY", "om_" + "x" * 30)
    # Point at an unreachable host so we don't actually hit Omium in CI.
    monkeypatch.setenv("OMIUM_API_BASE", "http://127.0.0.1:1")
    monkeypatch.setenv("OMIUM_API_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TRACE_BACKEND", "auto")
    # Skip the optional background workflow registration (it does a network
    # call that can be slow / noisy in CI).
    monkeypatch.setenv("OMIUM_SKIP_WORKFLOW_REGISTER", "true")

    tracer = tracing_module.Tracer(service_name="test")

    # Regression guard: when OMIUM_API_KEY is set (even with a fake host),
    # the SDK should initialize and ``_omium_tracer`` must be wired up.
    # If this assertion fails, the wrapper is silently falling back to local
    # mode and spans will never ship to Omium — exactly the bug we just fixed.
    assert tracer._omium_tracer is not None, (
        "Tracer._omium_tracer is None despite OMIUM_API_KEY being set. "
        "The wrapper has silently fallen back to local mode — check that "
        "_ensure_omium_initialized() ran successfully and OmiumTracer was "
        "instantiated without raising."
    )
    assert tracer.backend_used == "omium"

    handle = tracer.start_workflow("hello_world", metadata={})

    # The whole sequence must complete without exceptions even though the
    # configured api_base_url is unreachable.
    with handle.span("only_step") as span:
        span.event("ping", {"hello": "world"})

    handle.finish(status="success")
    tracer.shutdown()

    # JSONL is always written, regardless of whether Omium succeeded.
    records = _read_jsonl(jsonl_path)
    assert any(r["kind"] == "workflow_start" for r in records)
    assert any(r["kind"] == "workflow_end" for r in records)
    assert any(r["kind"] == "span_start" and r["name"] == "only_step" for r in records)
    assert any(r["kind"] == "span_end" and r["name"] == "only_step" for r in records)


# ---------------------------------------------------------------------------
# Case 3 — graceful degradation when Omium endpoint is bad
# ---------------------------------------------------------------------------


def test_graceful_degradation(_clean_env, monkeypatch):
    jsonl_path: Path = _clean_env

    monkeypatch.setenv("OMIUM_API_KEY", "om_" + "y" * 30)
    monkeypatch.setenv("OMIUM_API_BASE", "http://localhost:1")
    monkeypatch.setenv("OMIUM_API_URL", "http://localhost:1")
    monkeypatch.setenv("TRACE_BACKEND", "auto")
    monkeypatch.setenv("OMIUM_SKIP_WORKFLOW_REGISTER", "true")

    tracer = tracing_module.Tracer(service_name="test")
    handle = tracer.start_workflow("bad_endpoint_smoke", metadata={"x": 1})

    with handle.span("step_a") as step:
        step.event("touched", {"ok": True})
        with step.child_span("step_b"):
            pass

    handle.finish(status="success")

    records = _read_jsonl(jsonl_path)
    # We still have a complete local trace, ergo we did not raise.
    assert records[0]["kind"] == "workflow_start"
    assert records[-1]["kind"] == "workflow_end"
    # And causality is intact.
    starts = [r for r in records if r["kind"] == "span_start"]
    assert starts[0]["parent_id"] == handle.workflow_id
    assert starts[1]["parent_id"] == starts[0]["span_id"]


# ---------------------------------------------------------------------------
# Bonus: the singleton accessor honours the configured backend
# ---------------------------------------------------------------------------


def test_get_tracer_singleton(_clean_env, monkeypatch):
    monkeypatch.setenv("TRACE_BACKEND", "local")
    t1 = tracing_module.get_tracer()
    t2 = tracing_module.get_tracer()
    assert t1 is t2
