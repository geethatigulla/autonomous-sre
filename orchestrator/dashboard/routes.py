"""HTTP surface for the operator dashboard.

Four routes plus one demo helper:

* ``GET /`` — the dashboard SPA (single self-contained HTML file).
* ``GET /events/stream`` — Server-Sent Events feed of pipeline activity.
  Each connected browser receives every event published on the bus
  from the moment it connects.
* ``GET /api/incidents/recent`` — REST fallback the SPA hits on initial
  load and after a reconnect, so the user sees the most recent incident
  state even if they opened the dashboard mid-pipeline.
* ``GET /api/metrics`` — read-only aggregate statistics across all
  incidents. The dashboard polls this every few seconds to render the
  metrics summary row at the top of the page.

And one demo helper:

* ``POST /api/test/trigger`` — synthesises a fake alert (null_deref by
  default, or one of regression / config_error / unknown when ``?bug=``
  is supplied) and persists it through the orchestrator's own
  background dispatcher. Useful when the presenter doesn't want to
  switch to a terminal.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session, select
from sse_starlette.sse import EventSourceResponse

from orchestrator.config import get_settings
from orchestrator.dashboard.events import get_event_bus
from orchestrator.db import get_session
from orchestrator.models import AgentRun, Incident
from orchestrator.timeutil import utcnow


log = logging.getLogger("orchestrator.dashboard.routes")


router = APIRouter()


# ---------------------------------------------------------------------------
# Static HTML loading
# ---------------------------------------------------------------------------


_DASHBOARD_HTML_PATH = Path(__file__).parent / "static" / "index.html"


def _load_dashboard_html() -> str:
    """Read the single-page dashboard HTML from disk.

    Re-read on every request so a developer editing the file during the
    demo can refresh the page and see changes without restarting
    uvicorn. The file is small (<50KB) and reads come from the OS page
    cache, so the cost is negligible.
    """
    try:
        return _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.error("dashboard HTML missing at %s", _DASHBOARD_HTML_PATH)
        return (
            "<!doctype html><html><body>"
            "<h1>autonomous-sre dashboard</h1>"
            "<p>static/index.html missing — see orchestrator/dashboard/static/</p>"
            "</body></html>"
        )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_incident(incident: Incident) -> dict[str, Any]:
    """Return a JSON-friendly dump of an Incident row.

    Datetimes are flattened to ISO strings; the verification_result
    JSON is parsed back so the dashboard can render alternatives
    without re-parsing in the browser.
    """
    data = incident.model_dump()
    for k in ("created_at", "updated_at"):
        if data.get(k) is not None:
            data[k] = data[k].isoformat()
    if data.get("verification_result"):
        try:
            data["verification_result_parsed"] = json.loads(
                data["verification_result"]
            )
        except Exception:  # noqa: BLE001
            data["verification_result_parsed"] = None
    return data


def _serialize_run(run: AgentRun) -> dict[str, Any]:
    """Return a JSON-friendly dump of an AgentRun row."""
    data = run.model_dump()
    for k in ("started_at", "finished_at"):
        if data.get(k) is not None:
            data[k] = data[k].isoformat()
    return data


# ---------------------------------------------------------------------------
# GET / — the dashboard SPA
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard_index() -> HTMLResponse:
    """Serve the single-file dashboard."""
    return HTMLResponse(content=_load_dashboard_html())


# ---------------------------------------------------------------------------
# GET /events/stream — SSE feed
# ---------------------------------------------------------------------------


# Heartbeat cadence. The 15-second timeout matches typical proxy
# (nginx, Cloudflare, etc.) idle-connection windows so the connection
# stays alive even when no events are flowing.
_HEARTBEAT_SECONDS = 15


@router.get("/events/stream")
async def event_stream(request: Request) -> EventSourceResponse:
    """Open an SSE feed onto the dashboard event bus.

    Each connected browser receives a synthetic ``hello`` event so it
    can flip the connection indicator green, then every subsequent
    event published on the bus, plus a ``ping`` heartbeat every 15
    seconds of silence to keep proxies from closing the connection.
    """
    bus = get_event_bus()
    q = bus.subscribe()

    async def event_generator():
        try:
            yield {
                "event": "hello",
                "data": json.dumps(
                    {
                        "ts": utcnow().isoformat(),
                        "subscribers": bus.subscriber_count,
                    }
                ),
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        q.get(), timeout=_HEARTBEAT_SECONDS
                    )
                    yield {
                        "event": payload["type"],
                        "data": json.dumps(payload),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            bus.unsubscribe(q)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# GET /api/incidents/recent — REST fallback
# ---------------------------------------------------------------------------


@router.get("/api/incidents/recent")
async def recent_incidents(
    limit: int = 20,
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Return the N most recent incidents plus every AgentRun row.

    The dashboard hits this on page load (so we can show "what already
    happened" if a user opens the dashboard after an incident already
    finished) and after every SSE reconnect.
    """
    limit = max(1, min(int(limit), 100))

    incidents = session.exec(
        select(Incident).order_by(Incident.created_at.desc()).limit(limit)
    ).all()

    if not incidents:
        return JSONResponse(content={"incidents": [], "omium_project": None})

    incident_ids = [i.id for i in incidents]
    runs = session.exec(
        select(AgentRun)
        .where(AgentRun.incident_id.in_(incident_ids))
        .order_by(AgentRun.started_at)
    ).all()

    runs_by_incident: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        runs_by_incident.setdefault(r.incident_id, []).append(
            _serialize_run(r)
        )

    settings = get_settings()
    omium_project = (
        settings.omium_project if settings.omium_configured else None
    )

    out = []
    for inc in incidents:
        d = _serialize_incident(inc)
        d["agent_runs"] = runs_by_incident.get(inc.id, [])
        out.append(d)

    return JSONResponse(
        content={"incidents": out, "omium_project": omium_project}
    )


# ---------------------------------------------------------------------------
# GET /api/metrics — aggregate stats across all incidents
# ---------------------------------------------------------------------------


# Terminal statuses we count as outright failures. ``escalated`` is
# tracked separately because it represents human hand-off, not a
# pipeline error.
_FAILED_STATUSES = {
    "fix_rejected",
    "investigation_failed",
    "fix_failed",
    "verification_failed",
    "release_failed",
}

# Statuses we consider "still in flight" — anything that isn't a
# terminal outcome (released / escalated / *_failed / fix_rejected).
_TERMINAL_STATUSES = (
    _FAILED_STATUSES | {"released", "escalated"}
)

# Classifications the Triager actually emits. We keep an explicit
# allowlist so the dashboard always renders the same four buckets even
# when zero rows exist.
_KNOWN_CLASSIFICATIONS = (
    "null_deref",
    "recent_regression",
    "config_error",
    "unknown",
)


def _classify_status(status: str) -> str:
    """Bucket an Incident.status into one of the four metrics groups.

    Returns one of: ``"resolved"``, ``"escalated"``, ``"failed"``,
    ``"in_progress"``. The function is total — anything that isn't a
    documented terminal status is treated as still in progress.
    """
    if status == "released":
        return "resolved"
    if status == "escalated":
        return "escalated"
    if status in _FAILED_STATUSES or status.endswith("_failed"):
        return "failed"
    return "in_progress"


@router.get("/api/metrics")
async def metrics(
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Aggregate stats across all incidents. Read-only, no side effects.

    Returns a flat JSON dict with::

        total_incidents          int
        resolved_count           int  (status == "released")
        escalated_count          int  (status == "escalated")
        failed_count             int  (status ends with "_failed"
                                       or == "fix_rejected")
        in_progress_count        int  (everything else)
        success_rate             float in [0.0, 1.0]
        avg_resolution_seconds   float — average wall-clock from
                                          incident.created_at to the
                                          last AgentRun.finished_at for
                                          released incidents only.
        by_classification        dict[str, int] with one bucket per
                                                  known classification.
        total_prs_opened         int — incidents with non-null pr_url.

    The endpoint is intentionally cheap: it issues two SELECTs and does
    all aggregation in Python so we don't have to teach SQLAlchemy any
    of our domain rules.
    """
    incidents = session.exec(select(Incident)).all()
    total_incidents = len(incidents)

    resolved_count = 0
    escalated_count = 0
    failed_count = 0
    in_progress_count = 0
    total_prs_opened = 0
    by_classification: dict[str, int] = {
        c: 0 for c in _KNOWN_CLASSIFICATIONS
    }

    # Resolved incident IDs whose duration we'll average.
    resolved_ids: list[str] = []
    incident_created_at: dict[str, datetime] = {}

    for inc in incidents:
        bucket = _classify_status(inc.status or "")
        if bucket == "resolved":
            resolved_count += 1
            resolved_ids.append(inc.id)
            incident_created_at[inc.id] = inc.created_at
        elif bucket == "escalated":
            escalated_count += 1
        elif bucket == "failed":
            failed_count += 1
        else:
            in_progress_count += 1

        if inc.pr_url:
            total_prs_opened += 1

        cls = inc.classification
        if cls in by_classification:
            by_classification[cls] += 1
        elif cls:
            # An unexpected classification — still surface it under
            # "unknown" so the totals add up to len(incidents) for any
            # incident that *has* been classified.
            by_classification["unknown"] += 1

    # Success rate: resolved divided by (resolved + failed + escalated).
    # In-progress incidents shouldn't penalise the rate while they're
    # still moving through the pipeline.
    decided = resolved_count + failed_count + escalated_count
    success_rate = (resolved_count / decided) if decided > 0 else 0.0

    # Avg resolution = avg of (last AgentRun.finished_at - created_at)
    # for released incidents. If a released incident somehow has no
    # AgentRun rows we fall back to incident.updated_at to keep the
    # number meaningful.
    avg_resolution_seconds = 0.0
    if resolved_ids:
        runs = session.exec(
            select(AgentRun).where(AgentRun.incident_id.in_(resolved_ids))
        ).all()
        last_finish: dict[str, datetime] = {}
        for r in runs:
            if r.finished_at is None:
                continue
            cur = last_finish.get(r.incident_id)
            if cur is None or r.finished_at > cur:
                last_finish[r.incident_id] = r.finished_at

        durations: list[float] = []
        # Refetch updated_at lazily from the original list — cheaper
        # than another query.
        updated_at_by_id = {i.id: i.updated_at for i in incidents}
        for iid in resolved_ids:
            start = incident_created_at.get(iid)
            end = last_finish.get(iid) or updated_at_by_id.get(iid)
            if start is None or end is None:
                continue
            delta = (end - start).total_seconds()
            if delta >= 0:
                durations.append(delta)
        if durations:
            avg_resolution_seconds = sum(durations) / len(durations)

    return JSONResponse(
        content={
            "total_incidents": total_incidents,
            "resolved_count": resolved_count,
            "escalated_count": escalated_count,
            "failed_count": failed_count,
            "in_progress_count": in_progress_count,
            "success_rate": round(success_rate, 4),
            "avg_resolution_seconds": round(avg_resolution_seconds, 2),
            "by_classification": by_classification,
            "total_prs_opened": total_prs_opened,
        }
    )


# ---------------------------------------------------------------------------
# POST /api/test/trigger — synthesize an alert for the live demo
# ---------------------------------------------------------------------------


# Each bug type has a self-contained alert payload that mimics what a
# real monitoring webhook would have posted. The dashboard's trigger
# button synthesises one of these and runs it through the standard
# dispatcher — no demo-only code path.
_BUG_PAYLOADS: dict[str, dict[str, Any]] = {
    "null_deref": {
        "endpoint": "POST /shorten",
        "method": "POST",
        "path": "/shorten",
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
    },
    "regression": {
        "endpoint": "GET /expand/{code}",
        "method": "GET",
        "path": "/expand/deadbe",
        "exception_class": "KeyError",
        "exception_message": "'deadbe'",
        "traceback": (
            "Traceback (most recent call last):\n"
            '  File "victim_app/app.py", line 142, in expand\n'
            "    return {\"url\": STORE[code]}\n"
            "KeyError: 'deadbe'"
        ),
        "request_body": "{}",
        "source_service": "victim_app",
    },
    "config_error": {
        "endpoint": "STARTUP",
        "method": "STARTUP",
        "path": "STARTUP",
        "exception_class": "KeyError",
        "exception_message": "REDIS_URL",
        "traceback": (
            "Traceback (most recent call last):\n"
            '  File "victim_app/app.py", line 39, in <module>\n'
            '    REDIS_URL = os.environ["REDIS_URL"]\n'
            '  File "/usr/lib/python3.11/os.py", line 685, in __getitem__\n'
            "    raise KeyError(key) from None\n"
            "KeyError: 'REDIS_URL'"
        ),
        "request_body": "{}",
        "source_service": "victim_app",
    },
    "unknown": {
        "endpoint": "GET /healthcheck/internal",
        "method": "GET",
        "path": "/healthcheck/internal",
        "exception_class": "RuntimeError",
        "exception_message": "Event loop is closed",
        "traceback": (
            "Traceback (most recent call last):\n"
            '  File "victim_app/app.py", line 142, in healthcheck\n'
            "    return await self._poll_downstream()\n"
            '  File "victim_app/app.py", line 88, in _poll_downstream\n'
            "    fut = asyncio.ensure_future(self._client.get(url))\n"
            "RuntimeError: Event loop is closed"
        ),
        "request_body": "{}",
        "source_service": "victim_app",
    },
}


@router.post("/api/test/trigger")
async def trigger_test_incident(
    background_tasks: BackgroundTasks,
    bug: str = Query(
        default="null_deref",
        description=(
            "Which synthetic alert to fire. One of: null_deref, "
            "regression, config_error, unknown. Unknown values fall "
            "back to null_deref so the demo never errors out."
        ),
    ),
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Synthesise a fake alert and run the pipeline against it.

    Persists an Incident directly and schedules the same background
    dispatch the webhook uses. Keeping the demo path identical to the
    production path means we never run "demo-only" code in front of
    judges.

    ``bug`` selects which canned payload to use; if the value isn't
    recognised we fall back silently to the null_deref payload so a
    typo in the dropdown never breaks the demo.
    """
    bug_key = bug if bug in _BUG_PAYLOADS else "null_deref"
    payload = dict(_BUG_PAYLOADS[bug_key])
    payload["timestamp"] = utcnow().isoformat()

    incident = Incident(
        source_service=payload["source_service"],
        endpoint=payload["endpoint"],
        exception_class=payload["exception_class"],
        exception_message=payload["exception_message"],
        traceback=payload["traceback"],
        request_body=payload["request_body"],
        raw_payload=json.dumps(payload),
        status="received",
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)

    # Publish "incident_received" up front so the dashboard reacts
    # instantly — before the background task has even started.
    from orchestrator.dashboard.events import safe_publish

    safe_publish(
        "incident_received",
        {
            "incident_id": incident.id,
            "exception_class": incident.exception_class,
            "exception_message": incident.exception_message,
            "endpoint": incident.endpoint,
            "source_service": incident.source_service,
            "synthetic": True,
            "bug": bug_key,
        },
    )

    from orchestrator.webhooks.ingress import _dispatch  # noqa: WPS437

    background_tasks.add_task(_dispatch, incident.id)

    return JSONResponse(
        content={
            "incident_id": incident.id,
            "status": "queued",
            "synthetic": True,
            "bug": bug_key,
        },
        status_code=202,
    )


__all__ = ["router"]
