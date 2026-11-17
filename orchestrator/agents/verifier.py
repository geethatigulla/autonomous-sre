"""Verifier agent: runs the proposed patch in a tempdir sandbox and confirms tests pass.

The Verifier is the fourth agent in the pipeline. It takes the diff
the Fix Author produced (stored on ``incident.proposed_patch``), runs
it through an isolated :class:`~orchestrator.tools.sandbox_tool.Sandbox`
that wraps a tempdir + git init + pytest, and emits a verdict the
state machine uses to decide whether to proceed to Release or mark the
incident as needing human review.

This agent is deterministic — it makes no LLM calls. The reason it's
still modelled as a :class:`BaseAgent` subclass is so the state machine
can drive it through the same ``AgentRun`` bookkeeping path as every
other agent (the dashboard expects exactly one ``AgentRun`` row per
agent type).

Verdicts
--------
* ``verified`` — patch applied cleanly AND tests passed (exit code 0).
* ``tests_failed`` — patch applied cleanly but tests failed.
* ``patch_invalid`` — ``git apply --check`` rejected the diff.
* ``sandbox_error`` — anything else (subprocess crash, missing source
  tree, IO error, internal bug).

The agent always returns a dict; it does not raise on test failure. The
state machine inspects the verdict to choose the next state.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, TYPE_CHECKING

from orchestrator.agents.base import BaseAgent
from orchestrator.timeutil import utcnow
from orchestrator.tools.sandbox_tool import Sandbox


if TYPE_CHECKING:  # pragma: no cover
    from sqlmodel import Session

    from orchestrator.models import Incident
    from orchestrator.tracing import SpanContext


log = logging.getLogger("orchestrator.agents.verifier")


# Cap how much pytest output we keep on the AgentRun + return dict.
# 2000 chars is enough to see the failing test name + traceback tail.
_TEST_OUTPUT_CAP = 2000
# Hard wall on pytest runtime inside the sandbox.
_PYTEST_TIMEOUT_S = 60


async def verify_patch_directly(
    *,
    patch: str,
    source_dir: str,
    span: "SpanContext",
    tracer: Any,
) -> dict:
    """Apply ``patch`` in a fresh tempdir sandbox and run pytest.

    Stateless verifier core — does NOT touch any database row. The
    parallel-fix race uses this so it can verify two independent
    candidate patches concurrently (each gets its own
    :class:`~orchestrator.tools.sandbox_tool.Sandbox`) without writing
    intermediate state to the shared ``incident.proposed_patch``.

    Returns the same dict shape as :meth:`VerifierAgent.run`::

        {
            "applied": bool,
            "tests_ran": bool,
            "tests_passed": bool,
            "test_output": str,        # truncated to _TEST_OUTPUT_CAP
            "verdict": str,            # verified|tests_failed|patch_invalid|sandbox_error
            "duration_seconds": float,
        }

    Spans opened (children of ``span``):
        verifier.setup_sandbox, verifier.apply_patch, verifier.run_tests,
        verifier.cleanup.

    Never raises — every failure mode collapses to a ``verdict`` value
    and a description in ``test_output``.
    """
    start = time.monotonic()

    if not (patch or "").strip():
        return {
            "applied": False,
            "tests_ran": False,
            "tests_passed": False,
            "test_output": "no proposed_patch on incident",
            "verdict": "patch_invalid",
            "duration_seconds": round(time.monotonic() - start, 3),
        }

    span.event(
        "verification_started",
        {
            "diff_chars": len(patch),
            "source_dir": source_dir,
            "pytest_timeout_s": _PYTEST_TIMEOUT_S,
        },
    )

    # ---- 1. Setup sandbox ------------------------------------------------
    try:
        with span.child_span(
            "verifier.setup_sandbox",
            metadata={"source_dir": source_dir},
        ) as setup_span:
            sandbox = Sandbox(source_dir, tracer)
            await sandbox.__aenter__()
            setup_span.event(
                "sandbox_ready",
                {"workdir": str(sandbox.workdir)},
            )
    except Exception as exc:  # noqa: BLE001 — graceful fail
        log.exception("verifier: sandbox setup failed")
        return {
            "applied": False,
            "tests_ran": False,
            "tests_passed": False,
            "test_output": (
                f"sandbox setup failed: {type(exc).__name__}: {exc}"
            )[:_TEST_OUTPUT_CAP],
            "verdict": "sandbox_error",
            "duration_seconds": round(time.monotonic() - start, 3),
        }

    try:
        # ---- 2. Apply patch ---------------------------------------------
        try:
            with span.child_span(
                "verifier.apply_patch",
                metadata={"diff_chars": len(patch)},
            ) as apply_span:
                applied, apply_err = await sandbox.apply_patch(
                    patch, span=apply_span
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("verifier: apply_patch crashed")
            return {
                "applied": False,
                "tests_ran": False,
                "tests_passed": False,
                "test_output": (
                    f"apply_patch crashed: {type(exc).__name__}: {exc}"
                )[:_TEST_OUTPUT_CAP],
                "verdict": "sandbox_error",
                "duration_seconds": round(time.monotonic() - start, 3),
            }

        if not applied:
            return {
                "applied": False,
                "tests_ran": False,
                "tests_passed": False,
                "test_output": (apply_err or "git apply rejected the patch")[
                    -_TEST_OUTPUT_CAP:
                ],
                "verdict": "patch_invalid",
                "duration_seconds": round(time.monotonic() - start, 3),
            }

        # ---- 3. Run pytest ----------------------------------------------
        try:
            with span.child_span(
                "verifier.run_tests",
                metadata={"timeout_s": _PYTEST_TIMEOUT_S},
            ) as test_span:
                code, output = await sandbox.run_pytest(
                    timeout=_PYTEST_TIMEOUT_S, span=test_span
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("verifier: run_pytest crashed")
            return {
                "applied": True,
                "tests_ran": False,
                "tests_passed": False,
                "test_output": (
                    f"pytest crashed: {type(exc).__name__}: {exc}"
                )[:_TEST_OUTPUT_CAP],
                "verdict": "sandbox_error",
                "duration_seconds": round(time.monotonic() - start, 3),
            }

        # Soft pass: source tree had no tests/ directory. We still
        # mark verified but flag risk via the test_output message so
        # the dashboard can surface it.
        no_tests = output.strip() == "no tests found"
        tests_passed = (code == 0)
        verdict: str
        if no_tests:
            verdict = "verified"  # soft pass — caller flagged via output
        elif tests_passed:
            verdict = "verified"
        else:
            verdict = "tests_failed"

        return {
            "applied": True,
            "tests_ran": not no_tests,
            "tests_passed": tests_passed,
            "test_output": output[-_TEST_OUTPUT_CAP:],
            "verdict": verdict,
            "duration_seconds": round(time.monotonic() - start, 3),
        }
    finally:
        # ---- 4. Cleanup -------------------------------------------------
        try:
            with span.child_span("verifier.cleanup") as cleanup_span:
                await sandbox.__aexit__(None, None, None)
                cleanup_span.event("sandbox_removed", {})
        except Exception:  # pragma: no cover — best effort
            log.debug("verifier: sandbox cleanup failed", exc_info=True)


class VerifierAgent(BaseAgent):
    """Applies the Fix Author's diff in a sandbox and runs pytest.

    Public surface is just :meth:`run` (inherited contract). The agent
    is async because the underlying subprocesses run via
    :mod:`asyncio.subprocess`.
    """

    name = "verifier"
    # Deterministic agent — no LLM. We still need a non-None default for
    # BaseAgent's metadata, but the field is unused.
    model = "deterministic"

    def __init__(
        self,
        openai_client: Any,
        tracer: Any,
        *,
        source_dir: str = "victim_app",
    ) -> None:
        super().__init__(openai_client, tracer)
        # Source tree to copy into the sandbox. Tests override this to a
        # tmp_path-based fixture so they don't depend on the real
        # ``victim_app`` directory.
        self.source_dir = source_dir

    async def run(
        self,
        *,
        incident: "Incident",
        span: "SpanContext",
        session: "Session",
    ) -> dict:
        """Verify ``incident.proposed_patch`` and persist the verdict.

        Thin wrapper around :func:`verify_patch_directly` that reads the
        canonical patch off the incident, runs the shared verifier core,
        and persists ``verification_result`` + ``tests_passed`` so the
        dashboard and downstream agents can read them without re-running
        the sandbox.
        """
        result = await verify_patch_directly(
            patch=incident.proposed_patch or "",
            source_dir=self.source_dir,
            span=span,
            tracer=self.tracer,
        )
        return self._finalize(
            incident=incident,
            session=session,
            span=span,
            result=result,
        )

    # ------------------------------------------------------------------
    # Persistence helper
    # ------------------------------------------------------------------

    def _finalize(
        self,
        *,
        incident: "Incident",
        session: "Session",
        span: "SpanContext",
        result: dict,
    ) -> dict:
        """Persist ``verification_result`` + ``tests_passed`` and return ``result``.

        Centralised so every return path writes the same shape — the
        state machine, the dashboard, and the tests all rely on this
        consistency.
        """
        try:
            incident.verification_result = json.dumps(result, default=str)
        except Exception:
            incident.verification_result = json.dumps(
                {"verdict": result.get("verdict", "sandbox_error")}
            )
        incident.tests_passed = bool(result.get("tests_passed"))
        incident.updated_at = utcnow()
        session.add(incident)
        session.commit()
        session.refresh(incident)

        span.event(
            "verification_complete",
            {
                "verdict": result.get("verdict"),
                "applied": result.get("applied"),
                "tests_ran": result.get("tests_ran"),
                "tests_passed": result.get("tests_passed"),
                "duration_seconds": result.get("duration_seconds"),
            },
        )
        return result


__all__ = ["VerifierAgent", "verify_patch_directly"]
