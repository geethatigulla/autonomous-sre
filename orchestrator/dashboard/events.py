"""In-memory async pub/sub used to fan dashboard events out to SSE clients.

The :class:`EventBus` is a single-process, single-event-loop primitive:

* :meth:`EventBus.subscribe` hands out an :class:`asyncio.Queue` of
  fixed depth (100). The SSE endpoint awaits ``queue.get()`` in a loop
  and forwards each payload to the connected browser.
* :meth:`EventBus.publish` enqueues a payload on every subscriber. If a
  subscriber's queue is full we drop the *oldest* event on that queue,
  not the new one — when something is happening in the pipeline the
  judges care about the most recent activity, and a slow client should
  never block the producer.

The bus is intentionally fire-and-forget: ``publish`` swallows all
errors, returns ``None``, and is safe to call from anywhere in the
pipeline. A dashboard outage (no subscribers, or every subscriber
deadlocked) will never affect agent execution — the worst case is a
silent no-op.

Singleton access is via :func:`get_event_bus`; the orchestrator's
pipeline modules call it lazily so they don't take a hard dependency on
the dashboard package at import time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from orchestrator.timeutil import utcnow


log = logging.getLogger("orchestrator.dashboard.events")


# Cap on per-subscriber queue depth. Sized for "judges watching one
# incident": a typical run emits ~30 events, well under 100.
_QUEUE_MAXSIZE = 100


class EventBus:
    """Async pub/sub for dashboard SSE clients.

    Listeners subscribe and receive every event published after their
    subscribe() call. No replay of past events — the dashboard fetches
    historical state via REST on initial load.

    Backpressure policy: drop the oldest queued event if the queue is
    full. Real-time relevance beats completeness for a demo dashboard.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber and return its queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Drop a subscriber. Idempotent — safe to call twice."""
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        """How many SSE clients are currently connected. Used by tests."""
        return len(self._subscribers)

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, event_type: str, data: dict) -> None:
        """Fan an event out to every subscriber.

        Errors are swallowed: a broken queue is removed and processing
        continues. Pipeline code calls this without ever needing to
        worry about dashboard health.
        """
        payload = {
            "type": event_type,
            "data": data,
            "ts": utcnow().isoformat(),
        }
        dead: list[asyncio.Queue] = []
        # Snapshot the set because the publish loop may remove from it.
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop oldest, push newest. This is the documented
                # backpressure rule.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:  # pragma: no cover — queue weirdness
                    dead.append(q)
            except Exception:  # noqa: BLE001 — defensive
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def publish_nowait(self, event_type: str, data: dict) -> None:
        """Sync wrapper that schedules :meth:`publish` on the running loop.

        Some publish points in the pipeline (notably the background
        BackgroundTasks dispatch and SQLAlchemy session callbacks) live
        in sync code. We can't ``await`` from there, so we schedule the
        coroutine on whichever loop is running. If there's no loop, the
        call becomes a no-op — the dashboard simply doesn't see the
        event, but the pipeline is unaffected.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — record the event-drop reason for
            # observability but don't fail.
            log.debug(
                "publish_nowait dropped %s: no running event loop",
                event_type,
            )
            return
        try:
            loop.create_task(self.publish(event_type, data))
        except Exception:  # noqa: BLE001 — defensive
            log.debug("publish_nowait scheduling failed", exc_info=True)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_bus_instance: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Return the process-wide :class:`EventBus`, constructing on first call."""
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = EventBus()
    return _bus_instance


def reset_event_bus() -> None:
    """Drop the cached bus. Tests call this between cases."""
    global _bus_instance
    _bus_instance = None


# ---------------------------------------------------------------------------
# Convenience: fire-and-forget publish from any context
# ---------------------------------------------------------------------------


def safe_publish(event_type: str, data: dict[str, Any]) -> None:
    """Best-effort publish that never raises.

    Wraps :meth:`EventBus.publish_nowait` in a try/except so call sites
    in the pipeline don't have to. This is the function the state
    machine, ingress, and parallel_fix modules call.
    """
    try:
        get_event_bus().publish_nowait(event_type, data)
    except Exception:  # noqa: BLE001 — dashboard must never break pipeline
        log.debug("safe_publish dropped %s", event_type, exc_info=True)


__all__ = [
    "EventBus",
    "get_event_bus",
    "reset_event_bus",
    "safe_publish",
]
