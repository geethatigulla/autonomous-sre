"""Entry point for the victim app — the target of the autonomous-sre pipeline.

A tiny URL shortener that deliberately misbehaves based on the BUG_MODE
environment variable. Each handler contains a clean if/elif branch at the top
that selects between the correct implementation and one of several seeded
bugs. The bugs are intentionally NOT wrapped in try/except — the autonomous
SRE pipeline relies on the exceptions bubbling up so the monitor middleware
can capture them.

Valid BUG_MODE values:
    none          → app works correctly (default)
    null_deref    → /shorten crashes on {"url": null} via url.strip().lower()
    regression    → /shorten returns {"result": ...} instead of {"short": ...}
                    and /expand/{short} raises KeyError reading response["short"]
    config_error  → startup fails because os.environ["REDIS_URL"] is missing
                    (the deployment only sets REDIS_URI)
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from monitor import ExceptionReportingMiddleware

BUG_MODE = os.environ.get("BUG_MODE", "none")

# ---------------------------------------------------------------------------
# Startup-time bug: config_error
# ---------------------------------------------------------------------------
# In production we'd read REDIS_URL, but the seeded "config_error" bug assumes
# the deployment only set REDIS_URI (a typo'd key). Reading os.environ with
# subscript syntax raises KeyError before the app can serve a single request.
if BUG_MODE == "config_error":
    REDIS_URL = os.environ["REDIS_URL"]  # KeyError on startup — intentional
else:
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


app = FastAPI(title="victim_app", version="0.1.0")
app.add_middleware(ExceptionReportingMiddleware)


# In-memory store of short_code -> original URL. Resets per process.
_STORE: dict[str, str] = {}
_SHORTEN_COUNT = 0


class ShortenRequest(BaseModel):
    url: Optional[str] = None


def _short_code(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:6]


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe used by the Verifier agent to confirm recovery."""
    return {"status": "healthy"}


@app.post("/shorten")
async def shorten(payload: ShortenRequest) -> dict:
    global _SHORTEN_COUNT
    url = payload.url

    if BUG_MODE == "null_deref":
        # Seeded bug: no None-check before .strip().lower(). When the caller
        # sends {"url": null} this raises AttributeError: 'NoneType' object
        # has no attribute 'strip'.
        normalized = url.strip().lower()
        short = _short_code(normalized)
        _STORE[short] = normalized
        _SHORTEN_COUNT += 1
        return {"short": short, "original": normalized}

    elif BUG_MODE == "regression":
        # Seeded bug: response key renamed from "short" to "result". The HTTP
        # 200 still flows, but downstream consumers (and /expand below) break.
        if url is None:
            raise HTTPException(status_code=422, detail="url is required")
        short = _short_code(url)
        _STORE[short] = url
        _SHORTEN_COUNT += 1
        return {"result": short, "original": url}

    else:  # BUG_MODE in {"none", "config_error", anything else}
        if url is None:
            raise HTTPException(status_code=422, detail="url is required")
        short = _short_code(url)
        _STORE[short] = url
        _SHORTEN_COUNT += 1
        return {"short": short, "original": url}


@app.get("/expand/{short}")
async def expand(short: str) -> dict:
    if BUG_MODE == "regression":
        # Seeded bug: this handler internally builds the same payload shape as
        # /shorten and then reads response["short"] — which no longer exists
        # because the regression bug renamed it to "result". KeyError.
        if short not in _STORE:
            raise HTTPException(status_code=404, detail="short code not found")
        original = _STORE[short]
        response = {"result": short, "original": original}
        # Buggy lookup against the old key name.
        return {"short": response["short"], "original": response["original"]}

    if short not in _STORE:
        raise HTTPException(status_code=404, detail="short code not found")
    return {"short": short, "original": _STORE[short]}


@app.get("/stats")
async def stats() -> dict:
    """Pretend-Redis stats endpoint — just echoes the configured URL + count."""
    return {"redis_url": REDIS_URL, "count": _SHORTEN_COUNT}
