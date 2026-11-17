# victim_app

A deliberately fragile URL-shortener used as the target of the autonomous-SRE
pipeline. The app boots in one of four modes, selected by the `BUG_MODE`
environment variable, so we can demo a different failure scenario without
touching the source tree.

## Endpoints

- `GET  /healthz` — liveness probe, returns `{"status": "healthy"}`
- `POST /shorten` — body `{"url": "https://..."}`, returns `{"short": "abc123", "original": "..."}`
- `GET  /expand/{short}` — look up a previously shortened code
- `GET  /stats` — returns `{"redis_url": ..., "count": ...}` (Redis is faked via env var)

## Bug modes

Run with one of:

```bash
# Happy path — all tests pass.
BUG_MODE=none uvicorn victim_app.app:app --port 9000

# Null-deref: POST /shorten with {"url": null} crashes the handler.
BUG_MODE=null_deref uvicorn victim_app.app:app --port 9000

# Regression: /shorten returns {"result": ...} instead of {"short": ...}, and
# /expand internally references the old key and raises KeyError.
BUG_MODE=regression uvicorn victim_app.app:app --port 9000

# Config error: app fails to start because it reads os.environ["REDIS_URL"]
# but the deployment only sets REDIS_URI.
REDIS_URI=redis://fake BUG_MODE=config_error uvicorn victim_app.app:app --port 9000
```

If launching from inside the `victim_app/` directory (the way the Dockerfile
does), use `uvicorn app:app --port 9000` instead — `app.py` and `monitor.py`
are top-level modules in the container.

## Tests

Tests live under `victim_app/tests/test_app.py` and are designed to be run
against any `BUG_MODE`:

```bash
# Should pass cleanly.
BUG_MODE=none pytest victim_app/tests

# Should fail at test_shorten_null (expects 500 instead of 422).
BUG_MODE=null_deref pytest victim_app/tests

# Should fail at test_shorten_valid (response missing the "short" key).
BUG_MODE=regression pytest victim_app/tests
```

The Verifier agent uses these pass/fail patterns to confirm that a proposed
patch actually addresses the seeded bug.

## Webhook

`monitor.py` installs a FastAPI middleware that POSTs any unhandled exception
to `ORCHESTRATOR_WEBHOOK_URL` (default `http://localhost:8000/webhooks/alert`)
as fire-and-forget JSON with a 2-second timeout. The webhook receiver is
implemented elsewhere; if it isn't running yet, the POST simply fails silently
and the original 500 still reaches the client.

## Docker

```bash
docker build -t victim_app victim_app/
docker run --rm -p 9000:9000 -e BUG_MODE=none victim_app
```
