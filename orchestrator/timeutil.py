"""Time helpers.

Every timestamp the orchestrator writes is a timezone-aware UTC
``datetime``. Recent SQLModel releases refuse to persist naive datetimes,
and naive values also get misread as local time by the dashboard's
JavaScript, so all code should call :func:`utcnow` instead of
``datetime.utcnow()`` (which is deprecated as of Python 3.12).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import types


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Treat naive datetimes as UTC; convert aware ones to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UTCDateTime(types.TypeDecorator):
    """SQLAlchemy column type that always round-trips aware UTC datetimes.

    SQLite has no timezone support, so values are stored as naive UTC and
    re-tagged with ``timezone.utc`` on load. Naive values passed in are
    assumed to already be UTC. Using our own type (rather than relying on
    whatever the installed SQLModel version does by default) keeps reads
    and writes consistent across library versions.
    """

    impl = types.DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: D401
        value = ensure_utc(value)
        return None if value is None else value.replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return ensure_utc(value)


__all__ = ["utcnow", "ensure_utc", "UTCDateTime"]
