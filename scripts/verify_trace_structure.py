"""In-process end-to-end verification of the webhook -> Triager pipeline.

Runs the same flow the test does but prints a human-readable summary of:

* the persisted incident (status, classification, workflow_id),
* every AgentRun row,
* the parent-id chain in the local JSONL trace.

Useful when you want to eyeball the trace tree without spinning up uvicorn.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _FakeUsage:
    total_tokens = 73


class _FakeMsg:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMsg(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def create(self, **kwargs) -> _FakeResponse:
        return _FakeResponse(json.dumps(self._payload))


class _FakeChat:
    def __init__(self, payload: dict) -> None:
        self.completions = _FakeCompletions(payload)


class _FakeOpenAI:
    def __init__(self, payload: dict) -> None:
        self.chat = _FakeChat(payload)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "sre.db"
        trace_path = Path(tmp) / "local_traces.jsonl"

        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
        os.environ["TRACE_BACKEND"] = "local"
        os.environ["LOCAL_TRACE_PATH"] = str(trace_path)
        os.environ.pop("OMIUM_API_KEY", None)

        # Reset cached singletons so the fresh env vars take effect.
        from orchestrator import config as cfg, db as dbm, tracing as trc

        cfg.get_settings.cache_clear()
        dbm.reset_engine()
        trc.reset_tracer()

        canned = {
            "classification": "null_deref",
            "severity": "P1",
            "reasoning": (
                "Traceback shows AttributeError on a NoneType — the handler "
                "called .strip() on a missing url. Classic null deref."
            ),
            "confidence": 0.95,
        }

        from orchestrator.orchestration import state_machine
        from fastapi.testclient import TestClient

        with patch.object(
            state_machine,
            "_build_openai_client",
            lambda: _FakeOpenAI(canned),
        ):
            from orchestrator.main import app

            with TestClient(app) as client:
                payload = {
                    "endpoint": "POST /shorten",
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
                }
                resp = client.post("/webhooks/alert", json=payload)
                resp.raise_for_status()
                incident_id = resp.json()["incident_id"]

                deadline = time.monotonic() + 10
                detail: dict[str, Any] = {}
                while time.monotonic() < deadline:
                    detail = client.get(
                        f"/webhooks/alert/{incident_id}"
                    ).json()
                    if detail["incident"]["status"] != "received":
                        break
                    time.sleep(0.1)

        # -------- Render summary --------
        inc = detail["incident"]
        runs = detail["agent_runs"]
        print("=" * 70)
        print("INCIDENT")
        print("=" * 70)
        print(f"  id:             {inc['id']}")
        print(f"  endpoint:       {inc['endpoint']}")
        print(f"  exception:      {inc['exception_class']}: {inc['exception_message']}")
        print(f"  classification: {inc['classification']}")
        print(f"  severity:       {inc['severity']}")
        print(f"  confidence:     {inc['triager_confidence']}")
        print(f"  status:         {inc['status']}")
        print(f"  workflow_id:    {inc['workflow_id']}")

        print()
        print("=" * 70)
        print(f"AGENT RUNS ({len(runs)})")
        print("=" * 70)
        for r in runs:
            print(f"  {r['agent_name']:14s} status={r['status']} span_id={r['span_id']}")
            if r.get("output_summary"):
                print(f"      output_summary: {r['output_summary'][:200]}")
            if r.get("error_message"):
                print(f"      error: {r['error_message'][:200]}")

        print()
        print("=" * 70)
        print("LOCAL JSONL TRACE")
        print("=" * 70)
        records = [
            json.loads(line)
            for line in trace_path.read_text().splitlines()
            if line.strip()
        ]
        print(f"  total records: {len(records)}")
        print()
        for r in records:
            kind = r.get("kind")
            if kind == "workflow_start":
                print(
                    f"  WF_START name={r['name']} "
                    f"workflow_id={r['workflow_id']}"
                )
            elif kind == "workflow_end":
                print(
                    f"  WF_END   workflow_id={r['workflow_id']} "
                    f"status={r['status']} ({r['elapsed_s']}s)"
                )
            elif kind == "span_start":
                print(
                    f"  SPAN_S   name={r['name']:30s} "
                    f"span={r['span_id']} parent={r['parent_id']}"
                )
            elif kind == "span_end":
                print(
                    f"  SPAN_E   name={r['name']:30s} "
                    f"span={r['span_id']} status={r['status']} "
                    f"({r['duration_s']}s)"
                )
            elif kind == "event":
                print(
                    f"  EVENT    name={r['name']:30s} "
                    f"parent={r.get('parent_id', '-')}"
                )

        # -------- Parent-chain sanity assertions --------
        starts = [r for r in records if r["kind"] == "span_start"]
        by_name = {r["name"]: r for r in starts}
        wf_id = inc["workflow_id"]

        print()
        print("=" * 70)
        print("PARENT-CHAIN CHECK")
        print("=" * 70)
        ok = True
        if "triager.classify" in by_name:
            assert_eq("triager.classify.parent_id", by_name["triager.classify"]["parent_id"], wf_id) or (ok := False)
        else:
            print("  ❌ triager.classify span missing")
            ok = False
        if "llm_call" in by_name and "triager.classify" in by_name:
            assert_eq(
                "llm_call.parent_id",
                by_name["llm_call"]["parent_id"],
                by_name["triager.classify"]["span_id"],
            ) or (ok := False)
        else:
            print("  ❌ llm_call span missing")
            ok = False
        if "pipeline.null_deref.stub" in by_name:
            assert_eq(
                "pipeline.null_deref.stub.parent_id",
                by_name["pipeline.null_deref.stub"]["parent_id"],
                wf_id,
            ) or (ok := False)
        else:
            print("  ❌ pipeline.null_deref.stub span missing")
            ok = False

        if not ok:
            return 1
        if inc["status"] != "pending_implementation":
            print(f"  ❌ status is {inc['status']}, expected pending_implementation")
            return 1
        if inc["classification"] != "null_deref":
            print(f"  ❌ classification is {inc['classification']}, expected null_deref")
            return 1
        print()
        print("✅ end-to-end verification passed")
        return 0


def assert_eq(label: str, got: Any, expected: Any) -> bool:
    if got == expected:
        print(f"  ✅ {label}: {got}")
        return True
    print(f"  ❌ {label}: got {got!r}, expected {expected!r}")
    return False


if __name__ == "__main__":
    sys.exit(main())
