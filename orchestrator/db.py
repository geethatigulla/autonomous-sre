"""SQLite-backed persistence layer powered by SQLModel.

The orchestrator runs on a single-writer SQLite database by default
(``sqlite:///./sre.db``) but the URL is configurable via
``DATABASE_URL`` for tests (typically ``sqlite:///:memory:`` or a
``tmp_path``-scoped file).

Public surface:

* :func:`get_engine` — process-wide cached SQLAlchemy engine.
* :func:`init_db` — idempotent; creates all SQLModel tables on startup.
* :func:`get_session` — yields a session bound to the cached engine. Used
  as a FastAPI dependency *and* called directly from background tasks
  (where dependency injection is not available).
* :func:`reset_engine` — drops the cached engine so the next ``get_engine``
  call rebuilds it. Used by tests that flip ``DATABASE_URL`` between
  cases.
"""

from __future__ import annotations

import threading
from typing import Iterator, Optional

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from orchestrator.config import get_settings


# ---------------------------------------------------------------------------
# Engine cache
# ---------------------------------------------------------------------------

_engine_lock = threading.Lock()
_engine: Optional[Engine] = None


def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine, building it on demand.

    SQLite needs ``check_same_thread=False`` so the engine can be shared
    across FastAPI's worker threads + the background tasks that run on
    the event loop. For other backends we let SQLAlchemy pick sensible
    defaults.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        url = get_settings().database_url
        kwargs: dict = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        return _engine


def init_db() -> None:
    """Create every SQLModel table that's been imported.

    Importing :mod:`orchestrator.models` here is what registers the
    :class:`Incident` / :class:`AgentRun` tables on
    ``SQLModel.metadata``; without that import the call is a silent
    no-op. We do the import inside the function so the module remains
    importable in environments that don't yet have ``sqlmodel``
    installed (e.g. during lint passes).
    """
    # Side-effect import: registers tables on SQLModel.metadata.
    import orchestrator.models  # noqa: F401  pylint: disable=unused-import

    SQLModel.metadata.create_all(get_engine())


def get_session() -> Iterator[Session]:
    """Yield a session bound to the cached engine.

    Designed to be used both as a FastAPI dependency (``Depends(get_session)``)
    and as a context-manager-style generator in background tasks::

        with next(get_session()) as session:
            ...

    Prefer ``Session(get_engine())`` directly in background code where the
    generator protocol is awkward (the state machine does this).
    """
    with Session(get_engine()) as session:
        yield session


def reset_engine() -> None:
    """Drop the cached engine. Tests use this after mutating env vars."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:
                pass
        _engine = None


__all__ = ["get_engine", "init_db", "get_session", "reset_engine"]
