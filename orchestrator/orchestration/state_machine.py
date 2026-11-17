"""Planner / router that walks an incident through triage -> investigate -> fix -> verify -> release.

This module owns the top-level workflow for one incident. The webhook
ingress schedules :func:`handle_incident` as a background task; from
there the state machine:

1. Opens a tracer workflow named ``incident_response`` and binds its
   ``workflow_id`` to the persisted :class:`Incident` row.
2. Runs the Triager under a child span ``triager.classify``,
   recording an :class:`AgentRun` row before/after.
3. Routes based on the Triager's classification:

   * ``unknown`` → ``escalator.notify`` span; mark incident
     ``status="escalated"``.
   * any other class → ``pipeline.<classification>.stub`` span; mark
     incident ``status="pending_implementation"`` until the
     Investigator / FixAuthor / Verifier / Releaser pipeline lands.

The Investigator / FixAuthor / Verifier / Releaser branches are stubs in
this prompt — by design. They will be wired in subsequent prompts.

Tracing rules:

* Every meaningful step lives inside ``tracer.start_workflow`` or
  ``wf.span`` / ``span.event``. Tracing failures must never crash the
  request — the tracing wrapper already swallows them.
* Every agent invocation is bracketed by an :class:`AgentRun` insert
  (status="running") and an update (status="success" | "error") so the
  dashboard always knows what's in flight.
"""

from __future__ import annotations

import json
import logging
import traceback as tb_module

from openai import AsyncOpenAI
from sqlmodel import Session

from orchestrator.agents.investigator import InvestigatorAgent
from orchestrator.agents.releaser import ReleaserAgent
from orchestrator.agents.triager import TriagerAgent
from orchestrator.config import get_settings
from orchestrator.dashboard.events import safe_publish
from orchestrator.db import get_engine
from orchestrator.models import AgentRun, Incident
from orchestrator.orchestration.parallel_fix import race_fix_candidates
from orchestrator.timeutil import utcnow
from orchestrator.tools.github_tool import GitHubTool
from orchestrator.tools.search_tool import WebSearchTool
from orchestrator.tracing import get_tracer


# Strategies entered into the fix race. Defensive = fail-fast / raise;
# permissive = graceful fallback. Two genuinely different programs gives
# the Verifier two real chances at green tests instead of one.
FIX_STRATEGIES: list[str] = ["defensive", "permissive"]

# Source tree the verifier sandboxes copy from. Module-level so tests
# can monkeypatch it (the end-to-end fixture seeds a fake victim_app/
# under tmp_path and points us at it).
VERIFIER_SOURCE_DIR: str = "victim_app"


log = logging.getLogger("orchestrator.state_machine")


def _set_status(incident: Incident, new_status: str) -> None:
    """Mutate ``incident.status`` and emit a dashboard ``status_changed``.

    Centralizing the transition makes it impossible to bump status
    without also notifying the dashboard. Callers still commit() the
    incident themselves — this helper only touches the in-memory row
    and publishes the event.
    """
    old = incident.status
    if old == new_status:
        return
    incident.status = new_status
    incident.updated_at = utcnow()
    safe_publish(
        "status_changed",
        {
            "incident_id": incident.id,
            "old_status": old,
            "new_status": new_status,
        },
    )


def _publish_agent_started(
    incident_id: str, agent_name: str, span_id: str | None = None
) -> None:
    """Tell the dashboard an agent span has just opened."""
    safe_publish(
        "agent_started",
        {
            "incident_id": incident_id,
            "agent_name": agent_name,
            "span_id": span_id,
            "started_at": utcnow().isoformat(),
        },
    )


def _publish_agent_finished(
    incident_id: str,
    agent_name: str,
    status: str,
    *,
    summary: str | None = None,
    duration_s: float | None = None,
) -> None:
    """Tell the dashboard an agent span has just closed (success or error)."""
    safe_publish(
        "agent_finished",
        {
            "incident_id": incident_id,
            "agent_name": agent_name,
            "status": status,
            "summary": (summary or "")[:500],
            "duration_s": duration_s,
            "finished_at": utcnow().isoformat(),
        },
    )


def _run_duration(run: AgentRun) -> float | None:
    """Seconds between ``started_at`` and ``finished_at``, if both are set."""
    if run.started_at and run.finished_at:
        return (run.finished_at - run.started_at).total_seconds()
    return None


def _build_openai_client() -> AsyncOpenAI:
    """Construct the shared async OpenAI client.

    Kept in its own helper so tests can monkeypatch it to inject a mock
    client (e.g. via ``unittest.mock.patch``).
    """
    settings = get_settings()
    return AsyncOpenAI(api_key=settings.openai_api_key)


async def handle_incident(incident_id: str) -> None:
    """Top-level entry point — called by the webhook background task.

    Always returns ``None``. Exceptions are caught, logged, and
    persisted to the corresponding AgentRun row; the incident is moved
    to ``status="failed"`` so the dashboard can flag it. We never let
    an exception escape this function because a thrown background task
    becomes a silent loss in FastAPI's BackgroundTasks runner.
    """
    tracer = get_tracer()
    workflow_t0 = utcnow()

    engine = get_engine()
    with Session(engine) as session:
        incident = session.get(Incident, incident_id)
        if incident is None:
            log.warning("handle_incident: incident %s not found", incident_id)
            return

        with tracer.start_workflow(
            name="incident_response",
            metadata={
                "incident_id": incident.id,
                "exception_class": incident.exception_class,
                "endpoint": incident.endpoint,
                "source_service": incident.source_service,
            },
        ) as wf:
            # Persist the workflow_id so /webhooks/alert/{id} can show
            # the trace correlation in the dashboard.
            incident.workflow_id = wf.workflow_id
            incident.updated_at = utcnow()
            session.add(incident)
            session.commit()
            session.refresh(incident)

            wf.event(
                "incident_received",
                {"incident_id": incident.id, "status": incident.status},
            )

            # Tell the dashboard the orchestration has actually begun.
            safe_publish(
                "workflow_started",
                {
                    "incident_id": incident.id,
                    "workflow_id": wf.workflow_id,
                },
            )

            # OpenAI client construction can itself raise (missing
            # OPENAI_API_KEY, bad config, etc.). Build it INSIDE the
            # workflow so the failure is recorded as a span event +
            # persisted on the incident, rather than the background
            # task dying silently with the incident stuck on "received".
            try:
                openai_client = _build_openai_client()
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "OpenAI client construction failed for incident %s: %s",
                    incident.id,
                    exc,
                )
                wf.event(
                    "openai_client_init_failed",
                    {"error": f"{type(exc).__name__}: {exc}"[:500]},
                )
                _set_status(incident, "failed")
                session.add(incident)
                session.commit()
                safe_publish(
                    "workflow_finished",
                    {
                        "incident_id": incident.id,
                        "final_status": incident.status,
                        "total_duration_s": (
                            utcnow() - workflow_t0
                        ).total_seconds(),
                    },
                )
                return

            # -----------------------------------------------------------
            # PHASE 1 — Triage
            # -----------------------------------------------------------
            try:
                await _run_triager(
                    incident=incident,
                    session=session,
                    wf=wf,
                    openai_client=openai_client,
                    tracer=tracer,
                )
            except Exception as exc:  # noqa: BLE001 — caught + persisted
                log.exception(
                    "Triager failed for incident %s: %s", incident.id, exc
                )
                # AgentRun already marked error inside _run_triager;
                # bump incident status and exit cleanly so the workflow
                # closes with status=error in the tracer.
                _set_status(incident, "failed")
                session.add(incident)
                session.commit()
                safe_publish(
                    "workflow_finished",
                    {
                        "incident_id": incident.id,
                        "final_status": incident.status,
                        "total_duration_s": (
                            utcnow() - workflow_t0
                        ).total_seconds(),
                    },
                )
                return

            # -----------------------------------------------------------
            # PHASE 2 — Route based on classification
            # -----------------------------------------------------------
            classification = incident.classification or "unknown"
            settings = get_settings()
            github = GitHubTool(
                settings.github_token,
                settings.github_repo,
                tracer,
            )
            if classification in (
                "null_deref",
                "recent_regression",
                "config_error",
            ):
                await run_remediation_pipeline(
                    classification=classification,
                    incident=incident,
                    wf=wf,
                    session=session,
                    openai_client=openai_client,
                    tracer=tracer,
                    github=github,
                    settings=settings,
                )
            else:
                # ``unknown`` (or any unmapped class) — escalate without
                # running the remediation pipeline.
                with wf.span(
                    "escalator.notify",
                    metadata={
                        "reason": "unknown_classification",
                        "classification": classification,
                    },
                ) as esc_span:
                    esc_span.event(
                        "escalation_needed",
                        {"reason": "unknown_classification"},
                    )
                    # TODO(prompt 5+): when Releaser exists, call it
                    # with action='escalate' here.
                    _set_status(incident, "escalated")
                    session.add(incident)
                    session.commit()

            # ---------------------------------------------------------------
            # Workflow tail — publish a single ``workflow_finished`` so the
            # dashboard knows it can stop the global elapsed-time timer
            # and surface the final status badge.
            # ---------------------------------------------------------------
            safe_publish(
                "workflow_finished",
                {
                    "incident_id": incident.id,
                    "final_status": incident.status,
                    "total_duration_s": (
                        utcnow() - workflow_t0
                    ).total_seconds(),
                },
            )


async def run_remediation_pipeline(
    *,
    classification: str,
    incident: Incident,
    wf,
    session: Session,
    openai_client: AsyncOpenAI,
    tracer,
    github: "GitHubTool",
    settings,
) -> None:
    """Run the full Investigator → fix_race → Releaser pipeline.

    Bug-type-agnostic — works for ``null_deref``, ``recent_regression``,
    and ``config_error``. The ``classification`` arg is used only for
    tracing/labeling (span metadata) and so the Investigator / FixAuthor
    can specialize their prompts; the pipeline structure is identical
    across all three classes.

    All status transitions, :class:`AgentRun` bookkeeping, and dashboard
    event publishes are preserved exactly as in the original null_deref
    branch — no callers should observe a behavior change for the
    null_deref classification.
    """
    # -------- Investigator --------------------------------
    # Open a single ``investigator.investigate`` span; the agent opens
    # child spans for each tool call and the ``llm_call`` round-trips
    # itself. We attach ``classification`` to the span metadata so
    # traces are filterable per bug type without changing the span name.
    with wf.span(
        "investigator.investigate",
        metadata={"classification": classification},
    ) as inv_span:
        run = AgentRun(
            incident_id=incident.id,
            agent_name="investigator",
            span_id=inv_span.span_id,
            input_summary=(
                f"{incident.exception_class}: "
                f"{(incident.exception_message or '')[:200]}"
            ),
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        _publish_agent_started(
            incident.id, "investigator", inv_span.span_id
        )

        try:
            search = WebSearchTool(
                settings.tavily_api_key,
                tracer,
            )
            agent = InvestigatorAgent(
                openai_client,
                tracer,
                github_tool=github,
                search_tool=search,
            )
            result = await agent.run(
                incident=incident,
                span=inv_span,
                session=session,
            )
            run.status = "success"
            run.output_summary = json.dumps(result)[:2000]
            run.finished_at = utcnow()
            # ``incident.status`` already moved to ``"investigated"``
            # inside the agent; we keep the local row in sync defensively.
            # Use the publishing helper so the dashboard hears about
            # the transition.
            if incident.status != "investigated":
                _set_status(incident, "investigated")
            incident.updated_at = utcnow()
            session.add(run)
            session.add(incident)
            session.commit()
            _publish_agent_finished(
                incident.id,
                "investigator",
                "success",
                summary=(
                    f"suspect={incident.suspect_file or '?'}"
                    f":{incident.suspect_line or '?'}"
                ),
                duration_s=_run_duration(run),
            )
        except Exception as exc:  # noqa: BLE001 — caught + persisted
            run.status = "error"
            run.error_message = (
                f"{type(exc).__name__}: {exc}\n"
                f"{tb_module.format_exc()[:1500]}"
            )
            run.finished_at = utcnow()
            _set_status(incident, "investigation_failed")
            session.add(run)
            session.add(incident)
            session.commit()
            _publish_agent_finished(
                incident.id,
                "investigator",
                "error",
                summary=f"{type(exc).__name__}: {exc}",
                duration_s=_run_duration(run),
            )
            # Do NOT re-raise — we want the workflow to close cleanly
            # so the API surfaces the failure.

    # -------- Fix Race (Fix Authors + Verifiers) ---------
    # Replaces the old single-draft + single-verify pair. We fan out
    # N FixAuthorAgents (one per strategy in FIX_STRATEGIES)
    # concurrently, then fan out N Verifiers concurrently — each in
    # its own tempdir sandbox. The first verdict="verified" wins; the
    # loser is logged and discarded. The winning patch becomes
    # ``incident.proposed_patch`` so the Releaser block below stays
    # unchanged.
    if incident.status == "investigated":
        try:
            winner, all_candidates = await race_fix_candidates(
                incident=incident,
                openai_client=openai_client,
                tracer=tracer,
                github_tool=github,
                strategies=FIX_STRATEGIES,
                parent_span=wf,
                session=session,
                verifier_source_dir=VERIFIER_SOURCE_DIR,
            )

            # Persist the winning patch as the canonical one.
            # AgentRun rows for each strategy were already written
            # by the race; we only update the incident here.
            incident.proposed_patch = winner.patch
            incident.fix_explanation = winner.explanation
            incident.fix_confidence = winner.confidence
            incident.fix_risk_level = winner.risk_level
            incident.tests_passed = winner.tests_passed
            incident.verification_result = json.dumps(
                {
                    "winning_strategy": winner.strategy,
                    "verdict": winner.verdict,
                    "tests_passed": winner.tests_passed,
                    "duration_seconds": winner.duration_seconds,
                    "all_candidates": [
                        {
                            "strategy": c.strategy,
                            "verdict": c.verdict,
                            "tests_passed": c.tests_passed,
                            "duration_seconds": c.duration_seconds,
                            "confidence": c.confidence,
                            "had_patch": bool(c.patch),
                        }
                        for c in all_candidates
                    ],
                }
            )

            if winner.verdict == "verified":
                _set_status(incident, "verified")
            elif winner.verdict in (
                "tests_failed",
                "patch_invalid",
            ):
                _set_status(incident, "fix_rejected")
            elif winner.verdict == "draft_failed":
                _set_status(incident, "fix_failed")
            else:
                _set_status(incident, "verification_failed")

            session.add(incident)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            # Keep the workflow tidy even if the race code itself
            # blew up — don't re-raise.
            log.exception(
                "fix race crashed for incident %s",
                incident.id,
            )
            _set_status(incident, "fix_failed")
            incident.fix_explanation = (
                f"Race exception: {type(exc).__name__}: {exc}"
            )[:1500]
            session.add(incident)
            session.commit()

    # -------- Releaser -----------------------------------
    # Real Releaser: opens a GitHub Pull Request against the victim
    # repo and posts a Slack notification. Any failure here is
    # captured on the AgentRun row so the dashboard can surface the
    # underlying error (network, API quota, malformed diff, etc.)
    # without crashing the workflow.
    if incident.status == "verified":
        with wf.span(
            "releaser.release",
            metadata={"classification": classification},
        ) as rel_span:
            run = AgentRun(
                incident_id=incident.id,
                agent_name="releaser",
                span_id=rel_span.span_id,
                input_summary=(
                    f"verified patch={len(incident.proposed_patch or '')}c "
                    f"risk={incident.fix_risk_level or '?'} "
                    f"file={incident.suspect_file or '?'}"
                )[:1000],
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            _publish_agent_started(
                incident.id, "releaser", rel_span.span_id
            )

            try:
                agent = ReleaserAgent(
                    openai_client,
                    tracer,
                    github_tool=github,
                    slack_webhook_url=settings.slack_webhook_url,
                )
                rel_result = await agent.run(
                    incident=incident,
                    span=rel_span,
                    session=session,
                )
                run.status = "success"
                run.output_summary = json.dumps(
                    {
                        "pr_url": rel_result.get("pr_url"),
                        "pr_number": rel_result.get("pr_number"),
                        "pr_branch": rel_result.get("pr_branch"),
                        "slack_posted": rel_result.get(
                            "slack_posted", False
                        ),
                    }
                )[:1000]
                run.finished_at = utcnow()
                _set_status(incident, "released")
                session.add(run)
                session.add(incident)
                session.commit()

                # Surface PR + Slack outcomes as discrete events so
                # the dashboard can render the PR link as soon as
                # it's available.
                if rel_result.get("pr_url"):
                    safe_publish(
                        "pr_opened",
                        {
                            "incident_id": incident.id,
                            "pr_url": rel_result.get("pr_url"),
                            "pr_number": rel_result.get(
                                "pr_number"
                            ),
                        },
                    )
                safe_publish(
                    "slack_posted",
                    {
                        "incident_id": incident.id,
                        "success": bool(
                            rel_result.get("slack_posted")
                        ),
                    },
                )
                _publish_agent_finished(
                    incident.id,
                    "releaser",
                    "success",
                    summary=(
                        f"PR #{rel_result.get('pr_number', '?')}"
                    ),
                    duration_s=_run_duration(run),
                )
            except Exception as exc:  # noqa: BLE001
                run.status = "error"
                run.error_message = (
                    f"{type(exc).__name__}: {exc}\n"
                    f"{tb_module.format_exc()[:1500]}"
                )
                run.finished_at = utcnow()
                _set_status(incident, "release_failed")
                session.add(run)
                session.add(incident)
                session.commit()
                _publish_agent_finished(
                    incident.id,
                    "releaser",
                    "error",
                    summary=f"{type(exc).__name__}: {exc}",
                    duration_s=_run_duration(run),
                )


async def _run_triager(
    *,
    incident: Incident,
    session: Session,
    wf,
    openai_client: AsyncOpenAI,
    tracer,
) -> None:
    """Run the Triager and persist the corresponding AgentRun.

    Splitting this into a helper keeps :func:`handle_incident` readable
    and isolates the AgentRun bookkeeping. Re-raises on failure so the
    caller can move the incident to ``status="failed"`` and exit.
    """
    with wf.span("triager.classify") as triage_span:
        run = AgentRun(
            incident_id=incident.id,
            agent_name="triager",
            span_id=triage_span.span_id,
            input_summary=(
                f"{incident.exception_class}: "
                f"{incident.exception_message[:200]}"
            ),
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        _publish_agent_started(incident.id, "triager", triage_span.span_id)

        try:
            agent = TriagerAgent(openai_client, tracer)
            result = await agent.run(
                incident=incident,
                span=triage_span,
                session=session,
            )
            run.status = "success"
            run.output_summary = json.dumps(result)[:4000]
            run.finished_at = utcnow()
            session.add(run)
            session.commit()
            _publish_agent_finished(
                incident.id,
                "triager",
                "success",
                summary=(
                    f"{result.get('classification', '?')} "
                    f"({result.get('severity', '?')}) "
                    f"conf={result.get('confidence', 0):.2f}"
                ),
                duration_s=_run_duration(run),
            )
            # Triager mutates incident.classification/severity on the
            # row directly; surface that to the dashboard so the
            # incident card can update.
            safe_publish(
                "incident_classified",
                {
                    "incident_id": incident.id,
                    "classification": incident.classification,
                    "severity": incident.severity,
                    "confidence": incident.triager_confidence,
                },
            )
        except Exception as exc:  # noqa: BLE001 — caught + persisted
            run.status = "error"
            run.error_message = f"{type(exc).__name__}: {exc}"[:1000]
            run.output_summary = (
                "".join(tb_module.format_exception(type(exc), exc, exc.__traceback__))
            )[-2000:]
            run.finished_at = utcnow()
            session.add(run)
            session.commit()
            _publish_agent_finished(
                incident.id,
                "triager",
                "error",
                summary=run.error_message or "",
                duration_s=_run_duration(run),
            )
            raise


__all__ = ["handle_incident", "run_remediation_pipeline"]
