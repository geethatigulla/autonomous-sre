"""Top-level smoke tests for the orchestrator.

Fast sanity checks that the package imports, the app boots with no
credentials at all, and the liveness / readiness probes behave.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


MODULES = [
    "orchestrator.main",
    "orchestrator.config",
    "orchestrator.db",
    "orchestrator.models",
    "orchestrator.tracing",
    "orchestrator.timeutil",
    "orchestrator.agents.triager",
    "orchestrator.agents.investigator",
    "orchestrator.agents.fix_author",
    "orchestrator.agents.verifier",
    "orchestrator.agents.releaser",
    "orchestrator.orchestration.state_machine",
    "orchestrator.orchestration.parallel_fix",
    "orchestrator.orchestration.job_queue",
    "orchestrator.tools.github_tool",
    "orchestrator.tools.search_tool",
    "orchestrator.tools.sandbox_tool",
    "orchestrator.tools.patch_tool",
    "orchestrator.tools.slack_tool",
    "orchestrator.webhooks.ingress",
    "orchestrator.dashboard.routes",
    "orchestrator.dashboard.events",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    importlib.import_module(name)


@pytest.fixture
def bare_app(tmp_path, monkeypatch):
    """App with a temp DB and *no* credentials configured."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sre.db'}")
    monkeypatch.setenv("LOCAL_TRACE_PATH", str(tmp_path / "traces.jsonl"))
    monkeypatch.setenv("TRACE_BACKEND", "local")
    for var in ("OPENAI_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO", "TAVILY_API_KEY",
                "SLACK_WEBHOOK_URL", "OMIUM_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    from orchestrator import config, db, tracing

    config.get_settings.cache_clear()
    db.reset_engine()
    tracing.reset_tracer()

    from orchestrator.main import app

    yield app

    config.get_settings.cache_clear()
    db.reset_engine()
    tracing.reset_tracer()


def test_app_boots_without_credentials(bare_app) -> None:
    with TestClient(bare_app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_ready_reports_missing_config(bare_app) -> None:
    with TestClient(bare_app) as client:
        resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["database"]["ok"] is True
    assert set(body["missing_required"]) == {"OPENAI_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO"}
    # Optional integrations are reported but never block readiness.
    assert body["config"]["SLACK_WEBHOOK_URL"]["required"] is False
    assert "queue" in body


def test_ready_ok_when_configured(bare_app, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("GITHUB_REPO", "someone/victim")
    from orchestrator import config

    config.get_settings.cache_clear()
    with TestClient(bare_app) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    # Secret values must never be echoed back.
    assert "sk-test" not in resp.text and "ghp_test" not in resp.text


def test_placeholder_repo_counts_as_missing(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    from orchestrator.config import Settings

    assert "GITHUB_REPO" in Settings().missing_required()


def test_alert_without_openai_key_fails_cleanly(bare_app) -> None:
    """With no OPENAI_API_KEY the incident is recorded and ends ``failed``."""
    payload = {
        "source_service": "victim_app",
        "endpoint": "POST /shorten",
        "exception_class": "AttributeError",
        "exception_message": "'NoneType' object has no attribute 'strip'",
        "traceback": "Traceback (most recent call last): ...",
        "request_body": '{"url": null}',
    }
    with TestClient(bare_app) as client:
        resp = client.post("/webhooks/alert", json=payload)
        assert resp.status_code == 202
        incident_id = resp.json()["incident_id"]
        detail = client.get(f"/webhooks/alert/{incident_id}").json()
    assert detail["incident"]["status"] == "failed"
