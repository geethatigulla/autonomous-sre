"""End-to-end Investigator tests with fakes for OpenAI / GitHub / Tavily.

Each test exercises the state machine through ``TestClient`` (so the
real :func:`handle_incident` wiring runs end-to-end) but with three
swappable fakes:

* ``ScriptedOpenAI`` returns model-specific responses. The Triager
  always sees a tool-less JSON classification; the Investigator sees a
  scripted sequence of tool calls followed by a final JSON answer.
* ``FakeGitHubTool`` answers ``read_file`` / ``list_recent_commits``
  with hard-coded victim_app contents — no network.
* ``FakeWebSearchTool`` is a no-op that returns an empty list (the
  Investigator's system prompt is told ``web_search`` is disabled when
  it's a no-op).

The state machine resolves ``GitHubTool`` and ``WebSearchTool`` through
module-level references, so we monkeypatch those to point at our
factories. This is the same pattern the existing Triager test uses for
``_build_openai_client``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeToolCallFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id: str, name: str, arguments: dict) -> None:
        self.id = id
        self.type = "function"
        self.function = _FakeToolCallFunction(name, json.dumps(arguments))


class _FakeMessage:
    def __init__(self, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or None


class _FakeChoice:
    def __init__(self, message: _FakeMessage, finish_reason: str = "stop") -> None:
        self.message = message
        self.finish_reason = finish_reason


class _FakeUsage:
    total_tokens = 123


class _FakeResponse:
    def __init__(self, message: _FakeMessage) -> None:
        self.choices = [_FakeChoice(message)]
        self.usage = _FakeUsage()


class _ScriptedCompletions:
    def __init__(self, owner: "ScriptedOpenAI") -> None:
        self._owner = owner

    async def create(self, **kwargs):  # noqa: D401 — fake
        return self._owner._respond(kwargs)


class _Chat:
    def __init__(self, owner: "ScriptedOpenAI") -> None:
        self.completions = _ScriptedCompletions(owner)


class ScriptedOpenAI:
    """Returns model-specific scripted messages.

    ``triager_payload`` is the JSON dict the Triager should see. The
    Investigator's script is a list of :class:`_FakeMessage` values
    consumed in order; the last entry is repeated if the Investigator
    asks for more turns than we scripted.
    """

    def __init__(
        self,
        *,
        triager_payload: dict,
        investigator_messages: list[_FakeMessage],
    ) -> None:
        self._triager_payload = triager_payload
        self._inv_script = list(investigator_messages)
        self.triager_calls = 0
        self.investigator_calls = 0
        self.chat = _Chat(self)

    def _respond(self, kwargs: dict) -> _FakeResponse:
        model = kwargs.get("model", "")
        # The Triager uses ``response_format`` JSON mode and no tools;
        # the Investigator uses tool calling.
        if "tools" in kwargs and kwargs["tools"]:
            # Investigator call.
            idx = min(self.investigator_calls, len(self._inv_script) - 1)
            self.investigator_calls += 1
            return _FakeResponse(self._inv_script[idx])
        # Triager call.
        self.triager_calls += 1
        return _FakeResponse(
            _FakeMessage(content=json.dumps(self._triager_payload))
        )


# ---- GitHub / search fakes -------------------------------------------------


_VICTIM_APP_PY = (
    "import os\n"
    "from fastapi import FastAPI, HTTPException\n"
    "from pydantic import BaseModel\n"
    "\n"
    "BUG_MODE = os.environ.get('BUG_MODE', 'none')\n"
    "app = FastAPI()\n"
    "\n"
    "class ShortenRequest(BaseModel):\n"
    "    url: str | None = None\n"
    "\n"
    "@app.post('/shorten')\n"
    "async def shorten(payload: ShortenRequest) -> dict:\n"
    "    url = payload.url\n"
    "    if BUG_MODE == 'null_deref':\n"
    "        normalized = url.strip().lower()  # <-- no None-check\n"
    "        return {'short': normalized[:6]}\n"
    "    if url is None:\n"
    "        raise HTTPException(status_code=422, detail='url is required')\n"
    "    return {'short': url[:6]}\n"
)


class FakeGitHubTool:
    """Stand-in for :class:`GitHubTool` that never hits the network.

    The ``existing_paths`` set models which paths actually live in the
    repo. ``read_file`` and ``_resolve_path`` honour it so tests can
    simulate the production "victim_app/app.py vs app.py" mismatch.
    """

    def __init__(
        self,
        *,
        disabled: bool = False,
        existing_paths: set[str] | None = None,
    ) -> None:
        self.disabled = disabled
        self.read_calls: list[tuple[str, str]] = []
        self.commit_calls: list[tuple[Any, int]] = []
        # Default mirrors the historical FakeGitHubTool behaviour: any
        # path ending in app.py reads back the victim_app source. Tests
        # that care about path resolution pass an explicit set.
        self.existing_paths = (
            set(existing_paths) if existing_paths is not None else None
        )
        self.last_resolved_path: str | None = None

    def _path_exists(self, path: str) -> bool:
        if self.existing_paths is None:
            return path.endswith("app.py")
        return path in self.existing_paths

    async def _resolve_path(
        self,
        requested_path: str,
        ref: str = "main",
        span: Any = None,
    ) -> str | None:
        if self.disabled or not requested_path:
            return None
        basename = requested_path.rsplit("/", 1)[-1]
        candidates = [requested_path]
        if basename and basename != requested_path:
            candidates.append(basename)
        for prefix in ("victim_app", "src", "app"):
            alt = f"{prefix}/{basename}"
            if alt not in candidates:
                candidates.append(alt)
        for candidate in candidates:
            if self._path_exists(candidate):
                return candidate
        return None

    async def read_file(self, path: str, ref: str = "main", span: Any = None) -> str:
        self.read_calls.append((path, ref))
        if self.disabled:
            self.last_resolved_path = None
            return ""
        resolved = await self._resolve_path(path, ref=ref, span=span)
        if resolved is None:
            self.last_resolved_path = None
            return ""
        # Open child span so the trace tree records the tool call.
        if span is not None:
            try:
                with span.child_span(
                    "github.read_file",
                    metadata={"path": path, "ref": ref},
                ) as ch:
                    ch.event(
                        "output",
                        {"chars": len(_VICTIM_APP_PY), "resolved_path": resolved},
                    )
            except Exception:
                pass
        self.last_resolved_path = resolved
        if resolved.endswith("app.py"):
            return _VICTIM_APP_PY
        return ""

    async def read_file_at_lines(
        self,
        path: str,
        start: int,
        end: int,
        ref: str = "main",
        span: Any = None,
    ) -> str:
        text = await self.read_file(path, ref=ref, span=span)
        if not text:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[max(0, start - 1) : end])

    async def list_recent_commits(
        self,
        path: str | None = None,
        limit: int = 10,
        span: Any = None,
    ) -> list[dict]:
        self.commit_calls.append((path, limit))
        if self.disabled:
            return []
        if span is not None:
            try:
                with span.child_span(
                    "github.list_recent_commits",
                    metadata={"path": path, "limit": limit},
                ) as ch:
                    ch.event("output", {"count": 1})
            except Exception:
                pass
        return [
            {
                "sha": "abc1234",
                "author": "Alice",
                "date": "2026-05-10T12:00:00",
                "message": "Add null_deref BUG_MODE branch (intentional bug)",
            }
        ]

    async def get_commit_diff(self, sha: str, span: Any = None) -> dict:
        return {"sha": sha, "message": "", "files": []}

    async def list_recent_prs(self, limit: int = 5, span: Any = None) -> list[dict]:
        return []


class FakeWebSearchTool:
    """No-op search; matches WebSearchTool's `.enabled` contract."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.queries: list[str] = []

    async def search(
        self,
        query: str,
        max_results: int = 5,
        span: Any = None,
    ) -> list[dict]:
        self.queries.append(query)
        if span is not None:
            try:
                with span.child_span(
                    "tavily.search", metadata={"query": query[:100]}
                ) as ch:
                    ch.event("search_disabled", {})
            except Exception:
                pass
        return []


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Fresh DB, local-only tracing, clean singletons."""
    db_path = tmp_path / "sre.db"
    trace_path = tmp_path / "local_traces.jsonl"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("TRACE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_TRACE_PATH", str(trace_path))
    monkeypatch.delenv("OMIUM_API_KEY", raising=False)

    from orchestrator import config as config_module
    from orchestrator import db as db_module
    from orchestrator import tracing as tracing_module

    config_module.get_settings.cache_clear()
    db_module.reset_engine()
    tracing_module.reset_tracer()

    yield trace_path

    config_module.get_settings.cache_clear()
    db_module.reset_engine()
    tracing_module.reset_tracer()


def _install_state_machine_fakes(
    monkeypatch,
    *,
    scripted: ScriptedOpenAI,
    github: FakeGitHubTool,
    search: FakeWebSearchTool,
) -> None:
    """Wire the fakes into the state_machine module."""
    from orchestrator.orchestration import state_machine

    monkeypatch.setattr(
        state_machine, "_build_openai_client", lambda: scripted
    )
    monkeypatch.setattr(
        state_machine, "GitHubTool", lambda *a, **kw: github
    )
    monkeypatch.setattr(
        state_machine, "WebSearchTool", lambda *a, **kw: search
    )


_NULL_DEREF_PAYLOAD = {
    "endpoint": "POST /shorten",
    "method": "POST",
    "exception_class": "AttributeError",
    "exception_message": "'NoneType' object has no attribute 'strip'",
    "traceback": (
        "Traceback (most recent call last):\n"
        '  File "victim_app/app.py", line 76, in shorten\n'
        "    normalized = url.strip().lower()\n"
        "AttributeError: 'NoneType' object has no attribute 'strip'"
    ),
    "request_body": '{"url": null}',
    "source_service": "victim_app",
}

_TRIAGER_PAYLOAD = {
    "classification": "null_deref",
    "severity": "P1",
    "reasoning": "AttributeError on NoneType — handler did not null-check url.",
    "confidence": 0.92,
}


def _poll_until(client, incident_id: str, target_statuses: set[str], timeout_s: float = 20.0) -> dict:
    """Poll the detail endpoint until status is in ``target_statuses`` or timeout."""
    deadline = time.monotonic() + timeout_s
    detail: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/webhooks/alert/{incident_id}")
        detail = resp.json()
        status = detail.get("incident", {}).get("status")
        if status in target_statuses:
            return detail
        time.sleep(0.1)
    return detail


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_investigator_finds_null_deref(isolated_env, monkeypatch):
    """Happy path: Investigator reads app.py and emits a real hypothesis."""
    trace_path = isolated_env

    # Investigator script: turn 1 reads app.py, turn 2 emits final JSON.
    inv_messages = [
        _FakeMessage(
            content="",
            tool_calls=[
                _FakeToolCall(
                    id="call_1",
                    name="github_read_file",
                    arguments={"path": "victim_app/app.py", "ref": "main"},
                )
            ],
        ),
        _FakeMessage(
            content=json.dumps(
                {
                    "suspect_file": "victim_app/app.py",
                    "suspect_line": 76,
                    "suspect_function": "shorten",
                    "root_cause": (
                        "The /shorten handler dereferences url.strip() without "
                        "checking for None, so {\"url\": null} crashes."
                    ),
                    "hypotheses": [
                        {
                            "description": "Missing null guard on payload.url",
                            "evidence": (
                                "app.py:76 calls url.strip().lower() with no "
                                "None check; traceback confirms NoneType."
                            ),
                            "fix_strategy": (
                                "Raise HTTPException(422) when url is None "
                                "before calling .strip()."
                            ),
                            "confidence": 0.95,
                        }
                    ],
                    "investigation_log": [
                        "step 1: identified app.py:76 from traceback",
                        "step 2: read app.py and confirmed missing null guard",
                    ],
                }
            ),
        ),
    ]

    scripted = ScriptedOpenAI(
        triager_payload=_TRIAGER_PAYLOAD,
        investigator_messages=inv_messages,
    )
    github = FakeGitHubTool(disabled=False)
    search = FakeWebSearchTool(enabled=False)
    _install_state_machine_fakes(
        monkeypatch, scripted=scripted, github=github, search=search
    )

    from fastapi.testclient import TestClient
    from orchestrator.main import app

    with TestClient(app) as client:
        resp = client.post("/webhooks/alert", json=_NULL_DEREF_PAYLOAD)
        assert resp.status_code == 202, resp.text
        incident_id = resp.json()["incident_id"]

        detail = _poll_until(
            client,
            incident_id,
            target_statuses={
                "pending_fix",
                "fix_drafted",
                "fix_failed",
                "fix_rejected",
                "verified",
                "verification_failed",
                "pending_release",
                "investigation_failed",
            },
            timeout_s=20.0,
        )

    incident = detail["incident"]
    # The Investigator finishes cleanly here; the Fix Author then runs
    # with the same scripted client that only knows how to return a
    # Triager-shaped JSON for non-tool requests, so its diff check
    # fails and the state machine surfaces ``fix_failed``. This test
    # is asserting the Investigator's behaviour, so the precise
    # post-investigator terminal status is acceptable as long as it's
    # downstream-reachable.
    assert incident["status"] in {
        "pending_fix",
        "fix_drafted",
        "fix_failed",
        "fix_rejected",
        "verified",
        "pending_release",
        "verification_failed",
    }, incident
    assert incident["suspect_file"], "suspect_file should be populated"
    assert "app.py" in incident["suspect_file"]
    assert incident["root_cause"], "root_cause should be populated"
    assert incident["suspect_line"] == 76

    parsed = json.loads(incident["investigation_result"])
    hyps = parsed.get("hypotheses") or []
    assert hyps, "hypotheses list must not be empty"
    assert any(h.get("confidence", 0.0) > 0.3 for h in hyps), hyps

    # AgentRun assertions
    runs = detail["agent_runs"]
    inv_runs = [r for r in runs if r["agent_name"] == "investigator"]
    assert len(inv_runs) == 1
    assert inv_runs[0]["status"] == "success"
    assert inv_runs[0]["output_summary"]

    # FakeGitHubTool was actually consulted
    assert github.read_calls, "github.read_file should have been called"


def test_investigator_caps_tool_calls(isolated_env, monkeypatch):
    """If the model never emits a final answer, agent must stop at 5 tool calls."""
    trace_path = isolated_env

    # Always return the same tool call — the agent must hit the cap and
    # then force a final turn (tool_choice="none"), which we answer with
    # a degenerate JSON.
    looping_tool_call = _FakeMessage(
        content="",
        tool_calls=[
            _FakeToolCall(
                id="call_loop",
                name="github_read_file",
                arguments={"path": "victim_app/app.py"},
            )
        ],
    )
    final_after_budget = _FakeMessage(
        content=json.dumps(
            {
                "suspect_file": "victim_app/app.py",
                "suspect_line": None,
                "root_cause": "budget exhausted; degenerate fallback",
                "hypotheses": [
                    {
                        "description": "Unverified",
                        "evidence": "no analysis completed",
                        "fix_strategy": "manual review",
                        "confidence": 0.1,
                    }
                ],
                "investigation_log": ["budget exhausted"],
            }
        )
    )

    # Lots of looping messages, then the final one. The Investigator
    # may call the model up to (max_tool_calls + 2) times; pad
    # generously so the script never runs out before the cap fires.
    inv_messages = [looping_tool_call] * 10 + [final_after_budget]

    scripted = ScriptedOpenAI(
        triager_payload=_TRIAGER_PAYLOAD,
        investigator_messages=inv_messages,
    )
    github = FakeGitHubTool(disabled=False)
    search = FakeWebSearchTool(enabled=False)
    _install_state_machine_fakes(
        monkeypatch, scripted=scripted, github=github, search=search
    )

    from fastapi.testclient import TestClient
    from orchestrator.main import app

    with TestClient(app) as client:
        resp = client.post("/webhooks/alert", json=_NULL_DEREF_PAYLOAD)
        assert resp.status_code == 202
        incident_id = resp.json()["incident_id"]

        detail = _poll_until(
            client,
            incident_id,
            target_statuses={
                "pending_fix",
                "fix_drafted",
                "fix_failed",
                "fix_rejected",
                "verified",
                "verification_failed",
                "pending_release",
                "investigation_failed",
            },
            timeout_s=20.0,
        )

    incident = detail["incident"]
    # Even with a degenerate loop, the workflow should still finish.
    assert incident["status"] in {
        "pending_fix",
        "fix_drafted",
        "fix_failed",
        "fix_rejected",
        "verified",
        "pending_release",
        "verification_failed",
        "investigation_failed",
    }, incident
    # The Investigator must have stopped at exactly 5 budget calls.
    # Downstream agents (Fix Author) read the file independently with
    # the same fake instance, so the total may include a few extras —
    # we only care that the *investigator-phase* reads (all directed at
    # the path the looping tool call asked for) hit the cap exactly.
    investigator_reads = [
        c for c in github.read_calls if c[0] == "victim_app/app.py"
    ]
    assert len(investigator_reads) == 5, (
        f"expected 5 investigator-phase tool calls, got "
        f"{len(investigator_reads)} (total reads: {len(github.read_calls)})"
    )
    # And it must have produced *some* JSON we could persist.
    assert incident["investigation_result"], incident
    parsed = json.loads(incident["investigation_result"])
    assert parsed.get("hypotheses"), "hypotheses must not be empty even on fallback"


def test_investigator_handles_github_disabled(isolated_env, monkeypatch):
    """No GitHub token / no Tavily → agent still emits a hypothesis."""
    trace_path = isolated_env

    # Investigator answers in one turn (no tool calls).
    inv_messages = [
        _FakeMessage(
            content=json.dumps(
                {
                    "suspect_file": "victim_app/app.py",
                    "suspect_line": None,
                    "root_cause": (
                        "AttributeError on NoneType in /shorten — likely a "
                        "missing null guard on the url field."
                    ),
                    "hypotheses": [
                        {
                            "description": (
                                "url is None and .strip() is called on it"
                            ),
                            "evidence": (
                                "traceback shows AttributeError on NoneType.strip"
                            ),
                            "fix_strategy": "add None check before .strip()",
                            "confidence": 0.4,
                        }
                    ],
                    "investigation_log": [
                        "no tools available — reasoned from traceback only"
                    ],
                }
            ),
        ),
    ]

    scripted = ScriptedOpenAI(
        triager_payload=_TRIAGER_PAYLOAD,
        investigator_messages=inv_messages,
    )
    # disabled GitHub + disabled search.
    github = FakeGitHubTool(disabled=True)
    search = FakeWebSearchTool(enabled=False)
    _install_state_machine_fakes(
        monkeypatch, scripted=scripted, github=github, search=search
    )

    from fastapi.testclient import TestClient
    from orchestrator.main import app

    with TestClient(app) as client:
        resp = client.post("/webhooks/alert", json=_NULL_DEREF_PAYLOAD)
        assert resp.status_code == 202
        incident_id = resp.json()["incident_id"]

        detail = _poll_until(
            client,
            incident_id,
            target_statuses={
                "pending_fix",
                "fix_drafted",
                "fix_failed",
                "fix_rejected",
                "verified",
                "verification_failed",
                "pending_release",
                "investigation_failed",
            },
            timeout_s=20.0,
        )

    incident = detail["incident"]
    # The Investigator finishes cleanly here; the Fix Author then runs
    # with the same scripted client that only knows how to return a
    # Triager-shaped JSON for non-tool requests, so its diff check
    # fails and the state machine surfaces ``fix_failed``. This test
    # is asserting the Investigator's behaviour, so the precise
    # post-investigator terminal status is acceptable as long as it's
    # downstream-reachable.
    assert incident["status"] in {
        "pending_fix",
        "fix_drafted",
        "fix_failed",
        "fix_rejected",
        "verified",
        "pending_release",
        "verification_failed",
    }, incident
    parsed = json.loads(incident["investigation_result"])
    assert parsed.get("hypotheses"), parsed
    # Some hypothesis must exist (low confidence is fine).
    assert parsed["hypotheses"][0]["description"], parsed


def test_investigator_spans_nested_correctly(isolated_env, monkeypatch):
    """Verify the trace tree: investigator and triager are siblings; tools nest under investigator."""
    trace_path = isolated_env

    inv_messages = [
        _FakeMessage(
            content="",
            tool_calls=[
                _FakeToolCall(
                    id="call_1",
                    name="github_read_file",
                    arguments={"path": "victim_app/app.py"},
                )
            ],
        ),
        _FakeMessage(
            content=json.dumps(
                {
                    "suspect_file": "victim_app/app.py",
                    "suspect_line": 76,
                    "root_cause": "missing null guard",
                    "hypotheses": [
                        {
                            "description": "x",
                            "evidence": "y",
                            "fix_strategy": "z",
                            "confidence": 0.8,
                        }
                    ],
                    "investigation_log": ["read app.py"],
                }
            ),
        ),
    ]

    scripted = ScriptedOpenAI(
        triager_payload=_TRIAGER_PAYLOAD,
        investigator_messages=inv_messages,
    )
    github = FakeGitHubTool(disabled=False)
    search = FakeWebSearchTool(enabled=False)
    _install_state_machine_fakes(
        monkeypatch, scripted=scripted, github=github, search=search
    )

    from fastapi.testclient import TestClient
    from orchestrator.main import app

    with TestClient(app) as client:
        resp = client.post("/webhooks/alert", json=_NULL_DEREF_PAYLOAD)
        assert resp.status_code == 202
        incident_id = resp.json()["incident_id"]

        detail = _poll_until(
            client,
            incident_id,
            target_statuses={
                "pending_fix",
                "fix_drafted",
                "fix_failed",
                "fix_rejected",
                "verified",
                "verification_failed",
                "pending_release",
                "investigation_failed",
            },
            timeout_s=20.0,
        )

    # Post-investigator terminal status — the Fix Author will be
    # invoked next but the scripted OpenAI doesn't return a valid diff
    # for it, so the pipeline lands at ``fix_failed`` (or one of the
    # other downstream terminal states if anything else changes).
    assert detail["incident"]["status"] in {
        "pending_fix",
        "fix_drafted",
        "fix_failed",
        "fix_rejected",
        "verified",
        "pending_release",
        "verification_failed",
    }
    workflow_id = detail["incident"]["workflow_id"]
    assert workflow_id

    # Load all local trace records for this workflow.
    assert trace_path.exists(), "local trace file should exist"
    records = [
        json.loads(line)
        for line in trace_path.read_text().splitlines()
        if line.strip()
    ]
    starts = [
        r for r in records
        if r.get("kind") == "span_start" and r.get("workflow_id") == workflow_id
    ]

    by_name = {r["name"]: r for r in starts}
    assert "triager.classify" in by_name, by_name
    assert "investigator.investigate" in by_name, by_name

    triager_start = by_name["triager.classify"]
    investigator_start = by_name["investigator.investigate"]

    # Siblings: both have parent_id == workflow_id.
    assert triager_start["parent_id"] == workflow_id
    assert investigator_start["parent_id"] == workflow_id

    # tool.github_read_file is a child of investigator.investigate.
    tool_spans = [
        r for r in starts if r["name"].startswith("tool.")
    ]
    assert tool_spans, "expected at least one tool.* span"
    for ts in tool_spans:
        assert ts["parent_id"] == investigator_start["span_id"], (
            f"tool span {ts['name']} parent should be investigator.investigate, "
            f"got {ts['parent_id']}"
        )

    # Fix Author span follows the investigator and is a sibling of
    # triager.classify. The old ``pipeline.null_deref.fix_stub`` was
    # replaced with the real ``fix_author.draft`` span in this prompt.
    if "fix_author.draft" in by_name:
        assert by_name["fix_author.draft"]["parent_id"] == workflow_id


def test_investigator_resolves_paths_with_wrong_prefix(isolated_env, monkeypatch):
    """LLM emits suspect_file='victim_app/app.py' but the repo only has
    'app.py' at the root. Investigator must resolve to the actual path
    before persisting, so the Fix Author downstream isn't handed a 404."""
    trace_path = isolated_env

    # FakeGitHubTool: only 'app.py' at the root exists. Any prefixed
    # path (e.g. 'victim_app/app.py') 404s. The resolver must fall back
    # to the basename.
    github = FakeGitHubTool(disabled=False, existing_paths={"app.py"})
    search = FakeWebSearchTool(enabled=False)

    inv_messages = [
        _FakeMessage(
            content="",
            tool_calls=[
                _FakeToolCall(
                    id="call_1",
                    name="github_read_file",
                    arguments={"path": "victim_app/app.py", "ref": "main"},
                )
            ],
        ),
        _FakeMessage(
            content=json.dumps(
                {
                    # The LLM emits the wrong prefix on purpose — we
                    # want the agent to resolve and rewrite it.
                    "suspect_file": "victim_app/app.py",
                    "suspect_line": 76,
                    "suspect_function": "shorten",
                    "root_cause": "Missing null guard on payload.url.",
                    "hypotheses": [
                        {
                            "description": "url is None and .strip() is called on it",
                            "evidence": "AttributeError on NoneType.strip",
                            "fix_strategy": "Raise HTTPException(422) when url is None",
                            "confidence": 0.9,
                        }
                    ],
                    "investigation_log": [
                        "step 1: read app.py",
                        "step 2: confirmed missing guard",
                    ],
                }
            ),
        ),
    ]

    scripted = ScriptedOpenAI(
        triager_payload=_TRIAGER_PAYLOAD,
        investigator_messages=inv_messages,
    )
    _install_state_machine_fakes(
        monkeypatch, scripted=scripted, github=github, search=search
    )

    from fastapi.testclient import TestClient
    from orchestrator.main import app

    with TestClient(app) as client:
        resp = client.post("/webhooks/alert", json=_NULL_DEREF_PAYLOAD)
        assert resp.status_code == 202, resp.text
        incident_id = resp.json()["incident_id"]

        detail = _poll_until(
            client,
            incident_id,
            target_statuses={
                "pending_fix",
                "fix_drafted",
                "fix_failed",
                "fix_rejected",
                "verified",
                "verification_failed",
                "pending_release",
                "investigation_failed",
            },
            timeout_s=20.0,
        )

    incident = detail["incident"]
    # The crux of this test: the persisted suspect_file must be the
    # path that actually exists in the repo, not the prefixed string
    # the LLM emitted.
    assert incident["suspect_file"] == "app.py", incident
    parsed = json.loads(incident["investigation_result"])
    assert parsed.get("suspect_file") == "app.py", parsed
    # Top hypothesis confidence must survive resolution (we only knock
    # it down when resolution fails).
    hyps = parsed.get("hypotheses") or []
    assert hyps and hyps[0]["confidence"] >= 0.5, hyps
