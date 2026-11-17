# Autonomous SRE

**A multi-agent system that takes an alert from production all the way to a merged-ready pull request — with zero human input in the loop.**

It's 3 AM. Your URL-shortener just crashed on a null input. Instead of paging
the on-call engineer, a webhook fires into this orchestrator: five agents
classify the failure, read the offending code on GitHub, draft and verify a
fix in a sandbox, open a real PR, and ping Slack — usually in under a minute.
A human reviews and merges; the engineer keeps sleeping.

## What it does

The orchestrator runs a deterministic state machine across five LLM-driven
agents that consume real production-style alerts and produce real
side-effects. It is webhook-driven, fully autonomous end-to-end, and is
designed to fail loudly and safely: every agent's output is verified by the
next one, and the final step is a Pull Request — never a direct push to
`main`.

- **Multi-agent** — five specialised agents (Triager, Investigator, two
  parallel Fix Authors, Verifier, Releaser), each with a focused prompt and a
  narrow toolbelt.
- **Autonomous, webhook-driven** — incidents arrive over HTTP, are dispatched
  to an async background task, and traverse the pipeline without human
  intervention.
- **Real side-effects** — reads source via the GitHub API, opens a real Pull
  Request on the configured repo, posts a Slack notification.
- **Parallel hypothesis racing** — two Fix Authors with different "fix
  philosophies" (defensive vs. permissive) draft patches concurrently; the
  first one whose patch passes the Verifier wins.
- **Sandbox verification** — every candidate patch is applied with
  `git apply` to a tempdir copy of the source tree and validated by the
  victim's own pytest suite. The LLM never gets to claim its own fix works.
- **Full tracing** — every span (LLM call, tool call, sandbox run, GitHub
  mutation) ships to Omium when configured, with a transparent local-JSONL
  fallback so a tracing outage never breaks the pipeline.
- **Live dashboard** — Server-Sent Events stream every state transition,
  agent run, and verdict to a single-page dashboard at `/`.

## Architecture

```
                                                      ┌─────────────────┐
                                                      │  Fix Author     │
                                                      │  (defensive)    │
                                                      │  gpt-4o         │
                                                      └────────┬────────┘
                                                               │
victim app crash                                        ┌──────┴───────┐
        │                                               │   FIX RACE   │
        ▼                                               │  (parallel)  │
┌────────────────┐                                      └──────┬───────┘
│   exception    │                                             │
│  middleware    │                                      ┌──────┴────────┐
│ (victim_app)   │                                      │  Fix Author   │
└────────┬───────┘                                      │  (permissive) │
         │                                              │  gpt-4o       │
         │ POST /webhooks/alert                         └───────┬───────┘
         ▼                                                      │
┌────────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  Orchestrator  │───▶│   Triager    │───▶│ Investigator │──────┤
│   (FastAPI,    │    │  gpt-4o-mini │    │   gpt-4o     │      │
│    SQLite)     │    └──────────────┘    └──────────────┘      │
└───────┬────────┘                                              │
        │                                                       ▼
        │            ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
        └───────────▶│   Releaser   │◀───│   Verifier   │◀───│  winner of   │
                     │  gpt-4o-mini │    │ deterministic│    │   the race   │
                     │              │    │  (sandbox +  │    └──────────────┘
                     │  → GitHub PR │    │   pytest)    │
                     │  → Slack     │    └──────────────┘
                     └──────────────┘
```

**Triager** (`gpt-4o-mini`) — classifies an alert into one of `null_deref`,
`recent_regression`, `config_error`, or `unknown`, assigns a severity, and
emits self-reported confidence. No tools.

**Investigator** (`gpt-4o`) — uses OpenAI tool-calling against `GitHubTool`
(read files, list recent commits, view a commit's diff) and `WebSearchTool`
(Tavily) to localize the bug to a specific file and produce ranked
hypotheses. Tool-call budget capped at 5.

**Fix Author × 2** (`gpt-4o`, parallel) — two instances run concurrently with
different system prompts. The *defensive* strategy prefers fail-fast / raise;
the *permissive* strategy prefers graceful fallback. Each emits a unified
diff as a single LLM round-trip, no tool use.

**Verifier** (deterministic, no LLM) — copies the source tree into a
`tempfile.TemporaryDirectory`, runs `git init` so `git apply` is happy, applies
the candidate diff, then runs the project's own pytest suite. Emits one of
`verified`, `tests_failed`, `patch_invalid`, `sandbox_error`.

**Releaser** (`gpt-4o-mini`) — composes the PR title, body, commit message,
and Slack copy in one round-trip, then uses `PatchTool` (in-memory unified-diff
applier) and PyGithub to create the branch, commit the new blob, and open the
PR. Slack post is best-effort.

The **orchestrator** is a single FastAPI process backed by SQLite (`sre.db`).
Webhook ingress writes an `Incident` row, returns 202 immediately, and
schedules `handle_incident` as an asyncio background task. The state machine
walks each incident through `received → classified → investigated →
fix_drafted → verified → released`, persisting an `AgentRun` row before and
after every agent. The pipeline is *fail-stop*: any non-success agent
verdict moves the incident to a terminal state and halts the dispatch.

## The 5 agents

| Agent | Model | Responsibility | Tools used |
|---|---|---|---|
| Triager | `gpt-4o-mini` | Classify the alert and pick a downstream pipeline | none |
| Investigator | `gpt-4o` | Root-cause analysis — pinpoint suspect file/line and rank fix hypotheses | `GitHubTool`, `WebSearchTool` |
| Fix Author (defensive) | `gpt-4o` | Draft a unified diff biased toward fail-fast / raise on bad input | `GitHubTool` (read suspect file) |
| Fix Author (permissive) | `gpt-4o` | Draft a unified diff biased toward graceful fallback | `GitHubTool` (read suspect file) |
| Verifier | deterministic | Apply patch in a tempdir sandbox; verdict is the pytest exit code | `Sandbox` (`git apply` + `pytest`) |
| Releaser | `gpt-4o-mini` | Compose PR copy, open the GitHub PR, post the Slack message | `PatchTool`, `GitHubTool`, Slack webhook |

## Bug classes handled

| Class | Trigger example | Fix approach |
|---|---|---|
| `null_deref` | `POST /shorten {"url": null}` — handler calls `url.strip()` on `None` | Add an explicit null check; return `422` instead of letting the `AttributeError` escape |
| `recent_regression` | A recent commit renamed a response key from `"short"` to `"result"`, breaking `/expand` | Revert the rename in the response shape (or rename the reader) so the contract holds again |
| `config_error` | App startup raises `KeyError: 'REDIS_URL'` because the deployment sets `REDIS_URI` | Read both keys with a fallback (or rename the lookup to match the deployed env) |
| `unknown` | An `asyncio` `RuntimeError: Event loop is closed` that fits none of the above | **Escalated to a human** — pipeline halts at `escalated`, no PR is opened |

## Quickstart

These steps take a clean machine to a green demo. Estimated time: ~5 minutes
plus model latency.

### 1. Prerequisites

- **Python 3.11+** (the orchestrator uses `tomllib` from stdlib)
- **git** on your `PATH` — the Verifier sandbox uses `git apply` to test
  each candidate patch
- **A GitHub repository to act as the "victim"** plus a Personal Access Token
  with `repo` scope. The agents will read source from this repo and open PRs
  against it. **Do not point this at a production repo.**
- **An OpenAI API key** with access to `gpt-4o` and `gpt-4o-mini`.
- **Optional:** a Tavily API key (web search during investigation), a Slack
  incoming-webhook URL (notifications on PR open), an Omium API key (remote
  tracing — local JSONL fallback works without it).

### 2. Clone and install

macOS / Linux:

```bash
git clone <your-fork-url> autonomous-sre
cd autonomous-sre
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
```

Windows (PowerShell):

```powershell
git clone <your-fork-url> autonomous-sre
cd autonomous-sre
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.lock
```

`requirements.lock` pins the exact versions the test suite passes with.
`requirements.txt` holds looser version ranges, if you'd rather resolve
them yourself. For linting, also `pip install -r requirements-dev.txt`.

### 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

Fill in `OPENAI_API_KEY`, `GITHUB_TOKEN`, and `GITHUB_REPO` at minimum. See
[Configuration](#configuration) below for the full table.

The orchestrator still boots with settings missing, so you can open the
dashboard while you fill them in. It logs a warning that names each missing
key, and `GET /ready` returns `503` with the list until everything required
is in place:

```bash
curl -s http://localhost:8000/ready
# {"ready": false, "missing_required": ["GITHUB_TOKEN", "GITHUB_REPO"], ...}
```

### 4. Seed the victim repo

The Investigator reads the live victim source from the GitHub repo named in
`GITHUB_REPO`. Push the contents of `victim_app/` into the root of that repo
so the agents can find `app.py`, `monitor.py`, and `tests/test_app.py`:

```bash
# From inside this clone:
cp -R victim_app/* /path/to/your/victim-repo/
cd /path/to/your/victim-repo/
git add . && git commit -m "Seed victim app" && git push
cd -
```

### 5. Run the interactive demo

```bash
python scripts/demo.py
```

Pick `[1]` to start the orchestrator and dashboard, then `[4]` / `[5]` /
`[6]` / `[7]` to fire each bug class. The CLI polls the orchestrator and
prints a live progress bar through `received → classified → investigated →
fix_drafted → verified → released`.

### 6. Or run end-to-end non-interactively

```bash
bash scripts/smoke_test.sh null_deref
# or: regression | config_error
```

The smoke test boots both processes, fires the bug, polls to a terminal
state, and exits non-zero if the pipeline did not produce a real GitHub PR
URL. (It expects a `.venv/bin/` layout, so it runs on macOS / Linux. On
Windows, use `python scripts/demo.py --trigger null_deref` instead.)

### 6b. Or run with Docker

```bash
cp .env.example .env        # fill in the required keys
docker compose up --build
```

This starts the orchestrator on `:8000` (its SQLite DB and traces live in
the `sre-data` volume) and the victim app on `:9000`, which reports its
crashes to the orchestrator over the compose network. Set
`BUG_MODE=null_deref` (or `regression`) before `docker compose up` to
choose the seeded bug.

### 7. Open the dashboard

```
http://localhost:8000/
```

Live SSE view of every incident, agent run, span, and final PR link.

## Demo

Each bug class has a one-keystroke trigger in the interactive CLI and a
one-line equivalent in the smoke test:

| Bug | Interactive | Non-interactive | Expected outcome |
|---|---|---|---|
| `null_deref` | `[4]` | `bash scripts/smoke_test.sh null_deref` | PR opened on `autosre/inc-<id>` branch that adds a null check |
| `recent_regression` | `[5]` | `bash scripts/smoke_test.sh regression` | PR that restores the `"short"` response key |
| `config_error` | `[6]` | `bash scripts/smoke_test.sh config_error` | PR that reads `REDIS_URI` (or both keys) |
| `unknown` | `[7]` | _(interactive only)_ | Incident reaches `escalated`, no PR — by design |

Demo CLI menu (from `python scripts/demo.py`):

```
 SYSTEM
  [1] Start orchestrator + dashboard
  [2] Stop everything
  [3] Status check (are services healthy?)

 TRIGGER AN INCIDENT
  [4]  null_deref     — None value crash
  [5]  regression     — recent commit broke a contract
  [6]  config_error   — missing environment variable
  [7]  unknown        — unclassifiable (escalation demo)

 INSPECT
  [8]  Show latest incident (full detail)
  [9]  List recent incidents
  [10] Open dashboard in browser
  [11] Open latest PR in browser

 RESET
  [12] Clean slate (wipe DB, close stale PRs, delete autosre/* branches)
  [q]  Quit
```

`[12]` is the "secret sauce" for repeatable demos: it deletes `sre.db`,
closes every open PR whose branch starts with `autosre/`, and deletes those
branches. Run it before every take.

CLI also supports one-shot flags for scripted runs:

```bash
python scripts/demo.py --status                  # status table, exit
python scripts/demo.py --trigger null_deref      # fire one bug, poll to terminal, exit
python scripts/demo.py --reset                   # wipe DB + close PRs (no confirm), exit
```

When everything works you'll see a panel at the end of a run:

```
📋 Run complete
Incident:        <uuid>
Final status:    released
Classification:  null_deref
Suspect file:    app.py
PR:              https://github.com/<owner>/<repo>/pull/42
Total duration:  37.4s
```

## Project structure

```
autonomous-sre/
├── orchestrator/                  # The FastAPI + SQLite control plane
│   ├── main.py                    # App entry, lifespan, /health + /ready, router mounts
│   ├── config.py                  # Settings (pydantic-settings, reads .env)
│   ├── db.py                      # SQLModel engine + init_db()
│   ├── models.py                  # Incident, AgentRun row definitions
│   ├── tracing.py                 # Defensive Omium wrapper + JSONL fallback
│   ├── timeutil.py                # Timezone-aware UTC helpers + column type
│   ├── agents/                    # The five agent implementations
│   │   ├── base.py                # BaseAgent: shared _call_llm + span plumbing
│   │   ├── triager.py             # Classifier (gpt-4o-mini)
│   │   ├── investigator.py        # Tool-use root-cause analyst (gpt-4o)
│   │   ├── fix_author.py          # Defensive + permissive diff authors (gpt-4o)
│   │   ├── verifier.py            # Deterministic sandbox runner
│   │   └── releaser.py            # PR + Slack composer (gpt-4o-mini)
│   ├── tools/                     # External-side-effect wrappers
│   │   ├── github_tool.py         # Async PyGithub wrapper
│   │   ├── search_tool.py         # Tavily web search
│   │   ├── sandbox_tool.py        # Tempdir + git + pytest
│   │   ├── patch_tool.py          # In-memory unified-diff applier (strict + fuzzy)
│   │   └── slack_tool.py          # Slack incoming-webhook poster
│   ├── orchestration/
│   │   ├── state_machine.py       # The top-level incident dispatch loop
│   │   ├── parallel_fix.py        # Defensive ‖ permissive race
│   │   └── job_queue.py           # Bounded-concurrency dispatch + crash recovery
│   ├── webhooks/
│   │   └── ingress.py             # POST /webhooks/alert + GET /webhooks/alert[/{id}]
│   └── dashboard/
│       ├── routes.py              # GET /, /events/stream, /api/metrics, …
│       └── events.py              # SSE publisher
├── victim_app/                    # The deliberately-buggy URL shortener
│   ├── app.py                     # FastAPI app, BUG_MODE-selected misbehaviour
│   ├── monitor.py                 # Middleware that POSTs exceptions to /webhooks/alert
│   ├── scrubber.py                # Redacts secrets/PII before alerts leave the app
│   ├── Dockerfile
│   └── tests/test_app.py          # The pytest suite the Verifier runs
├── scripts/
│   ├── demo.py                    # Interactive demo control CLI (above)
│   ├── smoke_test.sh              # End-to-end shell smoke test
│   └── verify_trace_structure.py  # Validates the local JSONL trace schema
├── tests/                         # Pytest test suite (~100 tests, runs offline)
├── .github/workflows/ci.yml       # Lint + tests on 3.11/3.12, Docker build check
├── Dockerfile                     # Orchestrator image
├── docker-compose.yml             # Orchestrator + victim app
├── pyproject.toml                 # Project metadata, pytest + ruff config
├── requirements.txt               # Runtime deps (version ranges)
├── requirements.lock              # Exact, cross-platform pinned versions
├── requirements-dev.txt           # + lint tooling
├── omium.toml                     # Omium project config (used when shipping spans)
├── .env.example                   # Template for .env
└── README.md
```

## Configuration

Every environment variable read by the orchestrator. Set them in `.env` (or
export them); `pydantic-settings` reads from both.

| Name | Required? | Purpose | Example / default |
|---|---|---|---|
| `OPENAI_API_KEY` | yes | LLM provider for all four LLM-driven agents | `sk-...` |
| `GITHUB_TOKEN` | yes | PAT with `repo` scope; reads source and opens PRs | `ghp_...` |
| `GITHUB_REPO` | yes | The victim repo, in `owner/repo` form | `owner/repo` |
| `TAVILY_API_KEY` | no | Enables Investigator web search; agent still works without it | `tvly-...` |
| `SLACK_WEBHOOK_URL` | no | Incoming-webhook URL; Releaser posts a notification on PR open | `https://hooks.slack.com/services/...` |
| `DATABASE_URL` | no | SQLModel database URL | `sqlite:///./sre.db` |
| `MAX_CONCURRENT_INCIDENTS` | no | Pipelines allowed to run at once; extra alerts wait in line | `4` |
| `OMIUM_API_KEY` | no | Omium API key — when set, spans ship to Omium; otherwise JSONL only | _(unset)_ |
| `OMIUM_PROJECT` | no | Project slug; must match `[project].name` in `omium.toml` | `autonomous-sre` |
| `OMIUM_API_BASE` | no | Override Omium ingest URL (rarely needed) | _(unset)_ |
| `TRACE_BACKEND` | no | `auto` (default), `omium` (fail loud), or `local` (never touch SDK) | `auto` |
| `LOCAL_TRACE_PATH` | no | Where the local JSONL trace fallback writes | `orchestrator/.local_traces.jsonl` |
| `INVESTIGATOR_MAX_TOOL_CALLS` | no | Hard cap on Investigator tool calls per incident | `5` |
| `SRE_ENV_FILE` | no | Which dotenv file to read; empty string disables it (the test suite does this) | `.env` |

The victim app reads two additional env vars at runtime:

| Name | Purpose | Example |
|---|---|---|
| `BUG_MODE` | Selects the seeded misbehaviour: `none` / `null_deref` / `regression` / `config_error` | `null_deref` |
| `ORCHESTRATOR_WEBHOOK_URL` | Where the exception-reporting middleware POSTs alerts | `http://localhost:8000/webhooks/alert` |

## Observability — Omium Tracing

Tracing is not bolted on; it is how this project is debugged, demoed, and
reviewed. Every meaningful step in the orchestrator emits a span through the
Omium SDK, and the resulting traces render end-to-end on the Omium dashboard.

### What gets instrumented

Every meaningful step in the pipeline produces a span:

- **Webhook ingress** — `POST /webhooks/alert` opens the workflow span and
  publishes an `incident_received` event before the background task is
  dispatched.
- **Async dispatch** — `handle_incident` (the asyncio background task)
  becomes the root `workflow:incident_response` span; its lifetime equals
  the lifetime of the incident.
- **Every agent invocation** — Triager, Investigator, both Fix Authors,
  both Verifiers, and the Releaser each open a child span
  (`triager.classify`, `investigator.investigate`, `draft.<strategy>`,
  `verify.<strategy>`, `releaser.release`).
- **Every LLM call** — each `_call_llm` round-trip nests its own
  `llm_call` span underneath the agent that issued it, with the model
  name, prompt sizes, and round-trip duration as attributes.
- **Every tool call** — `github_read_file`, `github_list_recent_commits`,
  `web_search` (Tavily), `sandbox.apply_patch`, `sandbox.run_pytest`, the
  in-memory diff applier, GitHub PR creation, and the Slack webhook all
  open `github.<method>`, `verifier.run_tests`, `releaser.create_branch`,
  `releaser.notify_slack`, etc.

### Causal threading

Spans are not a flat list; they preserve the full causal graph of an
incident.

```
workflow:incident_response                              ← root
├── triager.classify
│   └── llm_call  (gpt-4o-mini)
├── investigator.investigate
│   ├── llm_call  (gpt-4o, tool round 1)
│   ├── github.get_file
│   ├── llm_call  (gpt-4o, tool round 2)
│   ├── github.list_recent_commits
│   └── llm_call  (gpt-4o, final JSON)
├── fix_race
│   ├── draft.defensive            ┐ run concurrently
│   │   └── llm_call (gpt-4o)      │
│   ├── draft.permissive           ┘
│   │   └── llm_call (gpt-4o)
│   ├── verify.defensive           ┐ run concurrently
│   │   ├── verifier.setup_sandbox │
│   │   ├── verifier.apply_patch   │
│   │   ├── verifier.run_tests     │
│   │   └── verifier.cleanup       │
│   └── verify.permissive          ┘
│       ├── verifier.setup_sandbox
│       ├── verifier.apply_patch
│       ├── verifier.run_tests
│       └── verifier.cleanup
└── releaser.release
    ├── releaser.compose           (llm_call)
    ├── releaser.create_branch     (github.ensure_branch)
    ├── releaser.apply_patch       (in-memory diff applier)
    ├── releaser.open_pr           (github.create_pull)
    └── releaser.notify_slack
```

The workflow is the root, agent spans are its children, LLM calls and tool
calls nest under their agent, and the parallel fix race shows both
branches under one `fix_race` span with `draft.<strategy>` and
`verify.<strategy>` children running concurrently. Every span carries its
parent's `span_id` so the Omium UI can render the tree without
reconstruction.

### Configuration

Three environment variables and one config file drive tracing:

| Variable | Purpose | Default |
|---|---|---|
| `OMIUM_API_KEY` | Authenticates the Omium ingest endpoint. Required to ship spans. | _(unset → local-only)_ |
| `OMIUM_PROJECT` | Project slug spans are bound to; must match `[project].name` in `omium.toml`. | `autonomous-sre` |
| `TRACE_BACKEND` | Selects the backend behaviour. | `auto` |

`TRACE_BACKEND` modes:

- **`auto`** (default) — try Omium if `OMIUM_API_KEY` is set; on any
  failure (missing key, malformed key, network failure, ingest 5xx) fall
  back to local JSONL without crashing the pipeline.
- **`omium`** — Omium only. If init fails or a flush errors, surface the
  error loudly. Use this in CI when you specifically want to assert the
  Omium integration is healthy.
- **`local`** — never touch the SDK; everything goes to JSONL.

`omium.toml` at the repo root holds `[project].name = "autonomous-sre"`,
which is the same string the SDK uses when binding spans to a project.
Env var wins if both are set.

### The defensive wrapper (deliberate reliability choice)

`orchestrator/tracing.py` is a deliberate wrapper around the Omium SDK
whose single design goal is: **a tracing failure must never crash the
pipeline.** The orchestrator's purpose is to remediate production
incidents at 3 AM; a 5xx from the tracing backend at that moment must not
take the remediation system down with it.

Concretely:

- The Omium import is wrapped in `try/except` so a missing SDK doesn't
  crash the orchestrator at import time.
- `omium.init()` is called inside a guarded block; on any exception we
  log and fall back to JSONL.
- `OmiumTracer.span(...)`, `set_attribute(...)`, `flush()`, and `close()`
  calls are individually wrapped — any one of them can fail and the
  workflow continues.
- Every span is *also* appended to the local JSONL writer
  (`_LocalBackend`) — so the JSONL trace is a complete audit log even
  when Omium is the active backend, not a degraded fallback.
- The fallback target is `orchestrator/.local_traces.jsonl`
  (overridable via `LOCAL_TRACE_PATH`) — one JSON object per line, with
  `kind`, `span_id`, `parent_id`, `workflow_id`, `name`, and timing
  fields ready to be re-rendered or grep'd.

On startup the orchestrator logs which backend is actually active:

```
Orchestrator ready, workflow tracing: omium
```

If you see `local` here when you expected `omium`, the SDK init failed
silently (most often `OMIUM_API_KEY` not exported in the shell that
launched uvicorn) — the pipeline is still fully traced to JSONL.

### How to view

- **Omium dashboard (primary):**
  https://app.omium.ai/automation?project=autonomous-sre. Every
  end-to-end `incident_response` run renders as a workflow with the full
  span tree shown above — click any span to see its inputs, outputs, and
  attributes (model name, prompt sizes, tool arguments, verifier
  verdicts).
- **Local dashboard (live):** `http://localhost:8000/` streams every
  workflow / agent / span event over Server-Sent Events
  (`GET /events/stream`) for in-flight observation while a demo is
  running.
- **Local JSONL (offline audit):** `orchestrator/.local_traces.jsonl`
  contains the same span structure, append-only, one JSON object per
  line. `scripts/verify_trace_structure.py` validates it against the
  expected schema; `jq` is enough for ad-hoc inspection:

  ```bash
  jq -c 'select(.kind == "span_start") | .name' \
      orchestrator/.local_traces.jsonl | sort | uniq -c
  ```

## Testing

```bash
pytest          # runs tests/ and victim_app/tests/ (configured in pyproject.toml)
ruff check .    # lint
```

The suite has about 100 tests across 14 files and covers:

- `test_end_to_end_triage.py` — webhook → Triager → state transition
- `test_investigator_flow.py` — tool-call loop, schema coercion, budget cap
- `test_fix_and_verify_flow.py` — Fix Author → Verifier handoff
- `test_parallel_race.py` — defensive ‖ permissive race semantics, winner selection
- `test_multi_pipeline.py` — all four classifications routed correctly
- `test_releaser_flow.py` — PR creation, branch idempotency, Slack opt-out
- `test_patch_tool.py` — strict and fuzzy unified-diff application
- `test_tracing.py` — Omium initialization, local fallback, swallow-all guarantee
- `test_dashboard.py` — SSE stream contracts, metrics endpoints
- `test_demo_cli.py` — argparse surface, menu dispatch
- `test_smoke.py` — every module imports, app boots with no credentials, `/ready` reports what's missing
- `test_job_queue.py` — concurrency cap, duplicate suppression, crash containment, orphan recovery
- `test_slack_tool.py` — disabled / success / non-2xx / network-error paths
- `test_scrubber.py` — secret and PII redaction in outgoing alerts
- `victim_app/tests/test_app.py` — the victim's own suite (the one the Verifier runs)

The tests use a stubbed OpenAI client, a temporary SQLite DB, and a fake
GitHub tool, so the suite runs offline and in CI without any of the
production credentials listed above. `tests/conftest.py` sets
`SRE_ENV_FILE=""` so a developer's real `.env` is never loaded during tests.
CI (`.github/workflows/ci.yml`) runs lint and tests on Python 3.11 and 3.12,
then builds both Docker images and checks that the orchestrator's `/health`
responds.

## Design decisions

- **Tempdir sandbox over Docker.** A Docker-backed verifier would be more
  isolated, but it's fragile across macOS / Linux / CI hosts. A
  `tempfile.TemporaryDirectory` + `git init` + `pytest` is reliable
  everywhere and is enough isolation given that the patches are
  bounded-size, single-file diffs.
- **Parallel fix race over single Fix Author.** Two genuinely different
  programs (raise-vs.-fallback) give the Verifier two real chances at green
  tests instead of one, and the "did anyone verify?" signal is a useful
  confidence proxy that one author can't produce.
- **Open a PR, never push to `main`.** The Releaser creates a branch
  (`autosre/inc-<short_id>`) and opens a PR. A human merges. The system is
  autonomous up to the point where merging affects production, and stops
  there on purpose.
- **The test suite is ground truth.** The Verifier doesn't read the LLM's
  self-evaluation, doesn't trust diff syntax, doesn't accept "looks good";
  it runs `pytest` and reads the exit code. If the project under repair
  doesn't have tests, this system has nothing to verify against.
- **Fail-stop pipeline.** Any non-success verdict from any agent moves the
  incident to a terminal state and halts dispatch. We never proceed past a
  failed agent on the hope that the next one will recover.
- **Defensive tracing wrapper.** The Omium SDK is wrapped in a module that
  swallows every exception and falls back to JSONL. A tracing outage at
  3 AM has zero blast radius on the actual fix pipeline.

## Limitations and future work

This is a research prototype, and the boundaries are honest:

- **Three bug classes, not N.** `null_deref`, `recent_regression`, and
  `config_error` cover a real slice of production failures, but they are
  not the whole space.
- **Requires an existing test suite in the target repo.** The Verifier is
  the only signal the orchestrator trusts; without tests there is no
  verdict.
- **Single-process orchestrator.** SQLite plus an in-process dispatcher
  (`orchestration/job_queue.py`: bounded concurrency, de-duplication,
  recovery of incidents interrupted by a restart). Fine for a demo and
  small-team usage; a multi-replica deployment would swap it for a real
  job queue (RQ / Celery / Temporal) and Postgres.
- **Concurrency and performance bugs are out of scope.** The agents reason
  about logic errors and config drift, not race conditions or latency
  regressions.

Planned next steps: generation of regression tests alongside the fix,
confidence-based auto-merge for low-risk classes, and a retry-with-feedback
loop so a `tests_failed` Verifier verdict can re-prompt the Fix Author with
the failing pytest output.

## Dependencies

| Package | Why it's here |
|---|---|
| `fastapi` + `uvicorn[standard]` | The orchestrator's HTTP server and the victim app's HTTP server |
| `pydantic` + `pydantic-settings` | Request models and `.env`-loading settings |
| `sqlmodel` | ORM for `Incident` and `AgentRun` rows in SQLite |
| `openai` | Async client for `gpt-4o` and `gpt-4o-mini` |
| `pygithub` | Reading source, listing commits, creating branches, opening PRs |
| `tavily-python` | Web search tool for the Investigator |
| `httpx` | Slack webhook POST, victim-app middleware POST, smoke test polling |
| `sse-starlette` | Server-Sent Events for the dashboard's live view |
| `omium` | Span ingestion when `OMIUM_API_KEY` is set |
| `unidiff` | Parsing unified diffs in the in-memory patch applier |
| `rich` | Interactive demo CLI rendering |
| `pytest` + `pytest-asyncio` | Test runner (and the Verifier's notion of "does this fix work?") |
| `python-dotenv` | Local `.env` loading (used by pydantic-settings) |

All third-party API usage (OpenAI, GitHub, Tavily, Slack, Omium) is billed
to the keys you provide. The orchestrator makes no calls Anthropic doesn't
need to know about — it talks directly to the providers listed above.
