"""Tests for the in-process incident dispatcher."""

from __future__ import annotations

import threading
import time

import pytest
from sqlmodel import Session, select


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sre.db'}")
    from orchestrator import config, db

    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()
    yield db.get_engine()
    config.get_settings.cache_clear()
    db.reset_engine()


def _add_incident(engine, status: str) -> str:
    from orchestrator.models import Incident

    with Session(engine) as session:
        inc = Incident(
            source_service="victim_app",
            endpoint="POST /shorten",
            exception_class="AttributeError",
            exception_message="boom",
            traceback="",
            request_body="",
            raw_payload="{}",
            status=status,
        )
        session.add(inc)
        session.commit()
        return inc.id


def test_is_terminal() -> None:
    from orchestrator.orchestration.job_queue import is_terminal

    for status in ("released", "escalated", "failed", "release_failed", "fix_rejected"):
        assert is_terminal(status)
    for status in ("received", "classified", "investigated", "verified", "", None):
        assert not is_terminal(status)


def test_recover_orphaned_incidents(temp_db) -> None:
    from orchestrator.models import AgentRun, Incident
    from orchestrator.orchestration.job_queue import recover_orphaned_incidents

    stuck = _add_incident(temp_db, "investigated")
    done = _add_incident(temp_db, "released")
    with Session(temp_db) as session:
        session.add(AgentRun(incident_id=stuck, agent_name="investigator", status="running"))
        session.commit()

    assert recover_orphaned_incidents() == 1

    with Session(temp_db) as session:
        assert session.get(Incident, stuck).status == "failed"
        assert session.get(Incident, done).status == "released"
        run = session.exec(select(AgentRun).where(AgentRun.incident_id == stuck)).one()
        assert run.status == "error"
        assert run.finished_at is not None
        assert "restarted" in run.error_message


def test_dispatch_skips_duplicate_and_limits_concurrency(monkeypatch) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_INCIDENTS", "1")
    from orchestrator import config
    from orchestrator.orchestration import job_queue, state_machine

    config.get_settings.cache_clear()
    job_queue._reset_for_tests()

    running = 0
    peak = 0
    calls: list[str] = []
    guard = threading.Lock()

    async def fake_handle(incident_id: str) -> None:
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)
            calls.append(incident_id)
        time.sleep(0.05)
        with guard:
            running -= 1

    monkeypatch.setattr(state_machine, "handle_incident", fake_handle)

    threads = [
        threading.Thread(target=job_queue.dispatch_incident, args=(iid,))
        for iid in ("a", "b", "c")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(calls) == ["a", "b", "c"]
    assert peak == 1
    assert job_queue.in_flight() == []
    config.get_settings.cache_clear()


def test_dispatch_swallows_errors_and_marks_failed(temp_db, monkeypatch) -> None:
    from orchestrator.models import Incident
    from orchestrator.orchestration import job_queue, state_machine

    incident_id = _add_incident(temp_db, "classified")

    async def boom(_incident_id: str) -> None:
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(state_machine, "handle_incident", boom)

    assert job_queue.dispatch_incident(incident_id) is True
    with Session(temp_db) as session:
        assert session.get(Incident, incident_id).status == "failed"
