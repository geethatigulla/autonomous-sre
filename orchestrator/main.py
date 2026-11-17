"""FastAPI entry point for the autonomous-sre orchestrator.

Wires the moving parts together:

* SQLite-backed persistence (:mod:`orchestrator.db`) is initialized on
  startup so the first webhook never crashes on a missing table.
* The webhook router (:mod:`orchestrator.webhooks.ingress`) is mounted
  under ``/webhooks``.
* The process-wide tracer is constructed on startup so the
  ``Tracing backend: <omium|local>`` log line appears before the first
  request — useful when someone is watching the orchestrator's terminal
  during a smoke test.

* The dashboard (SSE stream, metrics API, SPA) is mounted at the root.
* Missing configuration is reported loudly at startup and via
  ``GET /ready``; the process still boots so the dashboard is usable
  while keys are being filled in.
* Incidents left mid-pipeline by a previous crash are closed out on
  startup (see :func:`orchestrator.orchestration.job_queue.recover_orphaned_incidents`).
"""

from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from orchestrator.config import get_settings
from orchestrator.dashboard.routes import router as dashboard_router
from orchestrator.db import get_engine, init_db
from orchestrator.orchestration import job_queue
from orchestrator.tracing import get_tracer
from orchestrator.webhooks.ingress import router as webhooks_router


log = logging.getLogger("orchestrator.main")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: init DB + tracer on entry, shutdown tracer on exit."""
    # Persistence first — every webhook reads from / writes to the DB.
    init_db()
    log.info("Database initialized at %s", get_settings().database_url)
    job_queue.recover_orphaned_incidents()

    # Name every missing required setting up front instead of letting the
    # first incident fail deep inside the pipeline.
    missing = get_settings().missing_required()
    if missing:
        log.warning(
            "Missing required configuration: %s - incidents will be "
            "recorded but the pipeline cannot complete. Copy .env.example "
            "to .env and fill these in, then restart.",
            ", ".join(missing),
        )

    # Constructing the tracer here (vs. lazily on first request) means
    # the startup log makes the active backend obvious — operators
    # running ``uvicorn`` will see whether they're shipping to Omium or
    # only the local JSONL.
    tracer = get_tracer()
    log.info(
        "Orchestrator ready, workflow tracing: %s", tracer.backend_used
    )

    # Surface investigator tool availability up front so operators don't
    # have to inspect span metadata after the fact.
    settings = get_settings()
    repo_label = settings.github_repo if (
        settings.github_token and settings.github_repo
    ) else "disabled"
    search_label = "enabled" if settings.tavily_api_key else "disabled"
    log.info(
        "GitHub repo: %s, Web search: %s", repo_label, search_label
    )

    # Verifier sandbox config. Surfaced on startup so it's obvious to
    # anyone watching `uvicorn` that patches run in a tempdir (NOT the
    # real working tree) and how long pytest is allowed to take.
    log.info("Sandbox: tempdir-based, pytest timeout: 60s")
    if shutil.which("git") is None:
        log.warning(
            "git not found on PATH - the Verifier sandbox needs it to apply "
            "patches, so every fix will fail verification until it is installed."
        )

    # Parallel-fix race surfaces both strategies on startup so it's
    # obvious which "fix philosophies" the orchestrator will race.
    try:
        from orchestrator.orchestration.state_machine import FIX_STRATEGIES
        log.info(
            "Fix strategies enabled: %s", ", ".join(FIX_STRATEGIES)
        )
    except Exception:  # pragma: no cover — defensive
        log.info("Fix strategies enabled: defensive, permissive")

    # Releaser notifications. The Slack webhook is optional — when
    # unset, the Releaser logs the message it would have sent and
    # marks slack_posted=False but still opens the PR.
    slack_label = "enabled" if (
        settings.slack_webhook_url and settings.slack_webhook_url.strip()
    ) else "disabled"
    log.info("Slack webhook: %s", slack_label)

    # Surface the dashboard URL so anyone watching the uvicorn log
    # knows where to point their browser for the live demo view.
    log.info("Dashboard available at http://localhost:8000/")

    try:
        yield
    finally:
        # Flush any buffered spans on shutdown. The wrapper is
        # idempotent + swallow-all so this is safe to call even if
        # tracing never initialized.
        try:
            tracer.shutdown()
        except Exception:  # pragma: no cover — defensive
            pass


app = FastAPI(title="autonomous-sre", version="0.1.0", lifespan=lifespan)
app.include_router(webhooks_router, prefix="/webhooks", tags=["webhooks"])
# Dashboard mounts at the root: GET /, /events/stream, /api/incidents/recent,
# /api/test/trigger. No prefix so the SPA can use absolute paths.
app.include_router(dashboard_router, tags=["dashboard"])


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe used by docker-compose and the dashboard."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness probe: is the orchestrator able to remediate incidents?

    Returns 200 when the database is reachable and every required setting
    is present, 503 otherwise. The body lists each setting's status (never
    its value) so operators can see exactly what is missing.
    """
    settings = get_settings()
    report = settings.config_report()
    missing = settings.missing_required()

    db_ok = True
    db_error = None
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = f"{type(exc).__name__}: {exc}"[:300]

    git_ok = shutil.which("git") is not None

    is_ready = db_ok and git_ok and not missing
    body = {
        "ready": is_ready,
        "missing_required": missing,
        "config": report,
        "database": {"ok": db_ok, "error": db_error},
        "sandbox": {"git": git_ok},
        "queue": job_queue.stats(),
    }
    return JSONResponse(body, status_code=200 if is_ready else 503)
