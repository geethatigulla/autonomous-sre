"""Test suite for the victim app — exercised by the verifier agent in a sandbox.

The Verifier runs these tests against whatever BUG_MODE was set when the app
process booted. In BUG_MODE=none everything passes; under each seeded bug a
specific test fails in a specific way, which is how the Verifier confirms
both the diagnosis and the eventual fix.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make the victim_app package importable when pytest is invoked from the repo
# root. We deliberately import app.py and monitor.py as top-level modules
# because that's how the Dockerfile loads them (uvicorn app:app).
VICTIM_DIR = Path(__file__).resolve().parent.parent
if str(VICTIM_DIR) not in sys.path:
    sys.path.insert(0, str(VICTIM_DIR))


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Re-import app fresh so it picks up whatever BUG_MODE the runner set.
    if "app" in sys.modules:
        del sys.modules["app"]
    if "monitor" in sys.modules:
        del sys.modules["monitor"]
    import app as victim_app  # noqa: WPS433 — intentional late import

    return TestClient(victim_app.app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def bug_mode() -> str:
    return os.environ.get("BUG_MODE", "none")


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_shorten_valid(client: TestClient) -> None:
    # Asserts the CORRECT behavior. Under BUG_MODE=regression the response
    # uses "result" instead of "short", so this assertion fails — that's the
    # signal the Verifier needs to confirm the regression. Once a patch
    # restores the "short" key, the test passes again.
    resp = client.post("/shorten", json={"url": "https://example.com/foo"})
    assert resp.status_code == 200
    body = resp.json()
    assert "short" in body, f"expected 'short' key in response, got {body}"
    assert len(body["short"]) == 6


def test_shorten_null(client: TestClient) -> None:
    # Asserts the CORRECT behavior: a null URL is rejected with a 422
    # validation error. Under BUG_MODE=null_deref the handler crashes with a
    # 500 instead — that failure is exactly what the Verifier looks for.
    resp = client.post("/shorten", json={"url": None})
    assert resp.status_code == 422, (
        f"expected 422 for null url, got {resp.status_code}: {resp.text}"
    )


def test_expand_unknown(client: TestClient) -> None:
    resp = client.get("/expand/zzzzzz")
    assert resp.status_code == 404
