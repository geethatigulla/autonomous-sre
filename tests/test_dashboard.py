"""Tests for the operator dashboard wiring.

Covers:

1. ``test_dashboard_html_loads`` — ``GET /`` returns 200 and the HTML body
   contains the dashboard title so we know the static asset is being
   served.
2. ``test_sse_stream_sends_hello`` — subscribing to ``/events/stream``
   produces a ``hello`` event before anything else.
3. ``test_event_bus_publishes_to_subscribers`` — two queues subscribed to
   one bus both receive a single publish.
4. ``test_event_bus_drops_when_queue_full`` — at capacity (100), the
   oldest event is dropped, the newest event is retained.
5. ``test_recent_incidents_returns_json`` — REST fallback returns the
   expected envelope and reflects DB content.

The fixtures pin a tmpdir SQLite + local-only tracing so tests do not
share state with each other or with the user's machine.
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """Yield a freshly-built FastAPI app + a clean SQLite database.

    Resets the orchestrator's cached settings / engine / tracer between
    tests so they cannot bleed state. Mirrors the fixture pattern in
    :mod:`tests.test_end_to_end_triage` so we stay consistent across
    the suite.
    """
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
    from orchestrator.dashboard import events as events_module

    config_module.get_settings.cache_clear()
    db_module.reset_engine()
    tracing_module.reset_tracer()
    events_module.reset_event_bus()

    from orchestrator.main import app

    yield app

    config_module.get_settings.cache_clear()
    db_module.reset_engine()
    tracing_module.reset_tracer()
    events_module.reset_event_bus()


# ---------------------------------------------------------------------------
# Test 1 — GET / returns the dashboard HTML
# ---------------------------------------------------------------------------


def test_dashboard_html_loads(isolated_app):
    """The root path returns a 200 HTML response with the dashboard title."""
    from fastapi.testclient import TestClient

    with TestClient(isolated_app) as client:
        resp = client.get("/")
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Surface marker visible to a human inspecting devtools.
        assert "AUTONOMOUS SRE" in body
        # We expect the SSE client wire-up to be present too.
        assert "/events/stream" in body


# ---------------------------------------------------------------------------
# Test 2 — SSE stream sends a hello event
# ---------------------------------------------------------------------------


def test_sse_stream_sends_hello(isolated_app):
    """Subscribing to the stream produces a ``hello`` event first.

    We invoke the SSE route handler directly rather than going through
    a TestClient. The handler returns an :class:`EventSourceResponse`
    whose ``body_iterator`` yields formatted SSE chunks; we pull the
    first chunk under a tight asyncio timeout. This avoids the
    long-lived TCP semantics that make TestClient streaming unreliable
    for one-frame-only assertions.
    """
    from starlette.requests import Request

    from orchestrator.dashboard import events as events_module
    from orchestrator.dashboard.routes import event_stream

    # Fresh bus per call so this test doesn't pick up subscribers from
    # earlier tests in the same process.
    events_module.reset_event_bus()

    async def go():
        # Minimal ASGI scope sufficient for Request.is_disconnected().
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/events/stream",
            "headers": [],
            "query_string": b"",
        }

        async def receive():
            # Never sends a disconnect; the handler ends when our
            # generator iteration stops.
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(_msg):
            pass

        request = Request(scope, receive=receive, send=send)
        response = await event_stream(request)
        # EventSourceResponse stores the wrapped generator on .body_iterator.
        agen = response.body_iterator
        first_chunk = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
        # Drain a couple more frames so we capture the data line too.
        # sse-starlette emits "event:" and "data:" as one frame ending
        # with two newlines; one chunk is enough.
        try:
            agen.aclose  # noqa: B018 — just touch the attr to confirm presence
        except Exception:  # noqa: BLE001
            pass
        return first_chunk

    chunk = asyncio.run(go())
    # sse-starlette's wrapped generator yields ``dict`` payloads; the
    # SSE protocol formatting happens further down the response stack
    # (inside Starlette's response sender) which we don't drive here.
    # That's fine for this assertion — we only need to confirm the
    # generator emits a ``hello`` event with a parseable JSON ``data``.
    if isinstance(chunk, (bytes, bytearray)):
        body_text = chunk.decode("utf-8")
        # In bytes form sse-starlette has already formatted the SSE
        # frame: ``event: hello\r\ndata: {...}\r\n\r\n``.
        assert "hello" in body_text, body_text
        # Find a data: line and assert it parses.
        data_line = next(
            (ln for ln in body_text.splitlines() if ln.startswith("data:")),
            None,
        )
        assert data_line is not None, body_text
        payload = json.loads(data_line[len("data:"):].strip())
        assert "ts" in payload
    else:
        # The wrapper yielded a dict (``{"event": "hello", "data": "..."}``).
        assert isinstance(chunk, dict), repr(chunk)
        assert chunk.get("event") == "hello", chunk
        data = json.loads(chunk["data"])
        assert "ts" in data


# ---------------------------------------------------------------------------
# Test 3 — EventBus fan-out to multiple subscribers
# ---------------------------------------------------------------------------


def test_event_bus_publishes_to_subscribers():
    """A single publish() reaches every subscriber's queue."""
    from orchestrator.dashboard.events import EventBus

    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()

    async def go():
        await bus.publish("agent_started", {"incident_id": "inc_abc", "agent_name": "triager"})
        p1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        p2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        return p1, p2

    p1, p2 = asyncio.run(go())
    assert p1["type"] == "agent_started"
    assert p2["type"] == "agent_started"
    assert p1["data"]["agent_name"] == "triager"
    assert p2["data"]["agent_name"] == "triager"
    assert "ts" in p1


# ---------------------------------------------------------------------------
# Test 4 — backpressure drops oldest, not newest
# ---------------------------------------------------------------------------


def test_event_bus_drops_when_queue_full():
    """Filling a queue to capacity (100) and publishing once more drops
    the oldest event and keeps the freshest one — the documented policy.
    """
    from orchestrator.dashboard.events import EventBus

    bus = EventBus()
    q = bus.subscribe()

    async def go():
        # Fill exactly to capacity (100 — the queue's maxsize).
        for i in range(100):
            await bus.publish("status_changed", {"incident_id": "inc_x", "i": i})
        assert q.qsize() == 100
        # One more event — should evict event 0, push event 100.
        await bus.publish("status_changed", {"incident_id": "inc_x", "i": 100})
        assert q.qsize() == 100
        # Drain and confirm event 0 was dropped and event 100 is at the tail.
        drained = []
        while not q.empty():
            drained.append(await q.get())
        return drained

    drained = asyncio.run(go())
    indices = [d["data"]["i"] for d in drained]
    assert indices[0] == 1, f"expected oldest (i=0) dropped, got {indices[:3]}"
    assert indices[-1] == 100, f"expected newest (i=100) preserved, got tail={indices[-3:]}"


# ---------------------------------------------------------------------------
# Test 5 — recent-incidents endpoint shape
# ---------------------------------------------------------------------------


def test_recent_incidents_returns_json(isolated_app):
    """``GET /api/incidents/recent`` returns the expected envelope shape."""
    from fastapi.testclient import TestClient

    from orchestrator.db import get_engine
    from orchestrator.models import AgentRun, Incident
    from sqlmodel import Session

    with TestClient(isolated_app) as client:
        # Seed AFTER entering the lifespan so init_db has created the
        # incident / agent_run tables.
        with Session(get_engine()) as session:
            inc = Incident(
                source_service="victim_app",
                endpoint="POST /shorten",
                exception_class="AttributeError",
                exception_message="'NoneType' object has no attribute 'strip'",
                traceback="…",
                request_body="{}",
                raw_payload="{}",
                status="released",
                classification="null_deref",
                severity="P1",
                pr_url="https://github.com/example/repo/pull/42",
                pr_number=42,
            )
            session.add(inc)
            session.commit()
            session.refresh(inc)
            session.add(
                AgentRun(
                    incident_id=inc.id,
                    agent_name="triager",
                    status="success",
                    output_summary="classified as null_deref",
                )
            )
            session.commit()
            seeded_id = inc.id

        resp = client.get("/api/incidents/recent?limit=5")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, dict)
        assert "incidents" in body
        assert isinstance(body["incidents"], list)
        assert "omium_project" in body
        # We just seeded one — confirm it shows up with the right shape.
        assert len(body["incidents"]) >= 1
        first = body["incidents"][0]
        for key in (
            "id",
            "status",
            "classification",
            "severity",
            "exception_class",
            "endpoint",
            "agent_runs",
        ):
            assert key in first, f"recent incident envelope missing {key!r}"
        assert first["id"] == seeded_id
        assert isinstance(first["agent_runs"], list)
        assert first["agent_runs"][0]["agent_name"] == "triager"


# ---------------------------------------------------------------------------
# Test 6 — metrics endpoint shape and aggregation
# ---------------------------------------------------------------------------


def test_metrics_endpoint_returns_expected_shape(isolated_app):
    """``GET /api/metrics`` returns the documented aggregate envelope.

    Seeds three incidents covering each metrics bucket — one released
    (with a PR URL and an AgentRun for the resolution-time average),
    one escalated, and one in-progress — then asserts the response
    fields are present, well-typed, and reflect the seeded data.
    """
    from datetime import datetime, timedelta, timezone

    from fastapi.testclient import TestClient

    from orchestrator.db import get_engine
    from orchestrator.models import AgentRun, Incident
    from sqlmodel import Session

    with TestClient(isolated_app) as client:
        # 0 incidents — every counter should be 0 and the envelope
        # should still contain every documented key.
        resp = client.get("/api/metrics")
        assert resp.status_code == 200, resp.text
        empty = resp.json()
        for key in (
            "total_incidents",
            "resolved_count",
            "escalated_count",
            "failed_count",
            "in_progress_count",
            "success_rate",
            "avg_resolution_seconds",
            "by_classification",
            "total_prs_opened",
        ):
            assert key in empty, f"metrics envelope missing {key!r}"
        assert empty["total_incidents"] == 0
        assert empty["success_rate"] == 0.0
        assert empty["avg_resolution_seconds"] == 0.0
        assert isinstance(empty["by_classification"], dict)
        for bucket in ("null_deref", "recent_regression", "config_error", "unknown"):
            assert bucket in empty["by_classification"]
            assert empty["by_classification"][bucket] == 0

        # Seed three rows representing each terminal bucket.
        with Session(get_engine()) as session:
            now = datetime.now(timezone.utc)

            resolved = Incident(
                source_service="victim_app",
                endpoint="POST /shorten",
                exception_class="AttributeError",
                exception_message="'NoneType' object has no attribute 'strip'",
                traceback="…",
                request_body="{}",
                raw_payload="{}",
                status="released",
                classification="null_deref",
                severity="P1",
                pr_url="https://github.com/example/repo/pull/42",
                pr_number=42,
                created_at=now - timedelta(seconds=30),
                updated_at=now,
            )
            session.add(resolved)
            session.commit()
            session.refresh(resolved)

            session.add(
                AgentRun(
                    incident_id=resolved.id,
                    agent_name="releaser",
                    status="success",
                    started_at=now - timedelta(seconds=5),
                    finished_at=now,
                )
            )

            escalated = Incident(
                source_service="victim_app",
                endpoint="GET /healthcheck",
                exception_class="RuntimeError",
                exception_message="Event loop is closed",
                traceback="…",
                request_body="{}",
                raw_payload="{}",
                status="escalated",
                classification="unknown",
                severity="P2",
            )
            session.add(escalated)

            in_flight = Incident(
                source_service="victim_app",
                endpoint="POST /shorten",
                exception_class="AttributeError",
                exception_message="...",
                traceback="…",
                request_body="{}",
                raw_payload="{}",
                status="investigating",
                classification="null_deref",
                severity="P1",
            )
            session.add(in_flight)
            session.commit()

        resp = client.get("/api/metrics")
        assert resp.status_code == 200, resp.text
        m = resp.json()

        assert m["total_incidents"] == 3
        assert m["resolved_count"] == 1
        assert m["escalated_count"] == 1
        assert m["in_progress_count"] == 1
        assert m["failed_count"] == 0
        assert m["total_prs_opened"] == 1
        # 1 resolved out of (1 resolved + 1 escalated + 0 failed) = 0.5.
        assert abs(m["success_rate"] - 0.5) < 1e-6
        # Resolution duration should be around 30s (created 30s ago,
        # last AgentRun finished "now"). Allow generous slack.
        assert 5.0 < m["avg_resolution_seconds"] < 120.0
        assert m["by_classification"]["null_deref"] == 2
        assert m["by_classification"]["unknown"] == 1
        assert m["by_classification"]["recent_regression"] == 0
        assert m["by_classification"]["config_error"] == 0
