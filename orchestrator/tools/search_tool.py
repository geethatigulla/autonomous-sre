"""Web search tool backed by Tavily for investigator research.

The Investigator uses this to look up unusual exception classes, library
quirks, and known regressions. Tavily is sync, so the call is dispatched
through ``asyncio.to_thread``. When the API key is missing the tool is
disabled — the Investigator's system prompt is aware of that and is
expected to keep going on traceback evidence alone.

All failures are swallowed and recorded as span events so a slow / dead
Tavily endpoint never blocks the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

try:
    from tavily import TavilyClient

    _TAVILY_IMPORTABLE = True
except Exception:  # pragma: no cover - defensive
    TavilyClient = None  # type: ignore[assignment]
    _TAVILY_IMPORTABLE = False


log = logging.getLogger("orchestrator.tools.search")


_CONTENT_CHAR_CAP = 500
_EVENT_TRUNCATE = 500


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]


class WebSearchTool:
    """Tavily-backed web search with graceful degradation."""

    def __init__(self, api_key: Optional[str], tracer: Any) -> None:
        self.tracer = tracer
        self.client: Any = None
        if not _TAVILY_IMPORTABLE:
            log.warning("tavily-python not importable — WebSearchTool disabled")
            return
        if not api_key or not api_key.strip():
            log.info("Tavily API key missing — WebSearchTool disabled")
            return
        try:
            self.client = TavilyClient(api_key)
        except Exception as e:  # noqa: BLE001
            log.warning("TavilyClient init failed: %s — disabling", e)
            self.client = None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _open_child(self, span: Any, name: str, metadata: dict) -> Any:
        if span is not None:
            try:
                return span.child_span(name, metadata=metadata)
            except Exception:  # pragma: no cover
                pass

        class _Noop:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, et, e, tb):
                return False

            def event(self_inner, *_args, **_kwargs):
                return None

        return _Noop()

    def _sync_search(self, query: str, max_results: int) -> dict:
        # TavilyClient.search returns a dict with a "results" key.
        return self.client.search(query=query, max_results=int(max_results))

    async def search(
        self,
        query: str,
        max_results: int = 5,
        span: Any = None,
    ) -> list[dict]:
        """Return ranked search results or an empty list on any failure."""
        with self._open_child(
            span,
            "tavily.search",
            {"query": (query or "")[:100]},
        ) as child:
            child.event(
                "input",
                {
                    "query": (query or "")[:_EVENT_TRUNCATE],
                    "max_results": int(max_results),
                },
            )
            if self.client is None:
                child.event("search_disabled", {})
                return []
            try:
                resp = await asyncio.to_thread(
                    self._sync_search, query, max_results
                )
            except Exception as e:  # noqa: BLE001
                child.event(
                    "search_error",
                    {"error": str(e)[:300], "type": type(e).__name__},
                )
                return []

            raw_results = []
            if isinstance(resp, dict):
                raw_results = resp.get("results") or []
            elif isinstance(resp, list):
                raw_results = resp

            out: list[dict] = []
            for r in raw_results:
                if not isinstance(r, dict):
                    continue
                out.append(
                    {
                        "title": r.get("title", "") or "",
                        "url": r.get("url", "") or "",
                        "content": _truncate(r.get("content", "") or "", _CONTENT_CHAR_CAP),
                        "score": r.get("score"),
                    }
                )
            preview = ", ".join(
                (item.get("url") or "")[:60] for item in out[:3]
            )
            child.event(
                "output",
                {"count": len(out), "preview": preview[:_EVENT_TRUNCATE]},
            )
            return out


__all__ = ["WebSearchTool"]
