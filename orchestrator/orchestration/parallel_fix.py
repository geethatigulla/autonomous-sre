"""Parallel hypothesis racing for the null_deref pipeline.

Two ``FixAuthorAgent`` instances with different "fix philosophies"
(defensive vs. permissive) draft patches concurrently, then each
candidate is verified in its own sandbox — also concurrently. The first
verdict ``"verified"`` wins and that patch becomes the canonical
``incident.proposed_patch``; the loser is logged for visibility but
discarded.

Why race?
---------
* Diversity: two genuinely different programs (raise-vs.-fallback) gives
  the Verifier two real chances at green tests instead of one.
* Latency: total wall time is ~max(draft1, draft2) + max(verify1,
  verify2), not the sum.
* Visibility: every span and AgentRun is strategy-suffixed so the trace
  tree shows both contenders side-by-side and the dashboard can render
  the race.

Public API
----------
* :class:`FixCandidate` — per-strategy result envelope.
* :func:`race_fix_candidates` — runs the race, returns
  ``(winner, all_candidates)``.

The race never raises out of the public surface — every failure mode
ends up as a non-``verified`` ``verdict`` on a :class:`FixCandidate`.
Picking the winner is total: if no candidate verified, the one with
the cleanest failure (per the priority order in
:data:`_VERDICT_PRIORITY`) is returned so the state machine can still
transition the incident into a well-defined terminal state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from orchestrator.agents.fix_author import FixAuthorAgent
from orchestrator.agents.verifier import verify_patch_directly
from orchestrator.dashboard.events import safe_publish
from orchestrator.models import AgentRun
from orchestrator.timeutil import utcnow


if TYPE_CHECKING:  # pragma: no cover
    from sqlmodel import Session

    from orchestrator.models import Incident
    from orchestrator.tools.github_tool import GitHubTool


log = logging.getLogger("orchestrator.orchestration.parallel_fix")


# Ordering used when no candidate verified — we return whichever one
# failed "more cleanly" so the state machine can pick the most accurate
# downstream status. ``verified`` is in here only so the sort is total;
# in practice when the race lost, no candidate has that verdict.
_VERDICT_PRIORITY: dict[str, int] = {
    "verified": 0,
    "tests_failed": 1,
    "patch_invalid": 2,
    "sandbox_error": 3,
    "draft_failed": 4,
}


# ---------------------------------------------------------------------------
# FixCandidate dataclass
# ---------------------------------------------------------------------------


@dataclass
class FixCandidate:
    """One participant in the fix race — draft outcome + verify outcome."""

    strategy: str
    patch: Optional[str] = None
    explanation: Optional[str] = None
    confidence: float = 0.0
    risk_level: str = "unknown"
    draft_error: Optional[str] = None
    verdict: Optional[str] = None
    # verdict ∈ {"verified","tests_failed","patch_invalid","sandbox_error","draft_failed", None}
    tests_passed: Optional[bool] = None
    duration_seconds: Optional[float] = None
    verifier_output: Optional[str] = None
    # extras
    file_path: Optional[str] = None
    test_should_pass: Optional[str] = None
    # Trace correlation — recorded so the dashboard can deep-link from
    # the race summary into the per-strategy spans.
    draft_span_id: Optional[str] = None
    verify_span_id: Optional[str] = None
    # Free-form per-candidate timestamps; useful when tests assert
    # concurrent execution (start of B < end of A).
    timings: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _open_child_span(parent: Any, name: str, metadata: Optional[dict] = None):
    """Open a child span on either a WorkflowHandle or a SpanContext.

    The tracing module has two parent types: ``WorkflowHandle.span(...)``
    and ``SpanContext.child_span(...)``. They're semantically the same
    but spelled differently. The race orchestrator is called from the
    state machine with a workflow handle as parent, but unit tests can
    pass either — so we sniff for the API and dispatch.
    """
    if hasattr(parent, "child_span"):
        return parent.child_span(name, metadata=metadata)
    return parent.span(name, metadata=metadata)


def _commit(session: "Session", *rows: Any) -> None:
    """Add + commit a batch of rows on the shared session.

    Tiny helper because the race writes AgentRun rows from inside two
    asyncio.gather coroutines; centralizing the pattern makes it easy to
    spot all the persistence points in one place.
    """
    for r in rows:
        session.add(r)
    session.commit()


# ---------------------------------------------------------------------------
# Public surface — race_fix_candidates
# ---------------------------------------------------------------------------


async def race_fix_candidates(
    *,
    incident: "Incident",
    openai_client: Any,
    tracer: Any,
    github_tool: "GitHubTool",
    parent_span: Any,
    session: "Session",
    strategies: Optional[list[str]] = None,
    verifier_source_dir: str = "victim_app",
) -> tuple[FixCandidate, list[FixCandidate]]:
    """Run the fix race and return ``(winner, all_candidates)``.

    Phase 1 fans out N :class:`FixAuthorAgent` instances (one per
    strategy) under ``parent_span`` → ``fix_race`` → ``draft.<strategy>``
    spans. They run concurrently via :func:`asyncio.gather`. Each writes
    one ``AgentRun`` row named ``fix_author_<strategy>``.

    Phase 2 fans out N verifiers — only for candidates whose draft
    succeeded — under ``verify.<strategy>`` spans. Each gets its own
    tempdir sandbox via :func:`verify_patch_directly`. Each writes one
    ``AgentRun`` row named ``verifier_<strategy>``.

    Phase 3 picks the winner. Preferred ordering: ``verified`` > all
    other verdicts. Among verified candidates, fastest verification wins
    (ties broken by higher confidence). If no candidate verified, we
    return the one whose verdict is highest in
    :data:`_VERDICT_PRIORITY` so the caller still has something concrete
    to act on.
    """
    strategies = strategies or ["defensive", "permissive"]
    if not strategies:
        raise ValueError("race_fix_candidates: strategies must not be empty")

    with _open_child_span(
        parent_span,
        "fix_race",
        metadata={"strategies": strategies, "incident_id": incident.id},
    ) as race_span:
        race_span.event(
            "drafting_started",
            {"n_candidates": len(strategies)},
        )

        # Surface "race kicked off" to the dashboard so it can expand
        # the FIX RACE row and pre-populate one child per strategy.
        safe_publish(
            "race_started",
            {
                "incident_id": incident.id,
                "strategies": list(strategies),
            },
        )

        # ------------------------------------------------------------------
        # PHASE 1 — draft all candidates concurrently
        # ------------------------------------------------------------------

        async def draft_one(strategy: str) -> FixCandidate:
            candidate = FixCandidate(strategy=strategy)
            t0 = asyncio.get_event_loop().time()
            with _open_child_span(
                race_span,
                f"draft.{strategy}",
                metadata={"strategy": strategy},
            ) as draft_span:
                candidate.draft_span_id = draft_span.span_id
                run = AgentRun(
                    incident_id=incident.id,
                    agent_name=f"fix_author_{strategy}",
                    span_id=draft_span.span_id,
                    input_summary=(
                        f"strategy={strategy} "
                        f"{incident.exception_class} @ "
                        f"{incident.suspect_file or '?'}:"
                        f"{incident.suspect_line or '?'}"
                    )[:1000],
                )
                _commit(session, run)
                session.refresh(run)

                try:
                    agent = FixAuthorAgent(
                        openai_client,
                        tracer,
                        github_tool=github_tool,
                        strategy=strategy,
                    )
                    result = await agent.run_returning(
                        incident=incident,
                        span=draft_span,
                        session=session,
                    )
                    candidate.patch = result.get("diff")
                    candidate.explanation = result.get("explanation") or ""
                    candidate.confidence = float(
                        result.get("confidence", 0.0) or 0.0
                    )
                    candidate.risk_level = result.get("risk_level", "unknown")
                    candidate.file_path = result.get("file_path")
                    candidate.test_should_pass = result.get("test_should_pass")
                    run.status = "success"
                    run.output_summary = json.dumps(
                        {
                            "strategy": strategy,
                            "confidence": candidate.confidence,
                            "risk": candidate.risk_level,
                            "diff_chars": len(candidate.patch or ""),
                        }
                    )[:1000]
                    run.finished_at = utcnow()
                except Exception as exc:  # noqa: BLE001 — every draft failure is captured
                    candidate.draft_error = (
                        f"{type(exc).__name__}: {exc}"
                    )[:1500]
                    candidate.verdict = "draft_failed"
                    run.status = "error"
                    run.error_message = candidate.draft_error[:1500]
                    run.finished_at = utcnow()
                    draft_span.event(
                        "draft_failed",
                        {
                            "strategy": strategy,
                            "error": candidate.draft_error[:500],
                        },
                    )
                _commit(session, run)
            candidate.timings["draft_start"] = t0
            candidate.timings["draft_end"] = asyncio.get_event_loop().time()

            # Per-candidate draft outcome for the dashboard. ``success``
            # is True iff we actually got a patch we can verify.
            safe_publish(
                "race_candidate_drafted",
                {
                    "incident_id": incident.id,
                    "strategy": strategy,
                    "success": bool(candidate.patch),
                    "confidence": candidate.confidence,
                    "risk_level": candidate.risk_level,
                    "error": candidate.draft_error,
                },
            )
            return candidate

        drafts: list[FixCandidate] = await asyncio.gather(
            *[draft_one(s) for s in strategies]
        )
        race_span.event(
            "drafting_complete",
            {
                "successes": sum(1 for d in drafts if d.patch),
                "failures": sum(1 for d in drafts if not d.patch),
            },
        )

        # ------------------------------------------------------------------
        # PHASE 2 — verify successful drafts concurrently
        # ------------------------------------------------------------------

        async def verify_one(candidate: FixCandidate) -> FixCandidate:
            if not candidate.patch:
                # Draft already failed — no sandbox to spin up.
                return candidate
            t0 = asyncio.get_event_loop().time()
            with _open_child_span(
                race_span,
                f"verify.{candidate.strategy}",
                metadata={"strategy": candidate.strategy},
            ) as ver_span:
                candidate.verify_span_id = ver_span.span_id
                run = AgentRun(
                    incident_id=incident.id,
                    agent_name=f"verifier_{candidate.strategy}",
                    span_id=ver_span.span_id,
                    input_summary=(
                        f"strategy={candidate.strategy} "
                        f"patch={len(candidate.patch or '')}c "
                        f"risk={candidate.risk_level} "
                        f"confidence={candidate.confidence:.2f}"
                    )[:1000],
                )
                _commit(session, run)
                session.refresh(run)

                try:
                    ver_result = await verify_patch_directly(
                        patch=candidate.patch,
                        source_dir=verifier_source_dir,
                        span=ver_span,
                        tracer=tracer,
                    )
                    candidate.verdict = ver_result.get("verdict")
                    candidate.tests_passed = ver_result.get("tests_passed")
                    candidate.duration_seconds = ver_result.get(
                        "duration_seconds"
                    )
                    candidate.verifier_output = (
                        ver_result.get("test_output") or ""
                    )[:1500]
                    run.status = (
                        "success"
                        if candidate.verdict == "verified"
                        else "error"
                    )
                    run.output_summary = json.dumps(
                        {
                            "strategy": candidate.strategy,
                            "verdict": candidate.verdict,
                            "tests_passed": candidate.tests_passed,
                            "duration_seconds": candidate.duration_seconds,
                        }
                    )[:1000]
                    run.error_message = (
                        None
                        if candidate.verdict == "verified"
                        else (candidate.verifier_output or "")[:1500]
                    )
                    run.finished_at = utcnow()
                except Exception as exc:  # noqa: BLE001 — never let verifier crashes escape
                    candidate.verdict = "sandbox_error"
                    candidate.verifier_output = (
                        f"{type(exc).__name__}: {exc}"
                    )[:1500]
                    run.status = "error"
                    run.error_message = candidate.verifier_output
                    run.finished_at = utcnow()
                _commit(session, run)
            candidate.timings["verify_start"] = t0
            candidate.timings["verify_end"] = asyncio.get_event_loop().time()
            safe_publish(
                "race_candidate_verified",
                {
                    "incident_id": incident.id,
                    "strategy": candidate.strategy,
                    "verdict": candidate.verdict,
                    "tests_passed": candidate.tests_passed,
                    "duration_s": candidate.duration_seconds,
                },
            )
            return candidate

        verified: list[FixCandidate] = await asyncio.gather(
            *[verify_one(d) for d in drafts]
        )

        # ------------------------------------------------------------------
        # PHASE 3 — pick the winner
        # ------------------------------------------------------------------

        winners = [c for c in verified if c.verdict == "verified"]
        if winners:
            # Fastest verification first; ties broken by higher confidence.
            winners.sort(
                key=lambda c: (c.duration_seconds or 9999.0, -c.confidence)
            )
            winner = winners[0]
            race_span.event(
                "race_won",
                {
                    "strategy": winner.strategy,
                    "duration_s": winner.duration_seconds,
                    "confidence": winner.confidence,
                    "alternatives_tried": len(verified),
                },
            )
        else:
            # No verified candidate. Pick the cleanest failure.
            verified.sort(
                key=lambda c: _VERDICT_PRIORITY.get(
                    c.verdict or "draft_failed", 99
                )
            )
            winner = verified[0]
            race_span.event(
                "race_lost",
                {
                    "best_verdict": winner.verdict,
                    "alternatives_tried": len(verified),
                },
            )

        # Publish race_won regardless of outcome — the dashboard
        # interprets ``verdict`` to know whether to render the winner as
        # a champion or as the least-bad loser. ``alternatives`` carries
        # the other strategies' verdicts for the UI to render side-by-side.
        safe_publish(
            "race_won",
            {
                "incident_id": incident.id,
                "winning_strategy": winner.strategy,
                "verdict": winner.verdict,
                "alternatives": [
                    {
                        "strategy": c.strategy,
                        "verdict": c.verdict,
                        "tests_passed": c.tests_passed,
                        "duration_s": c.duration_seconds,
                    }
                    for c in verified
                    if c.strategy != winner.strategy
                ],
            },
        )
        return winner, verified


__all__ = ["FixCandidate", "race_fix_candidates"]
