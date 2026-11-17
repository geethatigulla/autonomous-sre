"""Background dispatch for incident pipelines.

The webhook ingress (and the dashboard's synthetic trigger) hand each new
incident to :func:`dispatch_incident`, which FastAPI runs as a
``BackgroundTasks`` callable on its worker thread pool. This module adds the
guard rails that a bare ``asyncio.run(handle_incident(...))`` lacks:

* **Bounded concurrency** — at most ``MAX_CONCURRENT_INCIDENTS`` pipelines
  run at once (each one makes several LLM calls and spins up pytest
  sandboxes). Extra incidents wait their turn instead of stampeding the
  OpenAI / GitHub rate limits.
* **De-duplication** — the same incident id is never processed twice
  concurrently, even if it is dispatched twice.
* **Crash containment** — exceptions are logged and never escape into the
  thread pool, where they would vanish silently.
* **Orphan recovery** — :func:`recover_orphaned_incidents` runs at startup
  and closes out incidents that were mid-pipeline when the process died, so
  the dashboard does not show them as "in progress" forever.

This is deliberately in-process. A multi-replica deployment would swap it
for a real queue (RQ / Celery / Temporal); the public functions here are
the seam to do that.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from sqlmodel import Session, select

from orchestrator.config import get_settings
from orchestrator.timeutil import utcnow


log = logging.getLogger("orchestrator.job_queue")

# Incident statuses after which the pipeline will not touch the row again.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "released",
        "escalated",
        "failed",
        "fix_failed",
        "fix_rejected",
        "investigation_failed",
        "verification_failed",
        "release_failed",
    }
)

_ORPHAN_MESSAGE = (
    "Orchestrator restarted while this incident was in flight; "
    "the pipeline was abandoned. Re-send the alert to retry."
)

_lock = threading.Lock()
_in_flight: set[str] = set()
_semaphore: Optional[threading.BoundedSemaphore] = None
_semaphore_size: int = 0


def is_terminal(status: Optional[str]) -> bool:
    """True when an incident has reached a final state."""
    if not status:
        return False
    return status in TERMINAL_STATUSES or status.endswith("_failed")


def _get_semaphore() -> threading.BoundedSemaphore:
    """Lazily build the concurrency gate from settings."""
    global _semaphore, _semaphore_size
    with _lock:
        if _semaphore is None:
            size = max(1, int(get_settings().max_concurrent_incidents))
            _semaphore = threading.BoundedSemaphore(size)
            _semaphore_size = size
        return _semaphore


def in_flight() -> list[str]:
    """Incident ids currently being processed (snapshot)."""
    with _lock:
        return sorted(_in_flight)


def stats() -> dict[str, int]:
    """Small summary for the health endpoint."""
    _get_semaphore()
    with _lock:
        return {"in_flight": len(_in_flight), "max_concurrent": _semaphore_size}


def dispatch_incident(incident_id: str) -> bool:
    """Run the full pipeline for ``incident_id`` on the current thread.

    Intended to be scheduled via FastAPI ``BackgroundTasks`` (which runs
    sync callables in a worker thread). Returns ``True`` if the pipeline
    ran, ``False`` if it was skipped because the incident was already in
    flight. Never raises.
    """
    # Local import: state_machine imports agents/tools which import a lot;
    # keeping it lazy keeps `import orchestrator.webhooks` cheap.
    from orchestrator.orchestration.state_machine import handle_incident

    with _lock:
        if incident_id in _in_flight:
            log.info("incident %s already in flight; skipping duplicate", incident_id)
            return False
        _in_flight.add(incident_id)

    semaphore = _get_semaphore()
    try:
        if not semaphore.acquire(blocking=False):
            log.info("incident %s queued: concurrency limit reached", incident_id)
            semaphore.acquire()
        try:
            asyncio.run(handle_incident(incident_id))
        finally:
            semaphore.release()
        return True
    except Exception as exc:  # noqa: BLE001 — last line of defense
        log.exception("background dispatch failed for incident %s: %s", incident_id, exc)
        _mark_failed(incident_id, f"{type(exc).__name__}: {exc}")
        return True
    finally:
        with _lock:
            _in_flight.discard(incident_id)


def _mark_failed(incident_id: str, reason: str) -> None:
    """Best-effort: move a non-terminal incident to ``failed``."""
    try:
        from orchestrator.db import get_engine
        from orchestrator.models import Incident

        with Session(get_engine()) as session:
            incident = session.get(Incident, incident_id)
            if incident is None or is_terminal(incident.status):
                return
            incident.status = "failed"
            incident.updated_at = utcnow()
            session.add(incident)
            session.commit()
        log.warning("incident %s marked failed: %s", incident_id, reason[:300])
    except Exception:  # noqa: BLE001
        log.exception("could not mark incident %s as failed", incident_id)


def recover_orphaned_incidents() -> int:
    """Close out incidents left mid-pipeline by a previous process.

    Any incident whose status is not terminal, and any AgentRun still
    marked ``running``, is moved to a failed state with an explanatory
    message. Returns the number of incidents recovered. Never raises.
    """
    try:
        from orchestrator.db import get_engine
        from orchestrator.models import AgentRun, Incident

        recovered = 0
        now = utcnow()
        with Session(get_engine()) as session:
            for incident in session.exec(select(Incident)).all():
                if is_terminal(incident.status) or incident.id in _in_flight:
                    continue
                incident.status = "failed"
                incident.updated_at = now
                session.add(incident)
                recovered += 1

            for run in session.exec(
                select(AgentRun).where(AgentRun.status == "running")
            ).all():
                if run.incident_id in _in_flight:
                    continue
                run.status = "error"
                run.finished_at = now
                run.error_message = run.error_message or _ORPHAN_MESSAGE
                session.add(run)

            session.commit()
        if recovered:
            log.warning("Recovered %d orphaned incident(s) from a previous run", recovered)
        return recovered
    except Exception:  # noqa: BLE001
        log.exception("orphaned-incident recovery failed")
        return 0


def _reset_for_tests() -> None:
    """Drop the cached semaphore and in-flight set (tests flip settings)."""
    global _semaphore, _semaphore_size
    with _lock:
        _semaphore = None
        _semaphore_size = 0
        _in_flight.clear()


__all__ = [
    "TERMINAL_STATUSES",
    "dispatch_incident",
    "in_flight",
    "is_terminal",
    "recover_orphaned_incidents",
    "stats",
]
