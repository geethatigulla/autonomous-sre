"""Shared pytest setup.

Runs before any test module imports ``orchestrator``, so it can make the
whole suite hermetic:

* ``SRE_ENV_FILE=""`` stops pydantic-settings from reading a developer's
  real ``.env`` — otherwise a test that forgets to blank a key could make
  real OpenAI / GitHub / Slack calls.
* Tracing defaults to local-only so no test ships spans to Omium unless it
  opts in explicitly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["SRE_ENV_FILE"] = ""
os.environ.setdefault("TRACE_BACKEND", "local")


@pytest.fixture(autouse=True)
def _reset_job_queue():
    """Keep the in-process dispatcher's state from leaking between tests."""
    from orchestrator.orchestration import job_queue

    job_queue._reset_for_tests()
    yield
    job_queue._reset_for_tests()
