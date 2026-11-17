"""Minimal smoke demo: emit a workflow with one nested span pair.

Run with: ``python -m orchestrator.scripts.hello_trace``

The VERIFICATION block at the end confirms which backend was actually used
and how many records were appended to the local JSONL audit log. If the
``Backend used`` line reads ``omium`` and ``Omium initialized: True``, the
``hello_world_demo`` workflow should appear on https://app.omium.ai/
(possibly after a short ingestion delay).
"""

from __future__ import annotations

import time

from orchestrator.config import get_settings
from orchestrator.tracing import get_tracer


def main() -> None:
    tracer = get_tracer()
    with tracer.start_workflow("hello_world_demo", metadata={"source": "demo"}) as wf:
        with wf.span("outer_step", metadata={"role": "outer"}) as outer:
            with outer.child_span("inner_step", metadata={"role": "inner"}) as inner:
                time.sleep(0.5)
                inner.event("inner_heartbeat", {"phase": "sleeping"})

    # Force any buffered spans to ship before we print verification.
    tracer.shutdown()

    # ``settings.omium_project`` reflects the resolved project (env >
    # omium.toml > default) once the tracer has initialized.
    settings = get_settings()

    print("=" * 60)
    print("VERIFICATION:")
    print(f"  Backend used: {tracer.backend_used}")
    print(f"  Omium initialized: {tracer._omium_tracer is not None}")
    print(f"  Local log entries: {tracer.local_entry_count}")
    print(f"  Project bound: {settings.omium_project}")
    print(
        f"  Dashboard URL: https://app.omium.ai/automation?project={settings.omium_project}"
    )
    print("  Workflow name to look for: hello_world_demo")
    print("=" * 60)


if __name__ == "__main__":
    main()
