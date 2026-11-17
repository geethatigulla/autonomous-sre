"""Triager agent: classifies incoming alerts and decides whether to engage.

The Triager is the first agent in the pipeline. It receives an
:class:`~orchestrator.models.Incident` row in ``status="received"`` and
emits a structured JSON classification covering:

* **classification** — which downstream specialist should handle the
  incident. One of ``null_deref`` / ``recent_regression`` /
  ``config_error`` / ``unknown``. The router in
  :mod:`orchestrator.orchestration.state_machine` reads this field to
  choose the pipeline branch.
* **severity** — P0 / P1 / P2. Used by the Releaser to decide paging
  policy (not in this prompt, but reserved here).
* **reasoning** — 1–3 sentences citing concrete evidence from the
  stack trace. Surfaced in the dashboard for human review.
* **confidence** — 0..1 self-reported confidence. Below a threshold the
  state machine could (later) downgrade to ``unknown`` and escalate.

The agent is intentionally conservative — when the evidence is thin it
should pick ``unknown`` rather than guess and burn a downstream agent
on the wrong pipeline.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from orchestrator.agents.base import BaseAgent
from orchestrator.timeutil import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from sqlmodel import Session

    from orchestrator.models import Incident
    from orchestrator.tracing import SpanContext


VALID_CLASSIFICATIONS = {
    "null_deref",
    "recent_regression",
    "config_error",
    "unknown",
}
VALID_SEVERITIES = {"P0", "P1", "P2"}


class TriagerAgent(BaseAgent):
    """Classifies an incident into one of four buckets + a severity."""

    name = "triager"
    model = "gpt-4o-mini"

    SYSTEM_PROMPT = """You are an SRE Triager. You receive a production incident and classify it.

Classify the incident into EXACTLY ONE of:
- null_deref: AttributeError or TypeError caused by None/null values on edge-case input (e.g. "'NoneType' object has no attribute X", "argument of type 'NoneType' is not iterable")
- recent_regression: KeyError, AttributeError, or assertion failure suggesting a recent code change broke the contract (e.g. KeyError on a field that USED to exist, renamed key, signature mismatch)
- config_error: KeyError or RuntimeError on startup or first request involving environment variables, config keys, missing credentials, or missing connections (e.g. KeyError: 'REDIS_URL', missing API key)
- unknown: anything else — race conditions, memory issues, deadlocks, third-party outages, anything you cannot confidently route to one of the above three.

Also assign severity:
- P0: app cannot serve any traffic (startup failure, every request crashes)
- P1: a specific endpoint or feature is broken, other traffic works
- P2: rare edge case, low user impact

Be conservative: when uncertain, classify as 'unknown' rather than guessing wrong. A wrong classification routes to the wrong specialist agent and wastes time.

Respond with ONLY a JSON object, no other text:
{
  "classification": "null_deref" | "recent_regression" | "config_error" | "unknown",
  "severity": "P0" | "P1" | "P2",
  "reasoning": "1-3 sentences explaining your decision, citing specific evidence from the stack trace",
  "confidence": 0.0 to 1.0
}"""

    async def run(
        self,
        *,
        incident: "Incident",
        span: "SpanContext",
        session: "Session",
    ) -> dict:
        # Build the user prompt from the incident payload. Tracebacks
        # can be huge; we send them verbatim since the model needs them
        # to do its job, but we cap the request body and traceback at
        # generous limits in case a misbehaving service floods us.
        traceback_excerpt = (incident.traceback or "")[-4000:]
        request_body_excerpt = (incident.request_body or "")[:2000]

        user_prompt = f"""INCIDENT REPORT

Service: {incident.source_service}
Endpoint: {incident.endpoint}
Exception: {incident.exception_class}: {incident.exception_message}

Request body:
{request_body_excerpt}

Traceback:
{traceback_excerpt}

Classify this incident."""

        span.event(
            "classifying",
            {
                "exception_class": incident.exception_class,
                "endpoint": incident.endpoint,
            },
        )

        content = await self._call_llm(
            system=self.SYSTEM_PROMPT,
            user=user_prompt,
            span=span,
            response_format={"type": "json_object"},
        )

        # The model is instructed to emit a JSON object only; if it
        # misbehaves, the JSONDecodeError surfaces up to the state
        # machine which records it on the AgentRun row.
        result = json.loads(content)

        # ---- Validate / coerce ------------------------------------
        # We never trust the model output blindly: bad classifications
        # route to the wrong specialist, so we downgrade to "unknown"
        # rather than propagate garbage.
        if result.get("classification") not in VALID_CLASSIFICATIONS:
            span.event(
                "invalid_classification_coerced",
                {"raw": result.get("classification")},
            )
            result["classification"] = "unknown"
        if result.get("severity") not in VALID_SEVERITIES:
            span.event(
                "invalid_severity_coerced",
                {"raw": result.get("severity")},
            )
            result["severity"] = "P1"

        # Confidence is best-effort; clamp to [0, 1] if present.
        confidence = result.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else 0.0
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0
        result["confidence"] = confidence

        span.event(
            "classified",
            {
                "classification": result["classification"],
                "severity": result["severity"],
                "confidence": result["confidence"],
            },
        )

        # ---- Persist back to the incident -------------------------
        incident.classification = result["classification"]
        incident.severity = result["severity"]
        incident.triager_reasoning = result.get("reasoning", "")
        incident.triager_confidence = result["confidence"]
        incident.status = "classified"
        incident.updated_at = utcnow()
        session.add(incident)
        session.commit()
        session.refresh(incident)

        return result


__all__ = ["TriagerAgent", "VALID_CLASSIFICATIONS", "VALID_SEVERITIES"]
