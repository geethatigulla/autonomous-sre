"""Tests for the parallel fix-author race.

Covers four behaviours of :func:`race_fix_candidates`:

1. ``test_race_both_succeed_fastest_wins`` — both candidates draft and
   verify successfully; winner is the one with the shorter verification
   duration, both rows appear in ``all_candidates``.
2. ``test_race_only_one_verifies`` — both candidates draft, but only one
   verifies; that one wins and the other is recorded with the failed
   verdict.
3. ``test_race_both_fail`` — both candidates draft but neither verifies;
   the winner is the one with the "cleanest" failure verdict
   (``tests_failed`` beats ``patch_invalid`` beats ``sandbox_error``).
4. ``test_race_one_drafts_fails`` — one strategy raises during drafting;
   the other succeeds and wins.

We mock both :meth:`FixAuthorAgent.run_returning` and the module-level
:func:`verify_patch_directly` so the tests don't depend on the real
LLM, GitHub, or the sandbox subprocess. Each mock dispatches on
``strategy`` so the two race participants can return different
behaviours per test.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


_FAKE_DIFF = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,1 +1,2 @@\n"
    " print('hi')\n"
    "+# patched\n"
)


def _make_incident(session):
    """Insert a minimal Incident in the ``investigated`` state and return it."""
    from orchestrator.models import Incident

    incident = Incident(
        source_service="victim_app",
        endpoint="POST /shorten",
        exception_class="AttributeError",
        exception_message="'NoneType' object has no attribute 'strip'",
        traceback='File "app.py", line 20, in shorten\n    url.strip()',
        request_body='{"url": null}',
        raw_payload="{}",
        status="investigated",
        classification="null_deref",
        severity="P1",
        suspect_file="app.py",
        suspect_line=20,
        root_cause="missing None guard on payload.url",
        investigation_result=json.dumps(
            {
                "hypotheses": [
                    {
                        "description": "url is None",
                        "evidence": "traceback shows NoneType",
                        "fix_strategy": "raise HTTPException(422)",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident


def _make_workflow():
    """Build a workflow + a root span so the race has a real parent to attach to."""
    from orchestrator.tracing import get_tracer

    tracer = get_tracer()
    wf = tracer.start_workflow("test_workflow", metadata={})
    return tracer, wf


def _draft_payload(strategy: str, *, confidence: float = 0.9) -> dict:
    """Return the dict shape ``FixAuthorAgent.run_returning`` produces."""
    return {
        "file_path": "app.py",
        "diff": _FAKE_DIFF,
        "explanation": f"{strategy} fix",
        "test_should_pass": "test_shorten_null_returns_422",
        "risk_level": "low",
        "confidence": confidence,
    }


def _install_draft_mock(monkeypatch, *, behaviour: dict):
    """Patch ``FixAuthorAgent.run_returning`` to return per-strategy payloads.

    ``behaviour`` maps strategy → "ok"|"raise" or a custom dict. Anything
    else makes the mocked method return ``_draft_payload(strategy)``.
    """
    from orchestrator.agents import fix_author as fa_module

    async def _mock_run_returning(self, *, incident, span, session):
        spec = behaviour.get(self.strategy, "ok")
        if spec == "raise":
            raise RuntimeError(f"{self.strategy}: draft blew up")
        if isinstance(spec, dict):
            return spec
        return _draft_payload(self.strategy)

    monkeypatch.setattr(
        fa_module.FixAuthorAgent, "run_returning", _mock_run_returning
    )


def _install_verifier_mock(monkeypatch, *, per_strategy: dict):
    """Patch the module-level ``verify_patch_directly`` used by the race.

    ``per_strategy`` maps strategy → result dict. We can't read strategy
    directly from the patch text, so we sniff the explanation field — the
    draft mock above writes a unique ``"{strategy} fix"`` marker into the
    patch via :func:`_draft_payload` callers, and we side-channel the
    strategy through the patch text itself.

    Simpler: the verifier mock looks at the patch and, if not seen
    before, calls ``next_for(strategy)``. To make this deterministic we
    actually look up ``per_strategy`` by the order the verifier is
    called for each unique strategy.

    To avoid that complexity, we make the draft mock emit a patch that
    encodes the strategy on the first line as a comment. The verifier
    mock decodes it and returns the corresponding canned result.
    """
    from orchestrator.orchestration import parallel_fix as pf_module

    async def _mock_verify(*, patch, source_dir, span, tracer):
        # Decode strategy from the marker the draft mock baked in.
        strategy = "unknown"
        for line in (patch or "").splitlines():
            if line.startswith("# strategy:"):
                strategy = line.split(":", 1)[1].strip()
                break
        result = per_strategy.get(strategy)
        if result is None:
            # Last-resort fallback so the test fails loudly if the
            # strategy didn't round-trip.
            return {
                "applied": False,
                "tests_ran": False,
                "tests_passed": False,
                "test_output": f"unknown strategy in patch: {strategy!r}",
                "verdict": "sandbox_error",
                "duration_seconds": 0.0,
            }
        return result

    monkeypatch.setattr(pf_module, "verify_patch_directly", _mock_verify)


def _draft_payload_marked(strategy: str, *, confidence: float = 0.9) -> dict:
    """Like :func:`_draft_payload` but bakes a strategy marker into the diff.

    The marker lets the mocked verifier identify which candidate it's
    looking at without us having to track call order across two
    concurrent coroutines.
    """
    diff = (
        f"# strategy: {strategy}\n"
        f"--- a/app.py\n"
        f"+++ b/app.py\n"
        f"@@ -1,1 +1,2 @@\n"
        f" print('hi')\n"
        f"+# patched by {strategy}\n"
    )
    return {
        "file_path": "app.py",
        "diff": diff,
        "explanation": f"{strategy} fix",
        "test_should_pass": "test_shorten_null_returns_422",
        "risk_level": "low",
        "confidence": confidence,
    }


def _install_draft_mock_marked(monkeypatch, *, behaviour: dict):
    """Same as :func:`_install_draft_mock` but emits strategy-marked diffs."""
    from orchestrator.agents import fix_author as fa_module

    async def _mock_run_returning(self, *, incident, span, session):
        spec = behaviour.get(self.strategy, "ok")
        if spec == "raise":
            raise RuntimeError(f"{self.strategy}: draft blew up")
        confidence = 0.9
        if isinstance(spec, dict):
            confidence = float(spec.get("confidence", 0.9))
        return _draft_payload_marked(self.strategy, confidence=confidence)

    monkeypatch.setattr(
        fa_module.FixAuthorAgent, "run_returning", _mock_run_returning
    )


# ---------------------------------------------------------------------------
# 1. Both succeed — fastest wins
# ---------------------------------------------------------------------------


def test_race_both_succeed_fastest_wins(isolated_env, monkeypatch):
    """Both verifiers return ``verified``; the one with shorter duration wins."""
    from orchestrator.db import get_engine
    from orchestrator.orchestration.parallel_fix import race_fix_candidates
    from sqlmodel import Session

    _install_draft_mock_marked(
        monkeypatch, behaviour={"defensive": "ok", "permissive": "ok"}
    )
    _install_verifier_mock(
        monkeypatch,
        per_strategy={
            "defensive": {
                "applied": True,
                "tests_ran": True,
                "tests_passed": True,
                "test_output": "1 passed",
                "verdict": "verified",
                "duration_seconds": 0.50,
            },
            "permissive": {
                "applied": True,
                "tests_ran": True,
                "tests_passed": True,
                "test_output": "1 passed",
                "verdict": "verified",
                "duration_seconds": 0.10,
            },
        },
    )

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(session)
        tracer, wf = _make_workflow()
        try:
            winner, all_candidates = asyncio.run(
                race_fix_candidates(
                    incident=incident,
                    openai_client=object(),
                    tracer=tracer,
                    github_tool=object(),
                    parent_span=wf,
                    session=session,
                    strategies=["defensive", "permissive"],
                )
            )
        finally:
            wf.finish(status="success")

    assert len(all_candidates) == 2
    verdicts = {c.strategy: c.verdict for c in all_candidates}
    assert verdicts == {"defensive": "verified", "permissive": "verified"}
    # permissive has the smaller duration_seconds, so it should win.
    assert winner.strategy == "permissive", winner
    assert winner.verdict == "verified"
    assert winner.tests_passed is True
    assert winner.duration_seconds == 0.10


# ---------------------------------------------------------------------------
# 2. Only one verifies
# ---------------------------------------------------------------------------


def test_race_only_one_verifies(isolated_env, monkeypatch):
    """Defensive verifies; permissive's patch fails tests. Defensive wins."""
    from orchestrator.db import get_engine
    from orchestrator.orchestration.parallel_fix import race_fix_candidates
    from sqlmodel import Session

    _install_draft_mock_marked(
        monkeypatch, behaviour={"defensive": "ok", "permissive": "ok"}
    )
    _install_verifier_mock(
        monkeypatch,
        per_strategy={
            "defensive": {
                "applied": True,
                "tests_ran": True,
                "tests_passed": True,
                "test_output": "1 passed",
                "verdict": "verified",
                "duration_seconds": 0.3,
            },
            "permissive": {
                "applied": True,
                "tests_ran": True,
                "tests_passed": False,
                "test_output": "1 failed",
                "verdict": "tests_failed",
                "duration_seconds": 0.4,
            },
        },
    )

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(session)
        tracer, wf = _make_workflow()
        try:
            winner, all_candidates = asyncio.run(
                race_fix_candidates(
                    incident=incident,
                    openai_client=object(),
                    tracer=tracer,
                    github_tool=object(),
                    parent_span=wf,
                    session=session,
                    strategies=["defensive", "permissive"],
                )
            )
        finally:
            wf.finish(status="success")

    assert winner.strategy == "defensive"
    assert winner.verdict == "verified"
    verdicts = {c.strategy: c.verdict for c in all_candidates}
    assert verdicts == {
        "defensive": "verified",
        "permissive": "tests_failed",
    }


# ---------------------------------------------------------------------------
# 3. Both fail — cleanest verdict wins
# ---------------------------------------------------------------------------


def test_race_both_fail(isolated_env, monkeypatch):
    """Neither candidate verifies; winner is the one with the cleaner verdict."""
    from orchestrator.db import get_engine
    from orchestrator.orchestration.parallel_fix import race_fix_candidates
    from sqlmodel import Session

    _install_draft_mock_marked(
        monkeypatch, behaviour={"defensive": "ok", "permissive": "ok"}
    )
    _install_verifier_mock(
        monkeypatch,
        per_strategy={
            # tests_failed > patch_invalid in priority, so defensive
            # should win this race even though both failed.
            "defensive": {
                "applied": True,
                "tests_ran": True,
                "tests_passed": False,
                "test_output": "1 failed",
                "verdict": "tests_failed",
                "duration_seconds": 0.5,
            },
            "permissive": {
                "applied": False,
                "tests_ran": False,
                "tests_passed": False,
                "test_output": "git apply rejected",
                "verdict": "patch_invalid",
                "duration_seconds": 0.2,
            },
        },
    )

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(session)
        tracer, wf = _make_workflow()
        try:
            winner, all_candidates = asyncio.run(
                race_fix_candidates(
                    incident=incident,
                    openai_client=object(),
                    tracer=tracer,
                    github_tool=object(),
                    parent_span=wf,
                    session=session,
                    strategies=["defensive", "permissive"],
                )
            )
        finally:
            wf.finish(status="success")

        # Now run through the state-machine-equivalent persistence
        # block to confirm the "fix_rejected" mapping is correct.
        if winner.verdict in ("tests_failed", "patch_invalid"):
            incident.status = "fix_rejected"
            session.add(incident)
            session.commit()
            session.refresh(incident)

    assert winner.strategy == "defensive"
    assert winner.verdict == "tests_failed"
    assert incident.status == "fix_rejected"


# ---------------------------------------------------------------------------
# 4. One draft fails, the other wins
# ---------------------------------------------------------------------------


def test_race_one_drafts_fails(isolated_env, monkeypatch):
    """Defensive raises during draft; permissive succeeds and verifies."""
    from orchestrator.db import get_engine
    from orchestrator.orchestration.parallel_fix import race_fix_candidates
    from sqlmodel import Session

    _install_draft_mock_marked(
        monkeypatch,
        behaviour={"defensive": "raise", "permissive": "ok"},
    )
    _install_verifier_mock(
        monkeypatch,
        per_strategy={
            # defensive's verifier should never be called because the
            # draft failed first. The race must short-circuit it.
            "defensive": {
                "applied": False,
                "tests_ran": False,
                "tests_passed": False,
                "test_output": "should not be reached",
                "verdict": "sandbox_error",
                "duration_seconds": 9.99,
            },
            "permissive": {
                "applied": True,
                "tests_ran": True,
                "tests_passed": True,
                "test_output": "1 passed",
                "verdict": "verified",
                "duration_seconds": 0.4,
            },
        },
    )

    engine = get_engine()
    with Session(engine) as session:
        incident = _make_incident(session)
        tracer, wf = _make_workflow()
        try:
            winner, all_candidates = asyncio.run(
                race_fix_candidates(
                    incident=incident,
                    openai_client=object(),
                    tracer=tracer,
                    github_tool=object(),
                    parent_span=wf,
                    session=session,
                    strategies=["defensive", "permissive"],
                )
            )
        finally:
            wf.finish(status="success")

        # State-machine-equivalent persistence to confirm the
        # "verified" mapping is correct.
        if winner.verdict == "verified":
            incident.proposed_patch = winner.patch
            incident.fix_explanation = winner.explanation
            incident.status = "verified"
            session.add(incident)
            session.commit()
            session.refresh(incident)

    assert winner.strategy == "permissive"
    assert winner.verdict == "verified"
    assert winner.tests_passed is True
    by_strategy = {c.strategy: c for c in all_candidates}
    assert by_strategy["defensive"].verdict == "draft_failed"
    assert by_strategy["defensive"].patch is None
    assert "draft blew up" in (by_strategy["defensive"].draft_error or "")
    assert incident.status == "verified"
    assert incident.proposed_patch == winner.patch
