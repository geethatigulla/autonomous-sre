"""Pydantic and SQLModel models shared across the orchestrator.

This module defines the SQL-backed entities that drive the incident
lifecycle:

* :class:`Incident` — the long-lived record for a single production
  alert. Carries the webhook payload, the Triager's classification
  output, and the lifecycle status. ``status`` is the canonical pointer
  to "where are we in the pipeline" — everything downstream reads it.
* :class:`AgentRun` — one row per agent invocation against an incident,
  used by the dashboard and tests to verify which agents fired, in what
  order, and whether they succeeded.

Both tables share an ``id`` of the form ``<prefix>_<12 hex chars>`` so
they're cheap to grep for in logs without colliding with workflow_ids
emitted by :mod:`orchestrator.tracing`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from orchestrator.timeutil import UTCDateTime, utcnow


# ---------------------------------------------------------------------------
# Incident
# ---------------------------------------------------------------------------


class Incident(SQLModel, table=True):
    """A single production incident captured from a victim service.

    The fields fall into three groups: webhook payload (immutable once
    persisted), Triager output (filled in by the first agent), and
    lifecycle bookkeeping (status, workflow_id, timestamps).

    ``status`` transitions::

        received → classified → investigating → fixing → verifying
                 ↘ escalated     ↘ failed       ↘ resolved
                 ↘ pending_implementation (until downstream agents land)
    """

    id: str = Field(
        default_factory=lambda: f"inc_{uuid.uuid4().hex[:12]}",
        primary_key=True,
    )
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)

    # ---- Webhook payload --------------------------------------------------
    source_service: str  # e.g. "victim_app"
    endpoint: str  # e.g. "POST /shorten"
    exception_class: str  # e.g. "AttributeError"
    exception_message: str
    traceback: str = Field(sa_column=Column(Text))
    request_body: str  # JSON-encoded
    raw_payload: str = Field(sa_column=Column(Text))  # full webhook for replay

    # ---- Triager output ---------------------------------------------------
    # null until the Triager has run; afterwards one of the four valid
    # classifications. severity follows P0/P1/P2.
    classification: Optional[str] = None
    severity: Optional[str] = None
    triager_reasoning: Optional[str] = Field(
        default=None, sa_column=Column(Text)
    )
    triager_confidence: Optional[float] = None

    # ---- Investigator output ---------------------------------------------
    # Filled in by the Investigator agent. ``investigation_result`` is the
    # full JSON blob the agent emitted (hypotheses + log); the three
    # scalar columns mirror the most-important top-level fields so the
    # dashboard and downstream agents can read them without parsing.
    investigation_result: Optional[str] = Field(
        default=None, sa_column=Column(Text)
    )  # JSON-encoded
    suspect_file: Optional[str] = None
    suspect_line: Optional[int] = None
    root_cause: Optional[str] = Field(default=None, sa_column=Column(Text))

    # ---- Fix Author output -----------------------------------------------
    # Filled in by the Fix Author agent. ``proposed_patch`` is the raw
    # unified diff the model produced; the explanation / risk / confidence
    # columns mirror the rest of the JSON envelope so the dashboard and
    # Verifier can read them without re-parsing.
    proposed_patch: Optional[str] = Field(
        default=None, sa_column=Column(Text)
    )
    fix_explanation: Optional[str] = Field(
        default=None, sa_column=Column(Text)
    )
    fix_risk_level: Optional[str] = None  # low | medium | high
    fix_confidence: Optional[float] = None

    # ---- Verifier output --------------------------------------------------
    # Filled in by the Verifier agent. ``verification_result`` is a JSON
    # blob containing the full Verifier dict (applied/tests_ran/verdict/
    # duration_seconds/test_output); ``tests_passed`` is a convenience
    # scalar mirroring the same field so the dashboard can render a
    # green/red dot without parsing JSON.
    verification_result: Optional[str] = Field(
        default=None, sa_column=Column(Text)
    )
    tests_passed: Optional[bool] = None

    # ---- Releaser output --------------------------------------------------
    # Filled in by the Releaser agent. ``pr_url`` is the canonical link
    # the dashboard / Slack message embeds; ``pr_number`` and
    # ``pr_branch`` are kept around for retry / idempotency (if the
    # Releaser runs twice on the same incident it must NOT create a
    # duplicate PR). ``slack_posted`` records whether the Slack webhook
    # POST returned 2xx — None when no webhook is configured.
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    pr_branch: Optional[str] = None
    slack_posted: Optional[bool] = None

    # ---- Outbound scrubber bookkeeping -----------------------------------
    # The victim app's monitor middleware runs a regex-based PII / secret
    # scrubber over the alert payload BEFORE POSTing it here, so secrets
    # never reach the orchestrator (or the LLM). These two columns surface
    # that fact to operators:
    #   - ``scrubbed`` is True when the victim app confirmed it ran the
    #     scrubber (even if no redactions fired), False when it ran but
    #     the scrubber itself failed, and None for legacy payloads that
    #     predate the feature.
    #   - ``redactions`` is a JSON-encoded list of redaction *types* that
    #     fired (e.g. ``["api_key", "email"]``) — surfaced on the
    #     dashboard so judges and operators can SEE that the safety
    #     feature worked.
    # NOTE: this is a schema change — run ``rm sre.db`` once to recreate
    # the SQLite file with the new columns. There's no migration story
    # because the demo DB is a throwaway.
    scrubbed: Optional[bool] = None
    redactions: Optional[str] = None  # JSON-encoded list[str]

    # ---- Lifecycle --------------------------------------------------------
    status: str = "received"
    workflow_id: Optional[str] = None  # tracer workflow id


# ---------------------------------------------------------------------------
# AgentRun
# ---------------------------------------------------------------------------


class AgentRun(SQLModel, table=True):
    """One invocation of an agent against an incident.

    Each agent in the pipeline (triager, investigator, fix_author,
    verifier, releaser) emits exactly one row per run. ``span_id`` links
    back to the tracer span so the dashboard can correlate a row in this
    table with a node in the Omium trace tree.
    """

    id: str = Field(
        default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}",
        primary_key=True,
    )
    incident_id: str = Field(foreign_key="incident.id", index=True)
    agent_name: str  # triager | investigator | fix_author | verifier | releaser
    started_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    finished_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    status: str = "running"  # running | success | error
    input_summary: Optional[str] = Field(
        default=None, sa_column=Column(Text)
    )
    output_summary: Optional[str] = Field(
        default=None, sa_column=Column(Text)
    )
    error_message: Optional[str] = None
    span_id: Optional[str] = None  # links to tracer span


__all__ = ["Incident", "AgentRun"]
