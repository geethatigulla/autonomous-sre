"""Slack incoming-webhook helper for human-in-the-loop notifications.

Slack is always optional in this project: when no webhook URL is
configured, :class:`SlackNotifier` reports itself as disabled and every
``post`` call returns a "skipped" result without touching the network.
Failures (timeouts, non-2xx responses, DNS errors) are returned as data,
never raised, so a Slack outage can't break the remediation pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx


log = logging.getLogger("orchestrator.tools.slack")

DEFAULT_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class SlackPostResult:
    """Outcome of a single webhook post."""

    ok: bool
    skipped: bool = False
    status_code: Optional[int] = None
    error: Optional[str] = None

    def as_event(self) -> dict:
        """Compact dict suitable for a tracing span event."""
        data: dict = {"ok": self.ok}
        if self.skipped:
            data["skipped"] = True
        if self.status_code is not None:
            data["status_code"] = self.status_code
        if self.error:
            data["error"] = self.error[:300]
        return data


class SlackNotifier:
    """Thin wrapper around a Slack incoming-webhook URL."""

    def __init__(
        self, webhook_url: Optional[str], timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        self.webhook_url = (webhook_url or "").strip() or None
        self.timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return self.webhook_url is not None

    async def post(self, text: str) -> SlackPostResult:
        """POST ``text`` to the webhook. Never raises."""
        if not self.enabled:
            return SlackPostResult(ok=False, skipped=True)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(self.webhook_url, json={"text": text})
        except Exception as exc:  # noqa: BLE001 — fire-and-forget
            log.warning("Slack post failed: %s", exc)
            return SlackPostResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return self._result_from_response(resp.status_code, resp.text)

    def post_sync(self, text: str) -> SlackPostResult:
        """Blocking variant for CLI scripts. Never raises."""
        if not self.enabled:
            return SlackPostResult(ok=False, skipped=True)
        try:
            resp = httpx.post(
                self.webhook_url, json={"text": text}, timeout=self.timeout_s
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Slack post failed: %s", exc)
            return SlackPostResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return self._result_from_response(resp.status_code, resp.text)

    @staticmethod
    def _result_from_response(status_code: int, body: Optional[str]) -> SlackPostResult:
        if 200 <= status_code < 300:
            return SlackPostResult(ok=True, status_code=status_code)
        snippet = (body or "")[:200]
        log.warning("Slack webhook returned %s: %s", status_code, snippet)
        return SlackPostResult(ok=False, status_code=status_code, error=snippet or None)


__all__ = ["SlackNotifier", "SlackPostResult", "DEFAULT_TIMEOUT_S"]
