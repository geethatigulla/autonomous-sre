"""Exception-reporting middleware for the victim app.

Catches any unhandled exception that bubbles out of a route handler, captures a
structured incident report (timestamp, endpoint, method, exception class +
message, full traceback, request body), and POSTs it to the orchestrator
webhook as JSON. The POST is fire-and-forget — we don't block the HTTP
response on it — and after dispatch we re-raise so Starlette still returns a
500 to the client.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import traceback
from typing import Any

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

try:  # running from the repo root (demo CLI, orchestrator tests)
    from victim_app.scrubber import scrub_dict
except ImportError:  # running from inside victim_app/ (Docker image, seeded victim repo)
    from scrubber import scrub_dict  # type: ignore[no-redef]

log = logging.getLogger("victim_app.monitor")

DEFAULT_WEBHOOK = "http://localhost:8000/webhooks/alert"
WEBHOOK_TIMEOUT_SECONDS = 2.0


class ExceptionReportingMiddleware(BaseHTTPMiddleware):
    """Catch unhandled exceptions, ship them to the orchestrator, re-raise."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._webhook_url = os.environ.get(
            "ORCHESTRATOR_WEBHOOK_URL", DEFAULT_WEBHOOK
        )

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # Buffer the request body up front so we can include it in the report
        # AND still let downstream handlers read it. Starlette's Request body
        # is a stream that can only be consumed once, so we re-inject it via
        # request._receive after reading.
        raw_body = await request.body()
        await self._rewind_body(request, raw_body)

        try:
            response: Response = await call_next(request)
            return response
        except Exception as exc:  # noqa: BLE001 — we genuinely want everything
            payload = self._build_payload(request, exc, raw_body)
            # Run the outbound payload through the PII/secret scrubber
            # BEFORE it leaves this process. Sensitive data in request
            # bodies and tracebacks must never reach the orchestrator
            # (and therefore the LLM provider). Defensive: if the
            # scrubber itself throws, we still want the alert to fly —
            # a missed scrub is bad, but a missed alert is worse.
            payload = self._scrub_payload(payload)
            # Fire and forget — don't await the webhook call.
            asyncio.create_task(self._send(payload))
            raise

    # ------------------------------------------------------------------ utils

    @staticmethod
    async def _rewind_body(request: Request, raw_body: bytes) -> None:
        """Replace request._receive so downstream handlers can re-read body."""

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": raw_body, "more_body": False}

        request._receive = receive  # type: ignore[attr-defined]

    @staticmethod
    def _build_payload(
        request: Request, exc: BaseException, raw_body: bytes
    ) -> dict[str, Any]:
        try:
            decoded_body: Any = raw_body.decode("utf-8") if raw_body else ""
        except UnicodeDecodeError:
            decoded_body = repr(raw_body)

        return {
            "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "path": request.url.path,
            "method": request.method,
            "exception_class": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
            "request_body": decoded_body,
        }

    @staticmethod
    def _scrub_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Run the alert payload through the outbound PII / secret scrubber.

        Returns a *new* payload dict with two additional bookkeeping
        fields so the orchestrator and dashboard can show, audibly, that
        scrubbing happened:

        * ``scrubbed``: bool — True when the scrubber ran, regardless of
          whether anything was actually redacted.
        * ``redactions``: list[str] — the distinct redaction types that
          fired (e.g. ``["api_key", "email"]``). Empty when the report
          contained no sensitive data.

        If the scrubber itself raises, we fall back to the raw payload
        but mark ``scrubbed=False`` and log a warning. Monitoring must
        never break because scrubbing failed.
        """
        try:
            cleaned, redactions = scrub_dict(payload)
            if not isinstance(cleaned, dict):
                # Paranoia: scrub_dict preserves types, but if it somehow
                # didn't return a dict we don't want to ship junk.
                raise TypeError("scrub_dict returned a non-dict payload")
            cleaned["scrubbed"] = True
            cleaned["redactions"] = redactions
            return cleaned
        except Exception as scrub_err:  # noqa: BLE001 — see docstring
            log.warning(
                "scrubber failed, sending raw payload: %s", scrub_err
            )
            # Be explicit so a downstream consumer can tell that this
            # alert went out *un*scrubbed.
            payload = dict(payload)
            payload["scrubbed"] = False
            payload["redactions"] = []
            return payload

    async def _send(self, payload: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
                await client.post(self._webhook_url, json=payload)
        except Exception as send_err:  # noqa: BLE001
            # Best-effort: log and move on. We must never let a webhook
            # failure mask the original application error.
            log.warning(
                "failed to deliver incident to %s: %s",
                self._webhook_url,
                send_err,
            )
