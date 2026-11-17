"""Defensive wrapper around the Omium SDK.

The wrapper exposes a small, opinionated surface (``Tracer`` / ``WorkflowHandle``
/ ``SpanContext``) so the rest of the orchestrator never imports ``omium``
directly. This isolates us from SDK breakage and lets us silently fall back to
a local JSONL log when Omium credentials are missing or the backend is
unreachable — a non-negotiable requirement for demo reliability.

Verified Omium SDK shape (omium==0.4.x)
---------------------------------------
* ``omium.init(api_key=..., api_base_url=..., project=..., auto_trace=...,
  debug=...)`` is the module-level entry point. Idempotent (logs a warning if
  called twice). Reads ``OMIUM_API_KEY`` / ``OMIUM_PROJECT`` / ``OMIUM_API_URL``
  from env when args are missing. Validates the API key format (must start with
  ``om_`` or ``omium_`` and be >= 20 chars) — raises ``ValueError`` otherwise.
* ``omium.integrations.tracer.OmiumTracer(execution_id=None, project=None,
  trace_id=None)``. The instance is the right thing to keep around: its
  ``.span(name, input=None, span_type="default", **attributes)`` is a
  ``@contextmanager``-decorated method yielding a mutable ``Span``, and its
  ``.flush()`` / ``.close()`` ship buffered spans to the ingest endpoint.
* ``omium.trace(...)`` is a DECORATOR factory only — we don't use it because
  we want explicit span boundaries, not function-wrapper magic.
* ``omium.OmiumClient`` is the async gRPC checkpoint client — unrelated to
  trace ingestion. Do NOT instantiate it here.
* Auto-instrumentation only kicks in for LangGraph / CrewAI; we use neither,
  so we initialize with ``auto_trace=False``.
* Ingest endpoint: ``POST {api_base_url}/traces/ingest`` (api_base_url is
  normalized to end with ``/api/v1`` internally).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

try:  # tomllib is stdlib on Python 3.11+; tomli is the back-port for 3.10.
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - 3.10 path
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

try:  # rich is a hard requirement but we degrade if missing.
    from rich.console import Console

    _console: Optional["Console"] = Console(stderr=False, highlight=False)
except Exception:  # pragma: no cover - very defensive
    _console = None

from orchestrator.config import get_settings


logger = logging.getLogger("orchestrator.tracing")


# ---------------------------------------------------------------------------
# Omium SDK (lazy / defensive import)
# ---------------------------------------------------------------------------

try:
    import omium  # type: ignore[import-not-found]
    from omium.integrations.tracer import OmiumTracer  # type: ignore[import-not-found]

    _OMIUM_IMPORTABLE = True
except Exception:  # pragma: no cover - SDK should be installed, but be safe.
    omium = None  # type: ignore[assignment]
    OmiumTracer = None  # type: ignore[assignment,misc]
    _OMIUM_IMPORTABLE = False


# Module-level init guard. ``omium.init`` is internally idempotent (it logs a
# warning on second call) but we also gate it here so we never double-log or
# re-validate after a successful initialization.
_initialized: bool = False
_init_lock = threading.Lock()


def _read_project_from_toml() -> Optional[str]:
    """Return ``[project].name`` from ``omium.toml`` at CWD, or ``None``.

    Used as a fallback project source so ``omium.toml`` can stay the
    single source of truth — env var ``OMIUM_PROJECT`` still wins if set.
    """
    if tomllib is None:  # pragma: no cover
        return None
    toml_path = Path.cwd() / "omium.toml"
    if not toml_path.exists():
        return None
    try:
        with toml_path.open("rb") as fh:
            data = tomllib.load(fh)
        name = data.get("project", {}).get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None
    except Exception:
        return None


def _resolve_project(settings) -> str:
    """Resolve the Omium project name with this precedence:

    1. ``OMIUM_PROJECT`` env var (if explicitly set)
    2. ``[project].name`` from ``omium.toml`` at CWD
    3. The pydantic ``Settings`` default (``"autonomous-sre"``)
    """
    if os.environ.get("OMIUM_PROJECT"):
        return settings.omium_project
    toml_project = _read_project_from_toml()
    if toml_project:
        return toml_project
    return settings.omium_project


def _ensure_omium_initialized() -> bool:
    """Run ``omium.init()`` at most once for this process.

    Returns True iff Omium is now ready to accept ``OmiumTracer`` instances.
    """
    global _initialized
    if _initialized:
        return True
    if not _OMIUM_IMPORTABLE:
        return False
    with _init_lock:
        if _initialized:
            return True
        settings = get_settings()
        if not (settings.omium_api_key and settings.trace_backend in ("omium", "auto")):
            return False
        # Resolve project (env > omium.toml > default) and propagate the
        # resolved value back onto the cached Settings instance so callers
        # like hello_trace's VERIFICATION block see the same value we
        # actually bound spans to.
        project = _resolve_project(settings)
        try:
            settings.omium_project = project
        except Exception:
            pass
        try:
            omium.init(
                api_key=settings.omium_api_key,
                api_base_url=settings.omium_api_base,
                project=project,
                auto_trace=False,  # we trace manually — no LangGraph/CrewAI here
                debug=False,
            )
            _initialized = True
        except Exception as e:
            print(f"[TRACING] omium.init() failed: {e}")
            traceback.print_exc()
            _initialized = False
    return _initialized


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

def _say(message: str, *, style: str = "") -> None:
    """Print to stdout via rich (with colour) — quietly noop if rich missing."""
    if _console is not None:
        try:
            _console.print(message, style=style or None)
            return
        except Exception:
            pass
    plain = message
    for tag in ("[bold]", "[/bold]", "[dim]", "[/dim]"):
        plain = plain.replace(tag, "")
    print(plain)


# ---------------------------------------------------------------------------
# Local JSONL backend
# ---------------------------------------------------------------------------


class _LocalBackend:
    """Append-only JSON Lines sink used as a fallback / audit log."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        record = dict(record)  # defensive copy
        record.setdefault("ts", time.time())
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------


class Tracer:
    """Top-level entry point. Creates ``WorkflowHandle``s.

    Holds (at most) one ``OmiumTracer`` instance for the lifetime of the
    process. Falls back to JSONL-only mode if Omium is not configured /
    cannot initialize / explicitly disabled via ``TRACE_BACKEND=local``.
    """

    def __init__(self, service_name: str = "autonomous-sre") -> None:
        settings = get_settings()
        self.service_name = service_name
        self._local = _LocalBackend(settings.local_trace_path)
        self._local_entry_count = 0
        self._backend_mode = settings.trace_backend
        self._omium_tracer: Optional[OmiumTracer] = None  # type: ignore[valid-type]

        if self._backend_mode == "local":
            return

        if self._backend_mode not in ("auto", "omium"):  # pragma: no cover
            raise ValueError(f"Unknown TRACE_BACKEND: {self._backend_mode!r}")

        if not settings.omium_configured:
            if self._backend_mode == "omium":
                raise RuntimeError(
                    "TRACE_BACKEND=omium but OMIUM_API_KEY is not set"
                )
            logger.info(
                "OMIUM_API_KEY not set — tracing will use local JSONL only"
            )
            return

        if not _ensure_omium_initialized():
            if self._backend_mode == "omium":
                raise RuntimeError(
                    "TRACE_BACKEND=omium but omium.init() failed; see logs"
                )
            logger.warning(
                "Omium init failed; falling back to local JSONL only"
            )
            return

        try:
            # NOTE: OmiumTracer's real signature is
            #   __init__(execution_id=None, project=None, trace_id=None)
            # so we pass the project name (which _ensure_omium_initialized
            # has already resolved via env > omium.toml > default). We keep
            # the wrapper-level ``service_name`` stamped on every local
            # record too for cross-service filtering.
            self._omium_tracer = OmiumTracer(project=settings.omium_project)
        except Exception as e:
            print(f"[TRACING] OmiumTracer instantiation failed: {e}")
            traceback.print_exc()
            self._omium_tracer = None
            if self._backend_mode == "omium":
                raise

    # -- introspection used by hello_trace's VERIFICATION block ----------
    @property
    def backend_used(self) -> str:
        return "omium" if self._omium_tracer is not None else "local"

    @property
    def local_entry_count(self) -> int:
        return self._local_entry_count

    # -- internal helpers accessible to WorkflowHandle / SpanContext -----
    def _write_local(self, kind: str, data: dict) -> None:
        record: dict[str, Any] = {
            "kind": kind,
            "service": self.service_name,
        }
        record.update(data)
        self._local.write(record)
        self._local_entry_count += 1

    def _log_span_start(self, span: "SpanContext") -> None:
        parent_label = (
            "workflow" if span.parent_id == span.workflow_id else span.parent_id
        )
        _say(
            f"  [SPAN] {span.name} (parent={parent_label}) — started",
            style="blue",
        )

    def _log_span_end(
        self,
        span: "SpanContext",
        duration: float,
        status: str,
    ) -> None:
        colour = "green" if status == "success" else "red"
        _say(
            f"  [SPAN] {span.name} — completed in {duration:.2f}s, status={status}",
            style=colour,
        )

    def _log_event(self, name: str) -> None:
        _say(f"    [SPAN-EVENT] {name}", style="dim")

    # -- async context manager (kept for symmetry with prior API) --------
    async def __aenter__(self) -> "Tracer":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        return None

    # -- factory ---------------------------------------------------------
    def start_workflow(
        self,
        name: str,
        metadata: Optional[dict] = None,
    ) -> "WorkflowHandle":
        workflow_id = f"{name}_{uuid.uuid4().hex[:12]}"
        return WorkflowHandle(
            tracer=self,
            workflow_id=workflow_id,
            name=name,
            metadata=metadata or {},
        )

    # -- shutdown --------------------------------------------------------
    def shutdown(self) -> None:
        """Flush + close the Omium tracer (idempotent, never raises)."""
        if self._omium_tracer is None:
            return
        try:
            self._omium_tracer.flush()
        except Exception as e:
            print(f"[TRACING] omium flush in shutdown failed: {e}")
        try:
            self._omium_tracer.close()
        except Exception as e:
            print(f"[TRACING] omium close in shutdown failed: {e}")


# ---------------------------------------------------------------------------
# WorkflowHandle — wraps a top-level Omium span ("workflow:<name>")
# ---------------------------------------------------------------------------


class WorkflowHandle:
    """A top-level trace. Acts as both a regular object (``handle.finish(...)``)
    and a sync context manager. Spans created off the handle inherit the
    workflow's open Omium span as their parent — that's the whole point.
    """

    def __init__(
        self,
        *,
        tracer: Tracer,
        workflow_id: str,
        name: str,
        metadata: dict,
    ) -> None:
        self.tracer = tracer
        self.workflow_id = workflow_id
        self.name = name
        self.metadata = dict(metadata)
        self._started_at = time.time()
        self._finished = False
        self._omium_span_cm: Any = None
        self._omium_span: Any = None
        self._open()

    # -- lifecycle -------------------------------------------------------
    def _open(self) -> None:
        self.tracer._write_local(
            "workflow_start",
            {
                "workflow_id": self.workflow_id,
                "name": self.name,
                "metadata": self.metadata,
            },
        )
        if self.tracer._omium_tracer is not None:
            try:
                attrs = {
                    "workflow_id": self.workflow_id,
                    **self.metadata,
                }
                # OmiumTracer.span signature: span(name, input=None,
                # span_type="default", **attributes). Spread attrs into kwargs.
                self._omium_span_cm = self.tracer._omium_tracer.span(
                    f"workflow:{self.name}",
                    input=self.metadata or None,
                    span_type="workflow",
                    **attrs,
                )
                self._omium_span = self._omium_span_cm.__enter__()
            except Exception as e:
                print(f"[TRACING] omium workflow span open failed: {e}")
                self._omium_span_cm = None
                self._omium_span = None

        _say(
            f"[bold][TRACE][/bold] workflow_started: {self.workflow_id}",
            style="cyan",
        )

    def finish(
        self,
        status: str = "success",
        output: Optional[dict] = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        elapsed = time.time() - self._started_at

        # Close the Omium workflow span (if open).
        if self._omium_span_cm is not None:
            try:
                if self._omium_span is not None and output is not None:
                    try:
                        self._omium_span.set_output(output)
                    except Exception:
                        pass
                if self._omium_span is not None:
                    try:
                        self._omium_span.set_attribute("status", status)
                    except Exception:
                        pass
                self._omium_span_cm.__exit__(None, None, None)
            except Exception as e:
                print(f"[TRACING] omium workflow span close failed: {e}")

        # Force a final POST to /api/v1/traces/ingest before the script exits.
        if self.tracer._omium_tracer is not None:
            try:
                self.tracer._omium_tracer.flush()
            except Exception as e:
                print(f"[TRACING] omium flush at finish failed: {e}")

        self.tracer._write_local(
            "workflow_end",
            {
                "workflow_id": self.workflow_id,
                "name": self.name,
                "status": status,
                "output": output,
                "elapsed_s": round(elapsed, 4),
            },
        )

        colour = "green" if status == "success" else "red"
        _say(
            f"[bold][TRACE][/bold] workflow_finished: {self.workflow_id} "
            f"[dim]({elapsed:.2f}s, status={status})[/dim]",
            style=colour,
        )

    def __enter__(self) -> "WorkflowHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finish(status="success" if exc is None else "error")

    # -- span factory ----------------------------------------------------
    def span(
        self,
        name: str,
        metadata: Optional[dict] = None,
    ) -> "SpanContext":
        return SpanContext(
            name=name,
            metadata=metadata,
            parent_id=self.workflow_id,
            workflow_id=self.workflow_id,
            tracer=self.tracer,
        )

    # -- event -----------------------------------------------------------
    def event(self, name: str, data: Optional[dict] = None) -> None:
        self.tracer._write_local(
            "event",
            {
                "workflow_id": self.workflow_id,
                "parent_id": self.workflow_id,
                "name": name,
                "data": data or {},
            },
        )
        if self._omium_span is not None:
            try:
                self._omium_span.add_event(name=name, attributes=data or {})
            except Exception as e:
                print(f"[TRACING] omium event failed: {e}")
        _say(f"  [TRACE-EVENT] {name}", style="dim")


# ---------------------------------------------------------------------------
# SpanContext — wraps a nested Omium span
# ---------------------------------------------------------------------------


class SpanContext:
    """A causally-linked unit of work inside a workflow.

    Each ``SpanContext`` owns one ``OmiumTracer.span(...)`` context manager
    plus a local JSONL record pair. Both sinks receive the same ``span_id``,
    ``parent_id``, and ``workflow_id`` so traces can be reconstructed even if
    the Omium endpoint was unreachable.
    """

    def __init__(self, name, metadata, parent_id, workflow_id, tracer):
        self.name = name
        self.metadata: dict[str, Any] = dict(metadata or {})
        # Causal info also stamped on metadata so JSONL consumers can rebuild
        # the tree without referring to the dedicated parent/workflow fields.
        self.metadata.setdefault("parent_id", parent_id)
        self.metadata.setdefault("workflow_id", workflow_id)
        self.parent_id = parent_id
        self.workflow_id = workflow_id
        self.tracer: Tracer = tracer
        self.span_id = f"span_{uuid.uuid4().hex[:12]}"
        self._omium_span_cm: Any = None
        self._omium_span: Any = None
        self._start_time: Optional[float] = None
        self._closed = False

    def __enter__(self):
        self._start_time = time.time()
        self.tracer._log_span_start(self)

        # Open the Omium span (best-effort; swallow any SDK error).
        if self.tracer._omium_tracer is not None:
            try:
                attrs = {
                    "workflow_id": self.workflow_id,
                    "parent_span_id": self.parent_id,
                    "internal_span_id": self.span_id,
                    **self.metadata,
                }
                # OmiumTracer.span(name, input=None, span_type="default",
                # **attributes) — spread our attrs as kwargs.
                self._omium_span_cm = self.tracer._omium_tracer.span(
                    self.name,
                    input=self.metadata or None,
                    span_type="step",
                    **attrs,
                )
                self._omium_span = self._omium_span_cm.__enter__()
            except Exception as e:
                print(f"[TRACING] omium span open failed: {e}")
                self._omium_span_cm = None
                self._omium_span = None

        # Always write the local span_start record.
        self.tracer._write_local(
            "span_start",
            {
                "span_id": self.span_id,
                "parent_id": self.parent_id,
                "workflow_id": self.workflow_id,
                "name": self.name,
                "metadata": self.metadata,
            },
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._closed:
            return False
        self._closed = True
        duration = time.time() - (self._start_time or time.time())
        status = "error" if exc_type else "success"

        if self._omium_span_cm is not None:
            try:
                self._omium_span_cm.__exit__(exc_type, exc, tb)
            except Exception as e:
                print(f"[TRACING] omium span close failed: {e}")

        record: dict[str, Any] = {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "workflow_id": self.workflow_id,
            "name": self.name,
            "duration_s": round(duration, 4),
            "elapsed_s": round(duration, 4),
            "status": status,
        }
        if exc is not None:
            record["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )[-1500:],
            }
        self.tracer._write_local("span_end", record)
        self.tracer._log_span_end(self, duration, status)
        return False  # never suppress exceptions

    # -- event -----------------------------------------------------------
    def event(self, name, data=None):
        if self._omium_span is not None:
            try:
                self._omium_span.add_event(name=name, attributes=data or {})
            except Exception as e:
                print(f"[TRACING] omium event failed: {e}")
        self.tracer._write_local(
            "event",
            {
                "span_id": self.span_id,
                "parent_id": self.span_id,
                "workflow_id": self.workflow_id,
                "name": name,
                "data": data or {},
            },
        )
        self.tracer._log_event(name)

    # -- nested span -----------------------------------------------------
    def child_span(self, name, metadata=None):
        return SpanContext(
            name=name,
            metadata=metadata,
            parent_id=self.span_id,
            workflow_id=self.workflow_id,
            tracer=self.tracer,
        )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_tracer_lock = threading.Lock()
_tracer_singleton: Optional[Tracer] = None


def get_tracer() -> Tracer:
    """Return the process-wide ``Tracer`` (creating it on first access)."""
    global _tracer_singleton
    if _tracer_singleton is None:
        with _tracer_lock:
            if _tracer_singleton is None:
                _tracer_singleton = Tracer()
    return _tracer_singleton


def reset_tracer() -> None:
    """Drop the cached tracer + init flag. Used in tests that mutate env."""
    global _tracer_singleton, _initialized
    with _tracer_lock:
        if _tracer_singleton is not None:
            try:
                _tracer_singleton.shutdown()
            except Exception:
                pass
        _tracer_singleton = None
    with _init_lock:
        _initialized = False
    try:
        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass


__all__ = [
    "Tracer",
    "WorkflowHandle",
    "SpanContext",
    "get_tracer",
    "reset_tracer",
]
