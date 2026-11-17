# Incident Autopilot

[![CI](https://github.com/geethatigulla/incident-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/geethatigulla/incident-autopilot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-control%20plane-009688)

**A multi-agent system that takes a production alert all the way to a verified, ready-to-merge pull request, with no human in the loop until review.**

When a service crashes, it reports the exception to this orchestrator over a webhook. A pipeline of specialised agents then classifies the failure, reads the offending code and recent commits on GitHub, drafts two competing fixes, verifies each one by running the project's own pytest suite in a sandbox, opens a real PR for the winner and posts to Slack. Incidents it can't classify are escalated to a human instead of guessed at. Merging always stays a human decision.

The repo ships with a deliberately buggy URL shortener (`victim_app/`), so you can watch the whole pipeline work end to end.

> Formerly `autonomous-sre`. That name still appears in a few places: the Python package name, the FastAPI title, the Omium project slug (`OMIUM_PROJECT` default and `omium.toml`), and the Docker image tags used in CI.

## Features

- **Five agent roles, six agent runs per incident**: Triager, Investigator, two parallel Fix Authors (defensive and permissive), a deterministic Verifier and a Releaser. Each has a focused prompt and a narrow toolbelt.
- **Webhook-driven and asynchronous**: `POST /webhooks/alert` saves an `Incident`, returns `202` immediately and hands the incident to a bounded-concurrency background job queue. The queue suppresses duplicates and recovers incidents a restart left half-finished.
- **Verification by tests, not by the model**: each candidate diff is applied with `git apply` to a temporary copy of the source and validated by pytest (60 s timeout). The first fix whose tests pass wins.
- **Real side effects**: reads files, commits and diffs through the GitHub API, opens a PR on an `autosre/inc-<id>` branch, posts a Slack notification (optional) and runs Tavily web search (optional).
- **Fail-stop state machine**: `received → classified → investigated → fix_drafted → verified → released`. Any failed step moves the incident to a terminal status, and `unknown` incidents stop at `escalated`.
- **Live dashboard**: a single-page UI at `/`, fed by Server-Sent Events, with metrics and recent incidents.
- **Tracing**: every LLM call, tool call, sandbox run and GitHub action is a span in one causal trace per incident. Spans ship to Omium when it's configured and are always written to a local JSONL file. Tracing failures can never break the pipeline.
- **Secret scrubbing**: the victim app redacts API keys, bearer tokens, JWTs, private keys, passwords, email addresses and card numbers from crash reports before sending them.
- **Boots without credentials**: every tool reports itself disabled when its key is missing. `GET /ready` lists what's missing, and the whole test suite runs offline.

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ (CI tests 3.11 and 3.12; Docker images use 3.12) |
| Web | FastAPI + Uvicorn (orchestrator and victim app) |
| Data | SQLite via SQLModel / SQLAlchemy |
| AI | OpenAI `gpt-4o` (Investigator, Fix Authors) and `gpt-4o-mini` (Triager, Releaser), with tool calling and JSON output |
| Integrations | PyGithub, Tavily, Slack incoming webhooks |
| Verification | `git apply` + pytest in a temp-directory sandbox; `unidiff` for the in-memory patch applier |
| Live UI | `sse-starlette` + a static HTML dashboard |
| Observability | Omium SDK, with a local JSONL trace that is always written |
| Tooling | pytest + pytest-asyncio, ruff, Docker / Docker Compose, GitHub Actions, `rich` (demo CLI) |

## Project structure

```text
incident-autopilot/
├── orchestrator/                 # FastAPI + SQLite control plane
│   ├── main.py                   # App entry, lifespan, /health and /ready, router mounts
│   ├── config.py                 # Settings (pydantic-settings, reads .env)
│   ├── db.py / models.py         # SQLModel engine; Incident and AgentRun tables
│   ├── tracing.py                # Defensive Omium wrapper + local JSONL backend
│   ├── agents/                   # triager, investigator, fix_author, verifier, releaser, base
│   ├── tools/                    # github_tool, search_tool (Tavily), sandbox_tool, patch_tool, slack_tool
│   ├── orchestration/            # state_machine, parallel_fix (fix race), job_queue
│   ├── webhooks/ingress.py       # POST/GET /webhooks/alert
│   └── dashboard/                # routes (/, SSE, metrics, test trigger), events, static/index.html
├── victim_app/                   # Deliberately buggy URL shortener (BUG_MODE-selected bugs)
│   ├── app.py / monitor.py       # App + middleware that POSTs exceptions to the orchestrator
│   ├── scrubber.py               # Redacts secrets/PII from outgoing alerts
│   └── tests/test_app.py         # The suite the Verifier runs
├── scripts/
│   ├── demo.py                   # Interactive demo CLI (rich)
│   ├── smoke_test.sh             # End-to-end smoke test (macOS/Linux)
│   └── verify_trace_structure.py # Validates the local JSONL trace structure
├── tests/                        # Offline pytest suite
├── DEMO_RUNBOOK.md               # Operator checklist for recording a demo
├── Dockerfile / docker-compose.yml
├── requirements.txt              # Runtime deps (version ranges)
├── requirements.lock             # Exact pins (used by Docker and CI)
├── requirements-dev.txt          # + ruff
├── omium.toml                    # Omium project config
└── .env.example                  # Configuration template
```

## Getting started

### Prerequisites

- **Python 3.11+**
- **git** on your `PATH`. The Verifier sandbox runs `git init` / `git apply`.
- **A GitHub repository to act as the victim**, plus a personal access token with `repo` scope. The agents read source from this repo and open PRs against it. **Never point it at a production repository.**
- **An OpenAI API key** with access to `gpt-4o` and `gpt-4o-mini`.
- Optional: a Tavily API key, a Slack incoming-webhook URL, an Omium API key.

### Install

```bash
git clone https://github.com/geethatigulla/incident-autopilot.git
cd incident-autopilot
python3.11 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.lock     # exact pins; or requirements.txt for ranges
pip install -r requirements-dev.txt  # optional: ruff
```

### Configure

```bash
cp .env.example .env
```

Orchestrator settings (read by `orchestrator/config.py` from the environment or `.env`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | yes | none | LLM provider for the Triager, Investigator, Fix Authors and Releaser |
| `GITHUB_TOKEN` | yes | none | PAT with `repo` scope; reads source and opens PRs |
| `GITHUB_REPO` | yes | none | Victim repo in `owner/repo` form (the `owner/repo` placeholder counts as missing) |
| `TAVILY_API_KEY` | no | none | Enables Investigator web search |
| `SLACK_WEBHOOK_URL` | no | none | Releaser posts here when a PR opens |
| `DATABASE_URL` | no | `sqlite:///./sre.db` | SQLModel database URL |
| `MAX_CONCURRENT_INCIDENTS` | no | `4` | Pipelines allowed to run at once; extra alerts queue up |
| `INVESTIGATOR_MAX_TOOL_CALLS` | no | `5` | Hard cap on Investigator tool calls per incident |
| `OMIUM_API_KEY` | no | none | When set, spans also ship to Omium |
| `OMIUM_PROJECT` | no | `autonomous-sre` | Omium project slug; should match `[project].name` in `omium.toml` |
| `OMIUM_API_BASE` | no | none | Override the Omium ingest URL |
| `TRACE_BACKEND` | no | `auto` | `auto` (Omium, falling back to JSONL), `omium` (fail loudly) or `local` (never touch the SDK) |
| `LOCAL_TRACE_PATH` | no | `orchestrator/.local_traces.jsonl` | Local JSONL trace file |
| `SRE_ENV_FILE` | no | `.env` | Dotenv file to read; an empty string disables it (the tests do this) |

Victim app settings:

| Variable | Default | Description |
|---|---|---|
| `BUG_MODE` | `none` | `none`, `null_deref`, `regression` or `config_error` |
| `ORCHESTRATOR_WEBHOOK_URL` | `http://localhost:8000/webhooks/alert` | Where the exception middleware POSTs alerts |

The orchestrator boots even with settings missing. It logs a warning naming each missing key, and `GET /ready` returns `503` (also checking the database and `git`) until everything required is present:

```bash
curl -s http://localhost:8000/ready
```

### Seed the victim repo

The Investigator and Releaser work against the GitHub repo named in `GITHUB_REPO`, while the Verifier tests patches against the local `victim_app/` directory. Push the contents of `victim_app/` to the root of your victim repo so the two match:

```bash
cp -R victim_app/* /path/to/your/victim-repo/
cd /path/to/your/victim-repo && git add . && git commit -m "Seed victim app" && git push
```

### Run

Orchestrator only:

```bash
uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000
# dashboard: http://localhost:8000/
```

Victim app (run it from inside `victim_app/`, because `app.py` imports `monitor` as a top-level module):

```bash
cd victim_app && BUG_MODE=null_deref uvicorn app:app --port 9000
curl -X POST localhost:9000/shorten -H 'content-type: application/json' -d '{"url": null}'
```

Interactive demo CLI (starts/stops services, fires each bug class, shows progress and the resulting PR):

```bash
python scripts/demo.py
python scripts/demo.py --status                 # status table, then exit
python scripts/demo.py --trigger null_deref     # fire one bug, poll to a terminal state, exit
python scripts/demo.py --demo-sequence          # null_deref -> regression -> config_error
python scripts/demo.py --reset                  # wipe sre.db, close autosre/* PRs and delete those branches
```

The menu's option `[12]` (or `--reset`) gives a clean slate between demo runs.

End-to-end smoke test (macOS/Linux; expects `.venv/bin/`). It fails unless the pipeline reaches `released` with a GitHub PR URL:

```bash
bash scripts/smoke_test.sh null_deref     # or: regression | config_error
```

Docker Compose (orchestrator on `:8000` with data in the `sre-data` volume, victim app on `:9000`):

```bash
cp .env.example .env    # fill in the required keys
BUG_MODE=null_deref docker compose up --build
```

`config_error` crashes the victim at startup by design. Trigger that class from the dashboard, the demo CLI, or `POST /api/test/trigger?bug=config_error`.

### Test and lint

```bash
pytest          # tests/ and victim_app/tests/ (configured in pyproject.toml)
ruff check .
```

The suite (roughly 100 test cases) uses a stubbed OpenAI client, a fake GitHub tool and a temporary SQLite database, so it needs no network or credentials. CI (`.github/workflows/ci.yml`) runs ruff and pytest on Python 3.11 and 3.12, builds both Docker images and checks the orchestrator's `/health`.

## API

Orchestrator (default `http://localhost:8000`):

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhooks/alert` | Report an exception; returns `202` with an `incident_id` |
| `GET` | `/webhooks/alert` | Recent incidents |
| `GET` | `/webhooks/alert/{incident_id}` | One incident plus its agent runs |
| `GET` | `/health` | Liveness |
| `GET` | `/ready` | Readiness: config, database, git, queue stats |
| `GET` | `/` | Dashboard |
| `GET` | `/events/stream` | SSE feed |
| `GET` | `/api/metrics` | Totals, success rate, resolution time, per-class counts |
| `GET` | `/api/incidents/recent` | Recent incidents for the dashboard |
| `POST` | `/api/test/trigger?bug=<class>` | Fire a canned incident (`null_deref`, `regression`, `config_error`, `unknown`) |

```bash
curl -X POST http://localhost:8000/webhooks/alert \
  -H "content-type: application/json" \
  -d '{"source_service": "victim_app", "endpoint": "POST /shorten",
       "exception_class": "AttributeError",
       "exception_message": "NoneType object has no attribute strip",
       "traceback": "...", "request_body": "{\"url\": null}"}'
```

Victim app (default `http://localhost:9000`): `POST /shorten`, `GET /expand/{short}`, `GET /stats`, `GET /healthz`.

## How it works

```text
victim_app crash ──▶ monitor middleware (scrub) ──POST /webhooks/alert──▶ Orchestrator (FastAPI, SQLite, job queue)
                                                                              │
     Triager (gpt-4o-mini) ──▶ Investigator (gpt-4o + GitHub/Tavily tools) ◀──┘
                                       │
                 ┌─────────────────────┴─────────────────────┐
     Fix Author: defensive (gpt-4o)            Fix Author: permissive (gpt-4o)
                 │                                           │
     Verifier (sandbox + pytest)               Verifier (sandbox + pytest)
                 └──────────── first verified fix ───────────┘
                                       │
                 Releaser (gpt-4o-mini) ──▶ branch + commit + PR on GitHub, Slack post
```

| Agent | Model | Job | Tools |
|---|---|---|---|
| Triager | `gpt-4o-mini` | Classify as `null_deref`, `recent_regression`, `config_error` or `unknown`, with severity and confidence | none |
| Investigator | `gpt-4o` | Localise the bug to a file/line and rank hypotheses (tool-call budget, default 5) | GitHub (files, recent commits, commit diffs), Tavily |
| Fix Author ×2 | `gpt-4o` | Draft a unified diff, one biased to fail-fast (defensive), one to graceful fallback (permissive) | reads the suspect file from GitHub |
| Verifier | none (deterministic) | Copy source to a temp dir, `git init`, `git apply`, run pytest; verdict `verified`, `tests_failed`, `patch_invalid` or `sandbox_error` | sandbox |
| Releaser | `gpt-4o-mini` | Write PR title/body/commit message, create the `autosre/inc-<id>` branch, commit, open the PR, post to Slack | patch applier, GitHub, Slack |

Seeded bug classes in `victim_app`:

| `BUG_MODE` | Failure | Expected fix |
|---|---|---|
| `null_deref` | `POST /shorten {"url": null}` calls `.strip()` on `None` | Null check returning `422` |
| `regression` | `/shorten` returns `"result"` instead of `"short"`; `/expand` raises `KeyError` | Restore the response contract |
| `config_error` | Startup reads `os.environ["REDIS_URL"]` but only `REDIS_URI` is set | Read with a fallback / correct key |
| (`unknown` trigger) | Canned `RuntimeError: Event loop is closed` | Escalated to a human, no PR |

### Tracing

The trace tree per incident is `workflow:incident_response` → `triager.classify`, `investigator.investigate`, `fix_race` (`draft.<strategy>` and `verify.<strategy>` run concurrently), and `releaser.release`. LLM and tool spans nest under their agent. Every span is appended to the local JSONL file even when Omium is active. Startup logs `Orchestrator ready, workflow tracing: <omium|local>` to show which backend is live. To check the JSONL structure, run `python scripts/verify_trace_structure.py`. If you have access to the owner's Omium project, traces render at https://app.omium.ai/automation?project=autonomous-sre.

## Safety model

- The system never pushes to `main`. Fixes arrive as PRs on `autosre/*` branches, and a person merges them.
- A fix that doesn't pass the test suite in the sandbox is never proposed.
- Unclassifiable incidents are escalated with no PR.
- Crash reports are scrubbed of secrets and PII before they leave the victim app.
- Behaviour is bounded: an Investigator tool-call budget, a pytest timeout and a concurrency cap on the job queue.
- Use a dedicated test repo: the GitHub token you supply can open PRs.

## Limitations

- Research/demo prototype. It handles three bug classes (`null_deref`, `recent_regression`, `config_error`) against the bundled victim app.
- The Verifier always tests against the local `victim_app/` directory (`source_dir="victim_app"`), so the GitHub victim repo has to mirror it. Pointing the system at an arbitrary repo needs code changes.
- The target project needs an existing pytest suite; without tests there is no verdict.
- Single process: SQLite and an in-process job queue. Running multiple replicas would need a real queue and database.
- The temp-dir sandbox is not a container, so patches run with the orchestrator's privileges.
- `scripts/smoke_test.sh` and the demo CLI's venv detection assume a POSIX `.venv/bin/` layout. On Windows, use `python scripts/demo.py --trigger <bug>`.
- Planned (not implemented): regression-test generation, confidence-based auto-merge, and a retry loop that feeds failing pytest output back to the Fix Authors.

All third-party API usage (OpenAI, GitHub, Tavily, Slack, Omium) is billed to the keys you provide.
