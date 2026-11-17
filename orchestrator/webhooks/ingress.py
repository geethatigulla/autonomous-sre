"""Ingress handler: accepts alerts from monitoring providers and enqueues incidents.

Exposes the public webhook surface:

* ``POST /webhooks/alert`` — receives a crash report from the victim
  service, persists an :class:`Incident` row in ``status="received"``,
  schedules :func:`orchestrator.orchestration.state_machine.handle_incident`
  as a background task, and returns ``202 Accepted`` immediately.
* ``GET  /webhooks/alert/{incident_id}`` — full incident detail plus
  every :class:`AgentRun` row for the dashboard / demo / tests.
* ``GET  /webhooks/alert`` — the 50 most recent incidents, newest first.

Implementation notes
--------------------
The :class:`AlertPayload` schema is intentionally permissive: real
monitoring providers (and the victim app's middleware) emit slightly
different shapes. We accept either ``endpoint`` (the documented field)
or ``path`` + ``method`` (what the victim app actually sends today) and
synthesize the canonical ``endpoint`` string before persisting.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from orchestrator.dashboard.events import safe_publish
from orchestrator.db import get_session
from orchestrator.models import AgentRun, Incident


log = logging.getLogger("orchestrator.webhooks.ingress")


router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AlertPayload(BaseModel):
    """Webhook body accepted by ``POST /webhooks/alert``.

    Supports both the documented shape (``endpoint``) and the victim
    app's middleware shape (``path`` + ``method``); the handler
    normalizes them to a single ``"<METHOD> <PATH>"`` endpoint string
    before persisting.
    """

    timestamp: Optional[str] = None
    endpoint: Optional[str] = None
    path: Optional[str] = None
    method: Optional[str] = None
    exception_class: str
    exception_message: str = ""
    traceback: str = ""
    request_body: str = ""
    source_service: str = "unknown"

    # Outbound-scrubber bookkeeping (set by victim_app.monitor before it
    # POSTs the alert). Optional so legacy payloads that pre-date the
    # scrubber still validate. ``redactions`` is a list of redaction
    # types that fired, e.g. ["api_key", "email"].
    scrubbed: Optional[bool] = None
    redactions: Optional[list[str]] = None

    # Allow forward-compatible fields without rejecting the request.
    model_config = {"extra": "allow"}


class AlertAccepted(BaseModel):
    """``202 Accepted`` response body."""

    incident_id: str
    status: str = Field(
        default="queued",
        description="Always 'queued' for newly created incidents.",
    )


class AgentRunOut(BaseModel):
    """Trimmed-down agent-run row used in detail responses."""

    id: str
    agent_name: str
    started_at: str
    finished_at: Optional[str]
    status: str
    input_summary: Optional[str]
    output_summary: Optional[str]
    error_message: Optional[str]
    span_id: Optional[str]


class IncidentDetail(BaseModel):
    """Full incident + every AgentRun, returned by the detail endpoint."""

    incident: dict
    agent_runs: list[AgentRunOut]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_endpoint(payload: AlertPayload) -> str:
    """Pick the best endpoint string from the (sometimes-inconsistent) payload.

    Order: explicit ``endpoint`` field, then ``"<METHOD> <PATH>"``, then
    ``path`` alone, then a placeholder so the column is never empty.
    """
    if payload.endpoint:
        return payload.endpoint
    if payload.path:
        if payload.method:
            return f"{payload.method.upper()} {payload.path}"
        return payload.path
    return "unknown"


def _serialize_incident(incident: Incident) -> dict[str, Any]:
    """Return a JSON-friendly dump of an Incident row.

    SQLModel's :py:meth:`model_dump` handles the columns; we ISO-format
    the datetimes so the dashboard doesn't have to.
    """
    data = incident.model_dump()
    for k in ("created_at", "updated_at"):
        if data.get(k) is not None:
            data[k] = data[k].isoformat()
    return data


def _serialize_run(run: AgentRun) -> AgentRunOut:
    return AgentRunOut(
        id=run.id,
        agent_name=run.agent_name,
        started_at=run.started_at.isoformat() if run.started_at else "",
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        status=run.status,
        input_summary=run.input_summary,
        output_summary=run.output_summary,
        error_message=run.error_message,
        span_id=run.span_id,
    )


def _dispatch(incident_id: str) -> None:
    """Background-task entrypoint.

    Delegates to :func:`orchestrator.orchestration.job_queue.dispatch_incident`,
    which runs the async state machine with bounded concurrency,
    de-duplication, and crash containment. FastAPI's BackgroundTasks runs
    this sync callable in a thread pool.
    """
    # Local import avoids a circular dependency: state_machine -> db ->
    # webhooks at import time.
    from orchestrator.orchestration.job_queue import dispatch_incident

    dispatch_incident(incident_id)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/alert",
    response_model=AlertAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_alert(
    payload: AlertPayload,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> AlertAccepted:
    """Persist an incident and schedule async triage.

    Returns ``202 Accepted`` immediately so the victim service's
    middleware never blocks. The Triager runs in the background.
    """
    endpoint = _resolve_endpoint(payload)

    # Stash the full webhook for replay/debugging.
    raw_payload = payload.model_dump_json()

    # Persist the victim-app scrubber's audit trail alongside the rest
    # of the payload. ``redactions`` is JSON-encoded so the column stays
    # a plain string in SQLite; the dashboard JSON-decodes it client-side.
    redactions_json: Optional[str]
    if payload.redactions is None:
        redactions_json = None
    else:
        redactions_json = json.dumps(payload.redactions)

    incident = Incident(
        source_service=payload.source_service or "unknown",
        endpoint=endpoint,
        exception_class=payload.exception_class,
        exception_message=payload.exception_message or "",
        traceback=payload.traceback or "",
        request_body=payload.request_body or "",
        raw_payload=raw_payload,
        scrubbed=payload.scrubbed,
        redactions=redactions_json,
        status="received",
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)

    log.info(
        "incident %s received: %s @ %s",
        incident.id,
        incident.exception_class,
        incident.endpoint,
    )

    # Fire dashboard "incident_received" before the background task is
    # scheduled. safe_publish swallows any error so the webhook surface
    # is unaffected if the dashboard package fails to load.
    safe_publish(
        "incident_received",
        {
            "incident_id": incident.id,
            "exception_class": incident.exception_class,
            "exception_message": incident.exception_message,
            "endpoint": incident.endpoint,
            "source_service": incident.source_service,
        },
    )

    background_tasks.add_task(_dispatch, incident.id)

    return AlertAccepted(incident_id=incident.id, status="queued")


@router.get("/alert/{incident_id}", response_model=IncidentDetail)
async def get_incident(
    incident_id: str,
    session: Session = Depends(get_session),
) -> IncidentDetail:
    """Return one incident plus every agent run attached to it."""
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")

    runs = session.exec(
        select(AgentRun)
        .where(AgentRun.incident_id == incident_id)
        .order_by(AgentRun.started_at)
    ).all()

    return IncidentDetail(
        incident=_serialize_incident(incident),
        agent_runs=[_serialize_run(r) for r in runs],
    )


@router.get("/alert")
async def list_incidents(
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """List the 50 most recent incidents, newest first."""
    incidents = session.exec(
        select(Incident).order_by(Incident.created_at.desc()).limit(50)
    ).all()
    return [_serialize_incident(i) for i in incidents]


__all__ = ["router"]
