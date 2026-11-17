"""Demo control CLI for the autonomous-sre system.

A single interactive menu that drives the orchestrator + victim_app
end-to-end. Designed for live demos and judge Q&A where you want zero
typing of curl commands and a clean reset between runs.

Two ways to run it::

    python scripts/demo.py                       # interactive menu
    python scripts/demo.py --trigger null_deref  # one-shot, exit on terminal status
    python scripts/demo.py --reset               # clean slate, exit
    python scripts/demo.py --status              # status table, exit
    python scripts/demo.py --demo-sequence       # scripted null_deref →
                                                 # regression → config_error,
                                                 # final summary table, exit

Design notes
------------
* All subprocesses (orchestrator, victim_app) are tracked at module
  scope so [2] / [q] / SIGINT can always tear them down. We also kill
  anything still bound to :8000 / :9000 defensively in case a previous
  presenter forgot to quit cleanly.
* HTTP calls use httpx with a 10s timeout. None of the demo's user-
  visible behavior depends on a single request succeeding — every call
  is wrapped so the menu never crashes on a transient network blip.
* The triggering logic for null_deref / regression mirrors what
  ``smoke_test.sh`` does so the demo and the smoke test are
  byte-for-byte equivalent from the orchestrator's point of view. The
  config_error path posts a synthetic startup-failure alert directly
  (victim_app can't boot under that BUG_MODE).
* The "unknown" path posts an asyncio-style RuntimeError that doesn't
  match any of the three real classifier categories, so the Triager
  routes it to ``escalated``.
* [12] clean slate is the secret sauce for repeatable demos: it wipes
  ``sre.db``, closes every ``autosre/*`` PR, deletes the branches, and
  posts a Slack note (if configured). Run it before every take.

This script intentionally has no dependencies beyond what
``requirements.txt`` already pins: rich, httpx, pygithub. ``argparse``
is stdlib.
"""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional

import httpx
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

# Repo root = parent of the scripts/ directory holding this file.
ROOT = Path(__file__).resolve().parent.parent

ORCH_PORT = 8000
VICTIM_PORT = 9000
ORCH_URL = f"http://127.0.0.1:{ORCH_PORT}"
VICTIM_URL = f"http://127.0.0.1:{VICTIM_PORT}"
DASHBOARD_URL = f"{ORCH_URL}/"

HTTP_TIMEOUT = 10.0

# Status values that mean "the pipeline has stopped moving on its own"
# (success or terminal failure). Used by the live poller in [4]/[5]/[6]/[7]
# to decide when to stop refreshing.
TERMINAL_STATUSES = {
    "released",
    "release_failed",
    "escalated",
    "investigation_failed",
    "fix_failed",
    "fix_rejected",
    "verification_failed",
}

# Mapping from user-facing bug name to (victim BUG_MODE, expected
# classification). config_error has no victim because the app can't
# boot — we POST a synthetic alert instead.
BUG_MODES = {
    "null_deref": ("null_deref", "null_deref"),
    "regression": ("regression", "recent_regression"),
    "config_error": (None, "config_error"),
}


console = Console()


# ---------------------------------------------------------------------------
# Module-level subprocess registry
# ---------------------------------------------------------------------------
# We keep a single dict of running subprocesses keyed by name so any
# code path (menu options, atexit, signal handlers) can clean them up
# uniformly. ``Popen`` objects are stored directly so we can call
# ``terminate()`` / ``kill()`` and check ``poll()``.

_processes: dict[str, subprocess.Popen] = {}


def _python_bin() -> str:
    """Path to the venv Python, falling back to the current interpreter.

    Demos almost always run inside ``.venv``; smoke_test.sh wires
    everything through ``.venv/bin/uvicorn``, so matching that here
    keeps environments consistent. If the venv isn't there, we fall
    back to ``sys.executable`` so the CLI still imports cleanly in CI.
    """
    venv_py = ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    # Linux CI workspace uses .venv-linux; fall through to that too.
    venv_linux = ROOT / ".venv-linux" / "bin" / "python"
    if venv_linux.exists():
        return str(venv_linux)
    return sys.executable


def _uvicorn_bin() -> str:
    """Path to the venv uvicorn binary, falling back to ``python -m uvicorn``."""
    venv_uv = ROOT / ".venv" / "bin" / "uvicorn"
    if venv_uv.exists():
        return str(venv_uv)
    venv_linux_uv = ROOT / ".venv-linux" / "bin" / "uvicorn"
    if venv_linux_uv.exists():
        return str(venv_linux_uv)
    venv_win_uv = ROOT / ".venv" / "Scripts" / "uvicorn.exe"
    if venv_win_uv.exists():
        return str(venv_win_uv)
    return ""  # signal to caller: use python -m


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """Cheap TCP probe — True if *something* is listening on (host, port)."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _kill_port(port: int) -> None:
    """Belt-and-suspenders: kill any process bound to *port*.

    Used by [2] and on shutdown. We rely on ``lsof -ti`` (BSD + most
    Linux distros ship it), or ``netstat`` + ``taskkill`` on Windows, and
    silently no-op if they aren't available — the calling code never
    depends on this succeeding.
    """
    if os.name == "nt":
        _kill_port_windows(port)
        return
    try:
        out = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = [p for p in out.stdout.split() if p.strip()]
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # lsof not installed (rare) or hung — nothing to do.
        pass


def _kill_port_windows(port: int) -> None:
    """Windows variant of :func:`_kill_port` using netstat + taskkill."""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    pids: set[str] = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        # Proto  Local Address  Foreign Address  State  PID
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            if parts[4].isdigit() and parts[4] != "0":
                pids.add(parts[4])
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", pid],
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass


def _http_get(path: str) -> Optional[httpx.Response]:
    """GET ``ORCH_URL + path`` with the standard timeout, swallowing errors.

    Returns the response on success, ``None`` on any failure. The menu
    treats ``None`` as "service unreachable" and shows a friendly
    message rather than a traceback.

    Catches a broad ``Exception`` rather than only ``httpx.HTTPError``
    because httpx can also raise ImportError-style configuration
    failures (e.g. when SOCKS proxy support is requested but the
    optional ``socksio`` dependency isn't installed) and we never want
    those bubbling up into the demo's UI.
    """
    try:
        return httpx.get(f"{ORCH_URL}{path}", timeout=HTTP_TIMEOUT)
    except Exception:  # noqa: BLE001 — robustness over precision here
        return None


def _http_post(path: str, json_body: dict) -> Optional[httpx.Response]:
    try:
        return httpx.post(
            f"{ORCH_URL}{path}", json=json_body, timeout=HTTP_TIMEOUT
        )
    except Exception:  # noqa: BLE001
        return None


def _orchestrator_alive() -> bool:
    """True iff GET /health returns 200."""
    r = _http_get("/health")
    return bool(r and r.status_code == 200)


def _victim_alive() -> bool:
    """True iff the victim app's /healthz returns 200."""
    try:
        r = httpx.get(f"{VICTIM_URL}/healthz", timeout=HTTP_TIMEOUT)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Subprocess management
# ---------------------------------------------------------------------------


def _start_orchestrator() -> bool:
    """Launch the orchestrator + dashboard on :8000.

    Returns True iff /health goes green within 15s. We don't tear the
    subprocess down on failure — the caller can inspect logs and try
    again. Stdout/stderr are sent to /dev/null because the demo doesn't
    want a wall of uvicorn output on the presenter's terminal.
    """
    if _orchestrator_alive():
        console.print(
            "[yellow]Orchestrator is already running on "
            f"{ORCH_URL}[/yellow]"
        )
        return True

    uv = _uvicorn_bin()
    if uv:
        cmd = [
            uv,
            "orchestrator.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(ORCH_PORT),
            "--log-level",
            "warning",
        ]
    else:
        cmd = [
            _python_bin(),
            "-m",
            "uvicorn",
            "orchestrator.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(ORCH_PORT),
            "--log-level",
            "warning",
        ]

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    log_path = ROOT / ".demo_orch.log"
    log_fh = open(log_path, "ab", buffering=0)

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    _processes["orchestrator"] = proc

    # Poll /health for up to 15s.
    deadline = time.time() + 15
    with console.status(
        "[cyan]Booting orchestrator + dashboard...[/cyan]"
    ):
        while time.time() < deadline:
            if _orchestrator_alive():
                console.print(
                    f"[green]✅ Orchestrator + dashboard live at "
                    f"{DASHBOARD_URL}[/green]"
                )
                return True
            if proc.poll() is not None:
                console.print(
                    "[red]❌ Orchestrator process exited before becoming "
                    f"healthy. See {log_path} for details.[/red]"
                )
                return False
            time.sleep(0.3)

    console.print(
        f"[red]❌ Orchestrator did not become healthy within 15s. "
        f"See {log_path} for details.[/red]"
    )
    return False


def _start_victim(bug_mode: str) -> bool:
    """Launch victim_app on :9000 with the given BUG_MODE.

    A no-op if the victim is already alive. Returns True on success.
    Used by [4] / [5] only — config_error never starts the victim.
    """
    if _victim_alive():
        console.print(
            "[yellow]victim_app is already running on "
            f"{VICTIM_URL} — reusing it[/yellow]"
        )
        return True

    uv = _uvicorn_bin()
    victim_dir = ROOT / "victim_app"
    if uv:
        cmd = [
            uv,
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(VICTIM_PORT),
            "--app-dir",
            str(victim_dir),
            "--log-level",
            "warning",
        ]
    else:
        cmd = [
            _python_bin(),
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(VICTIM_PORT),
            "--app-dir",
            str(victim_dir),
            "--log-level",
            "warning",
        ]

    env = os.environ.copy()
    env["BUG_MODE"] = bug_mode
    env["ORCHESTRATOR_WEBHOOK_URL"] = f"{ORCH_URL}/webhooks/alert"
    env["PYTHONPATH"] = str(victim_dir) + os.pathsep + env.get("PYTHONPATH", "")

    log_path = ROOT / ".demo_victim.log"
    log_fh = open(log_path, "ab", buffering=0)

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    _processes["victim_app"] = proc

    deadline = time.time() + 15
    with console.status(
        f"[cyan]Booting victim_app (BUG_MODE={bug_mode})...[/cyan]"
    ):
        while time.time() < deadline:
            if _victim_alive():
                console.print(
                    f"[green]✅ victim_app live on {VICTIM_URL} "
                    f"(BUG_MODE={bug_mode})[/green]"
                )
                return True
            if proc.poll() is not None:
                # Most common cause: config_error baked into the venv,
                # but that path doesn't reach this function.
                console.print(
                    "[red]❌ victim_app exited before becoming healthy. "
                    f"See {log_path}.[/red]"
                )
                return False
            time.sleep(0.3)

    console.print(
        "[red]❌ victim_app did not become healthy within 15s.[/red]"
    )
    return False


def _stop_everything() -> None:
    """Kill every tracked subprocess + anything still bound to our ports.

    Idempotent. Called from [2], on quit, on Ctrl+C, and via atexit.
    """
    for name, proc in list(_processes.items()):
        if proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                # Already dead, permissions issue, whatever — keep
                # cleaning the rest up.
                pass
        _processes.pop(name, None)

    # Belt-and-suspenders: kill anything *else* that snuck onto these
    # ports (e.g. a previous demo run that crashed without cleanup).
    _kill_port(ORCH_PORT)
    _kill_port(VICTIM_PORT)


# Register the cleanup with atexit so even an unexpected SystemExit
# tears subprocesses down.
atexit.register(_stop_everything)


# ---------------------------------------------------------------------------
# Settings loading (best-effort)
# ---------------------------------------------------------------------------


def _settings() -> Any:
    """Return the orchestrator Settings object, or None if unavailable.

    Wrapping this lets the CLI keep working in CI / wheel-build
    environments where pydantic-settings might fail to import. None of
    the menu's *core* behavior depends on this — only the status
    table and Slack-on-reset features do.
    """
    try:
        # Make sure ROOT is importable. In production it always is
        # (uvicorn sets it), but the CLI sometimes runs from random
        # cwds during testing.
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from orchestrator.config import get_settings

        return get_settings()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_header() -> Panel:
    """Top banner showing live service status + dashboard URL."""
    orch_alive = _orchestrator_alive()
    victim_alive = _victim_alive()

    orch_status = (
        "[green]● running[/green]" if orch_alive else "[dim]○ stopped[/dim]"
    )
    victim_status = (
        "[green]● running[/green]" if victim_alive else "[dim]○ stopped[/dim]"
    )

    body = Text.from_markup(
        f"[bold]🤖  AUTONOMOUS SRE — DEMO CONTROL[/bold]\n"
        f"Orchestrator: {orch_status}    Victim app: {victim_status}\n"
        f"Dashboard: [cyan]{DASHBOARD_URL}[/cyan]"
    )
    return Panel(body, border_style="cyan", padding=(0, 1))


def _render_menu() -> None:
    """Print the menu beneath the header. Plain Text — no tables — so
    the visual matches the prompt's mockup exactly.
    """
    lines = [
        "",
        "[bold] SYSTEM[/bold]",
        "  [cyan][1][/cyan] Start orchestrator + dashboard",
        "  [cyan][2][/cyan] Stop everything",
        "  [cyan][3][/cyan] Status check (are services healthy?)",
        "",
        "[bold] TRIGGER AN INCIDENT[/bold]",
        "  [cyan][4][/cyan] 💥 null_deref     — None value crash",
        "  [cyan][5][/cyan] 💥 regression     — recent commit broke a contract",
        "  [cyan][6][/cyan] 💥 config_error   — missing environment variable",
        "  [cyan][7][/cyan] 💥 unknown        — unclassifiable (escalation demo)",
        "",
        "[bold] INSPECT[/bold]",
        "  [cyan][8][/cyan] Show latest incident (full detail)",
        "  [cyan][9][/cyan] List recent incidents",
        "  [cyan][10][/cyan] Open dashboard in browser",
        "  [cyan][11][/cyan] Open latest PR in browser",
        "",
        "[bold] RESET[/bold]",
        "  [cyan][12][/cyan] 🧹 Clean slate (wipe DB, close stale PRs, "
        "delete autosre/* branches)",
        "",
        "[bold] SCRIPTED DEMO[/bold]",
        "  [cyan][13][/cyan] 🎬 Run full demo sequence "
        "(null_deref → regression → config_error)",
        # ``[q]`` has to be escaped — rich's markup parser treats
        # bracketed letters as opening tags, so an unescaped ``[q]``
        # gets eaten as an unknown tag. Numeric markers like ``[1]``
        # don't trigger this because they begin with a digit.
        "  [cyan]\\[q][/cyan] Quit",
        "",
    ]
    for line in lines:
        console.print(line)


# ---------------------------------------------------------------------------
# Status check (option 3 / --status)
# ---------------------------------------------------------------------------


def cmd_status() -> None:
    """Render two tables of component health: services + API keys.

    Used by [3] and ``--status``. Output is intentionally laid out for
    screenshots: a small banner, then a "Services" table (process
    health), then an "API Keys" table (config sanity). Each row's status
    is a single bold glyph so it reads cleanly when shrunk into a video
    frame.
    """
    settings = _settings()

    orch_alive = _orchestrator_alive()
    victim_alive = _victim_alive()

    # ---- Banner ----------------------------------------------------------
    overall_ok = orch_alive  # victim is "expected stopped" between runs
    overall_color = "green" if overall_ok else "yellow"
    overall_glyph = "✅" if overall_ok else "⚠️"
    console.print(
        Panel(
            Text.from_markup(
                f"[bold]{overall_glyph}  SYSTEM STATUS[/bold]\n"
                f"[dim]Orchestrator + dashboard:[/dim] {ORCH_URL}\n"
                f"[dim]Victim app:[/dim] {VICTIM_URL}"
            ),
            border_style=overall_color,
            padding=(0, 1),
        )
    )

    # ---- Services --------------------------------------------------------
    svc_table = Table(
        title="Services",
        title_style="bold",
        show_header=True,
        header_style="bold cyan",
        expand=False,
    )
    svc_table.add_column("Component", style="bold")
    svc_table.add_column("Status", justify="left")
    svc_table.add_column("Endpoint", style="dim")

    def _svc_row(label: str, ok: bool, endpoint: str, expected: str) -> None:
        if ok:
            badge = "[bold green]● UP[/bold green]"
        else:
            badge = f"[bold dim]○ {expected}[/bold dim]"
        svc_table.add_row(label, badge, endpoint)

    _svc_row("Orchestrator", orch_alive, f"{ORCH_URL}/health", "DOWN")
    # Victim being "down" between recordings is the *expected* state —
    # render it neutrally so the presenter doesn't see scary red ✗.
    _svc_row(
        "Victim app",
        victim_alive,
        f"{VICTIM_URL}/healthz",
        "ready (auto-started on trigger)",
    )

    console.print(svc_table)

    # ---- API keys --------------------------------------------------------
    keys_table = Table(
        title="API Keys & Integrations",
        title_style="bold",
        show_header=True,
        header_style="bold cyan",
        expand=False,
    )
    keys_table.add_column("Key", style="bold")
    keys_table.add_column("Status", justify="left")
    keys_table.add_column("Notes", style="dim")

    def _key_row(label: str, ok: bool, notes: str, required: bool) -> None:
        if ok:
            badge = "[bold green]✓ set[/bold green]"
        elif required:
            badge = "[bold red]✗ missing[/bold red]"
        else:
            badge = "[bold yellow]– optional[/bold yellow]"
        keys_table.add_row(label, badge, notes)

    if settings is not None:
        _key_row(
            "OPENAI_API_KEY",
            bool(settings.openai_api_key),
            "powers all LLM agents",
            required=True,
        )
        _key_row(
            "GITHUB_TOKEN / GITHUB_REPO",
            bool(settings.github_token and settings.github_repo),
            f"repo = {settings.github_repo or 'unset'}",
            required=True,
        )
        _key_row(
            "OMIUM_API_KEY",
            bool(settings.omium_api_key),
            "tracing (falls back to local JSONL)",
            required=False,
        )
        _key_row(
            "SLACK_WEBHOOK_URL",
            bool(settings.slack_webhook_url),
            "incident notifications",
            required=False,
        )
    else:
        # Settings couldn't load — show the env vars directly so the
        # operator at least knows what's set.
        _key_row(
            "OPENAI_API_KEY",
            bool(os.environ.get("OPENAI_API_KEY")),
            "from env",
            required=True,
        )
        _key_row(
            "GITHUB_TOKEN / GITHUB_REPO",
            bool(os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO")),
            "from env",
            required=True,
        )
        _key_row(
            "OMIUM_API_KEY",
            bool(os.environ.get("OMIUM_API_KEY")),
            "from env",
            required=False,
        )
        _key_row(
            "SLACK_WEBHOOK_URL",
            bool(os.environ.get("SLACK_WEBHOOK_URL")),
            "from env",
            required=False,
        )

    console.print(keys_table)

    # ---- Bottom-line summary --------------------------------------------
    if orch_alive:
        console.print(
            "[bold green]Ready to record.[/bold green] "
            f"[dim]Dashboard: {DASHBOARD_URL}[/dim]"
        )
    else:
        console.print(
            "[bold yellow]Orchestrator is not running.[/bold yellow] "
            "[dim]Pick [1] in the menu, or run "
            "'python scripts/demo.py' and choose [1] first.[/dim]"
        )


# ---------------------------------------------------------------------------
# Incident triggering (options 4-7)
# ---------------------------------------------------------------------------


def _trigger_null_deref() -> bool:
    """Fire the canonical null_deref bug. Returns True on success."""
    if not _start_victim("null_deref"):
        return False
    console.print("[cyan]⚡ Sending null url to victim_app /shorten...[/cyan]")
    try:
        httpx.post(
            f"{VICTIM_URL}/shorten",
            json={"url": None},
            timeout=HTTP_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        # The endpoint *should* crash — the request failing or returning
        # 500 is expected. The orchestrator will receive the alert
        # through the victim's middleware.
        pass
    return True


def _trigger_regression() -> bool:
    """Fire the regression bug — /shorten then /expand."""
    if not _start_victim("regression"):
        return False
    console.print("[cyan]⚡ Sending /shorten to victim_app...[/cyan]")
    short_code = "deadbe"
    try:
        r = httpx.post(
            f"{VICTIM_URL}/shorten",
            json={"url": "https://example.com/regression"},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code < 500:
            payload = r.json()
            # The buggy /shorten returns {"result": "..."}; the fixed
            # version returns {"short": "..."}. Try both.
            short_code = (
                payload.get("short") or payload.get("result") or short_code
            )
    except Exception:  # noqa: BLE001
        pass

    console.print(
        f"[cyan]⚡ Sending /expand/{short_code} to trigger the crash...[/cyan]"
    )
    try:
        httpx.get(f"{VICTIM_URL}/expand/{short_code}", timeout=HTTP_TIMEOUT)
    except Exception:  # noqa: BLE001
        pass
    return True


def _trigger_config_error() -> bool:
    """POST a synthetic startup-failure alert directly to /webhooks/alert.

    victim_app can't actually boot with BUG_MODE=config_error, so the
    smoke test (and we) fabricate the alert the way a startup-probe
    monitoring system would have emitted it.
    """
    body = {
        "endpoint": "STARTUP",
        "method": "STARTUP",
        "exception_class": "KeyError",
        "exception_message": "REDIS_URL",
        "traceback": (
            "Traceback (most recent call last):\n"
            '  File "victim_app/app.py", line 39, in <module>\n'
            '    REDIS_URL = os.environ["REDIS_URL"]\n'
            '  File "/usr/lib/python3.11/os.py", line 685, in __getitem__\n'
            "    raise KeyError(key) from None\n"
            "KeyError: 'REDIS_URL'"
        ),
        "request_body": "{}",
        "source_service": "victim_app",
    }
    console.print(
        "[cyan]⚡ Posting synthetic startup-failure alert to "
        "/webhooks/alert...[/cyan]"
    )
    r = _http_post("/webhooks/alert", body)
    if r is None or r.status_code >= 400:
        console.print(
            "[red]❌ Failed to post synthetic alert to orchestrator.[/red]"
        )
        return False
    return True


def _trigger_unknown() -> bool:
    """Post an unclassifiable alert — should land in 'escalated'.

    Uses an asyncio RuntimeError that doesn't fit null_deref / regression
    / config_error. The Triager should hand it to a human.
    """
    body = {
        "endpoint": "GET /healthcheck/internal",
        "method": "GET",
        "path": "/healthcheck/internal",
        "exception_class": "RuntimeError",
        "exception_message": "Event loop is closed",
        "traceback": (
            "Traceback (most recent call last):\n"
            '  File "victim_app/app.py", line 142, in healthcheck\n'
            "    return await self._poll_downstream()\n"
            '  File "victim_app/app.py", line 88, in _poll_downstream\n'
            "    fut = asyncio.ensure_future(self._client.get(url))\n"
            '  File "/usr/lib/python3.11/asyncio/tasks.py", line 691, in '
            "ensure_future\n"
            "    loop = events.get_event_loop()\n"
            "RuntimeError: Event loop is closed"
        ),
        "request_body": "{}",
        "source_service": "victim_app",
    }
    console.print(
        "[cyan]⚡ Posting unclassifiable RuntimeError alert...[/cyan]"
    )
    r = _http_post("/webhooks/alert", body)
    if r is None or r.status_code >= 400:
        console.print("[red]❌ Failed to post alert.[/red]")
        return False
    return True


def _latest_incident() -> Optional[dict]:
    """Return the most-recent incident dict, or None if there are none."""
    r = _http_get("/webhooks/alert")
    if r is None or r.status_code >= 400:
        return None
    try:
        items = r.json()
    except ValueError:
        return None
    if not items:
        return None
    return items[0]


def _incident_detail(incident_id: str) -> Optional[dict]:
    """Full detail blob (incident + agent_runs) for one incident."""
    r = _http_get(f"/webhooks/alert/{incident_id}")
    if r is None or r.status_code >= 400:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# Pipeline stage labels used by the live status display. Order matters —
# the renderer marks every stage up to (and including) the current one
# as "done" by simple ordinal comparison.
STAGE_LABELS = [
    ("received", "received"),
    ("classified", "classified"),
    ("investigated", "investigated"),
    ("fix_drafted", "fix_drafted"),
    ("verified", "verified"),
    ("released", "released"),
]
# Map from raw status -> the "milestone" stage it represents in the
# live display. Lots of pipeline statuses share a milestone — e.g.
# pending_implementation is still in the classified phase.
STATUS_TO_STAGE = {
    "received": "received",
    "queued": "received",
    "classified": "classified",
    "pending_implementation": "classified",
    "investigating": "classified",
    "investigated": "investigated",
    "pending_fix": "investigated",
    "fixing": "investigated",
    "fix_drafted": "fix_drafted",
    "verifying": "fix_drafted",
    "pending_release": "verified",
    "verified": "verified",
    "released": "released",
    # Terminal failure statuses keep the stage at whatever was
    # furthest along — they're rendered as a red marker instead.
}


def _render_stage_line(current_status: str) -> Text:
    """One-line representation of the pipeline progress."""
    stage = STATUS_TO_STAGE.get(current_status)
    if stage is None:
        # An unrecognized status (e.g. a terminal failure). Show it
        # in red so the operator notices.
        return Text.from_markup(
            f"[red]● {current_status}[/red]"
        )

    try:
        idx = [s for s, _ in STAGE_LABELS].index(stage)
    except ValueError:
        idx = -1

    parts: list[str] = []
    for i, (_, label) in enumerate(STAGE_LABELS):
        if i < idx:
            parts.append(f"[green]✓ {label}[/green]")
        elif i == idx:
            parts.append(f"[bold yellow]● {label}[/bold yellow]")
        else:
            parts.append(f"[dim]○ {label}[/dim]")
    return Text.from_markup(
        f"  {'  →  '.join(parts)}    [dim](status={current_status})[/dim]"
    )


def _poll_to_terminal(incident_id: str) -> Optional[dict]:
    """Live-update the pipeline status until the incident reaches a
    terminal state, then return the full detail blob.

    Returns ``None`` if the orchestrator becomes unreachable.
    """
    last_status = ""
    detail: Optional[dict] = None
    deadline = time.time() + 240  # ~4 minutes — generous; releaser can be slow

    with Live(
        Text.from_markup("[dim]Waiting for orchestrator...[/dim]"),
        console=console,
        refresh_per_second=4,
        transient=False,
    ) as live:
        while time.time() < deadline:
            detail = _incident_detail(incident_id)
            if detail is None:
                live.update(
                    Text.from_markup(
                        "[yellow]⚠ Orchestrator unreachable — "
                        "retrying...[/yellow]"
                    )
                )
                time.sleep(3)
                continue

            incident = detail.get("incident") or {}
            status = incident.get("status") or ""
            last_status = status

            live.update(_render_stage_line(status))

            if status in TERMINAL_STATUSES:
                break
            time.sleep(3)

    if detail is None:
        return None
    if last_status not in TERMINAL_STATUSES:
        console.print(
            "[yellow]⚠ Stopped polling after 4 minutes — incident "
            f"still in status={last_status}.[/yellow]"
        )
    return detail


def _print_terminal_summary(detail: dict) -> None:
    """Pretty box at the end of a triggered incident run."""
    incident = detail.get("incident") or {}
    runs = detail.get("agent_runs") or []

    classification = incident.get("classification") or "<none>"
    suspect_file = incident.get("suspect_file") or "<none>"
    pr_url = incident.get("pr_url") or "<no PR>"
    status = incident.get("status") or "<unknown>"

    # Total duration: oldest run start -> newest run finish.
    started = [r.get("started_at") for r in runs if r.get("started_at")]
    finished = [r.get("finished_at") for r in runs if r.get("finished_at")]
    duration_str = "<unknown>"
    if started and finished:
        try:
            from datetime import datetime

            t0 = min(datetime.fromisoformat(s) for s in started)
            t1 = max(datetime.fromisoformat(s) for s in finished)
            duration_str = f"{(t1 - t0).total_seconds():.1f}s"
        except (ValueError, TypeError):
            pass

    body = Text.from_markup(
        f"[bold]Incident:[/bold] {incident.get('id', '<?>')}\n"
        f"[bold]Final status:[/bold] {status}\n"
        f"[bold]Classification:[/bold] {classification}\n"
        f"[bold]Suspect file:[/bold] {suspect_file}\n"
        f"[bold]PR:[/bold] [cyan]{pr_url}[/cyan]\n"
        f"[bold]Total duration:[/bold] {duration_str}"
    )
    border = "green" if status == "released" else (
        "yellow" if status == "escalated" else "red"
    )
    console.print(Panel(body, title="📋 Run complete", border_style=border))


def _run_trigger(bug: str) -> Optional[dict]:
    """Top-level driver for [4]/[5]/[6]/[7]: fire bug, poll, summarize.

    Returns the full incident-detail dict (``{"incident": ..., "agent_runs": ...}``)
    on success, or ``None`` on any failure. Callers that just want the
    interactive behavior can ignore the return value; the demo-sequence
    runner uses it to assemble a final summary table.
    """
    if not _orchestrator_alive():
        console.print(
            "[yellow]⚠ Orchestrator not running — pick [1] first.[/yellow]"
        )
        return None

    triggered = False
    if bug == "null_deref":
        triggered = _trigger_null_deref()
    elif bug == "regression":
        triggered = _trigger_regression()
    elif bug == "config_error":
        triggered = _trigger_config_error()
    elif bug == "unknown":
        triggered = _trigger_unknown()
    else:
        console.print(f"[red]Unknown bug type: {bug}[/red]")
        return None

    if not triggered:
        return None

    console.print(
        f"[bold cyan]⚡ Incident triggered. Watch the dashboard: "
        f"{DASHBOARD_URL}[/bold cyan]"
    )

    # Give the orchestrator a moment to persist the incident before we
    # ask for "the latest".
    time.sleep(1.5)
    latest = _latest_incident()
    if latest is None:
        console.print(
            "[red]❌ No incident appeared on the orchestrator. "
            "Check the orchestrator log.[/red]"
        )
        return None

    incident_id = latest.get("id")
    if not incident_id:
        console.print("[red]❌ Latest incident has no id.[/red]")
        return None

    detail = _poll_to_terminal(incident_id)
    if detail is None:
        console.print(
            "[red]❌ Could not retrieve final incident detail.[/red]"
        )
        return None

    incident = detail.get("incident") or {}

    if bug == "unknown" and incident.get("status") == "escalated":
        console.print(
            "[bold yellow]🔼 Incident escalated to human — no autonomous "
            "fix attempted (this is correct behavior).[/bold yellow]"
        )

    _print_terminal_summary(detail)
    return detail


# ---------------------------------------------------------------------------
# Full demo sequence (option 13 / --demo-sequence)
# ---------------------------------------------------------------------------


# Bugs included in the scripted sequence, in the order the recording
# wants them. All three are expected to terminate in "released" — the
# happy-path showcase.
DEMO_SEQUENCE_BUGS: list[str] = ["null_deref", "regression", "config_error"]
# Pause between bugs so the recording has a beat between PR-merged
# moments. 3s matches the spec.
DEMO_SEQUENCE_PAUSE_SEC: float = 3.0


def cmd_demo_sequence() -> None:
    """Hands-off scripted run of all three bug types end-to-end.

    Triggers null_deref → regression → config_error, polling each one to
    its terminal state and pausing 3s between them. Prints a final
    summary table with every incident's classification, status, PR URL,
    and per-incident duration, plus the total wall-clock elapsed.

    Used by option [13] in the menu and by the ``--demo-sequence`` CLI
    flag. Designed to be the single command the presenter runs while
    the camera is rolling — no further input required.
    """
    if not _orchestrator_alive():
        console.print(
            "[yellow]⚠ Orchestrator not running — starting it now...[/yellow]"
        )
        if not _start_orchestrator():
            console.print(
                "[red]❌ Could not start orchestrator. Aborting "
                "demo sequence.[/red]"
            )
            return

    console.print(
        Panel(
            Text.from_markup(
                "[bold]🎬  STARTING FULL DEMO SEQUENCE[/bold]\n"
                f"Bugs: {', '.join(DEMO_SEQUENCE_BUGS)}\n"
                f"Pause between bugs: {DEMO_SEQUENCE_PAUSE_SEC:.0f}s\n"
                f"Dashboard: [cyan]{DASHBOARD_URL}[/cyan]"
            ),
            border_style="magenta",
            padding=(0, 1),
        )
    )

    sequence_start = time.time()
    results: list[tuple[str, Optional[dict]]] = []

    for i, bug in enumerate(DEMO_SEQUENCE_BUGS):
        console.print(
            f"\n[bold magenta]━━━ [{i + 1}/{len(DEMO_SEQUENCE_BUGS)}] "
            f"{bug} ━━━[/bold magenta]"
        )
        detail = _run_trigger(bug)
        results.append((bug, detail))

        if i < len(DEMO_SEQUENCE_BUGS) - 1:
            console.print(
                f"[dim]Pausing {DEMO_SEQUENCE_PAUSE_SEC:.0f}s before next "
                "bug...[/dim]"
            )
            time.sleep(DEMO_SEQUENCE_PAUSE_SEC)

    total_elapsed = time.time() - sequence_start

    # ---- Final summary table -------------------------------------------
    table = Table(
        title="🎬  Demo Sequence — Final Summary",
        title_style="bold magenta",
        show_header=True,
        header_style="bold cyan",
        expand=False,
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("Bug", style="bold")
    table.add_column("Classification")
    table.add_column("Status")
    table.add_column("Duration", justify="right")
    table.add_column("PR URL", overflow="fold")

    released_count = 0
    pr_urls: list[str] = []
    for i, (bug, detail) in enumerate(results, start=1):
        if detail is None:
            table.add_row(
                str(i),
                bug,
                "[red]<failed>[/red]",
                "[red]<no detail>[/red]",
                "-",
                "-",
            )
            continue
        incident = detail.get("incident") or {}
        runs = detail.get("agent_runs") or []
        classification = incident.get("classification") or "<none>"
        status = incident.get("status") or "<unknown>"
        pr_url = incident.get("pr_url") or "-"
        if status == "released":
            released_count += 1
            status_cell = f"[bold green]{status}[/bold green]"
        elif status == "escalated":
            status_cell = f"[bold yellow]{status}[/bold yellow]"
        else:
            status_cell = f"[bold red]{status}[/bold red]"
        if pr_url and pr_url != "-":
            pr_urls.append(pr_url)

        # Per-incident duration: oldest start -> newest finish.
        duration_str = "-"
        started = [r.get("started_at") for r in runs if r.get("started_at")]
        finished = [r.get("finished_at") for r in runs if r.get("finished_at")]
        if started and finished:
            try:
                from datetime import datetime

                t0 = min(datetime.fromisoformat(s) for s in started)
                t1 = max(datetime.fromisoformat(s) for s in finished)
                duration_str = f"{(t1 - t0).total_seconds():.1f}s"
            except (ValueError, TypeError):
                pass

        table.add_row(
            str(i),
            bug,
            classification,
            status_cell,
            duration_str,
            pr_url,
        )

    console.print(table)

    # ---- Bottom-line panel ---------------------------------------------
    border = "green" if released_count == len(DEMO_SEQUENCE_BUGS) else "yellow"
    glyph = "✅" if released_count == len(DEMO_SEQUENCE_BUGS) else "⚠️"
    console.print(
        Panel(
            Text.from_markup(
                f"[bold]{glyph}  Sequence complete[/bold]\n"
                f"Incidents triggered: [bold]{len(results)}[/bold]\n"
                f"Reached 'released': [bold]{released_count}[/bold] / "
                f"[bold]{len(results)}[/bold]\n"
                f"PR URLs collected: [bold]{len(pr_urls)}[/bold]\n"
                f"Total elapsed: [bold]{total_elapsed:.1f}s[/bold]"
            ),
            border_style=border,
            padding=(0, 1),
        )
    )

    # Plain-text PR URL listing — easy to copy/paste from a recording
    # frame or CI log even when rich formatting is stripped.
    if pr_urls:
        console.print("[bold]PR URLs:[/bold]")
        for url in pr_urls:
            console.print(f"  • {url}")


# ---------------------------------------------------------------------------
# Inspection (options 8 / 9)
# ---------------------------------------------------------------------------


def cmd_show_latest() -> None:
    """Pretty-print every relevant field of the most recent incident."""
    if not _orchestrator_alive():
        console.print(
            "[yellow]⚠ Orchestrator not running — pick [1] first.[/yellow]"
        )
        return
    latest = _latest_incident()
    if latest is None:
        console.print("[dim]No incidents yet.[/dim]")
        return
    detail = _incident_detail(latest["id"])
    if detail is None:
        console.print("[red]Could not fetch incident detail.[/red]")
        return

    incident = detail.get("incident") or {}
    runs = detail.get("agent_runs") or []

    table = Table(
        title=f"Incident {incident.get('id', '<?>')}",
        show_header=False,
        title_style="bold",
    )
    table.add_column("Field", style="bold cyan")
    table.add_column("Value", overflow="fold")
    for field in (
        "id",
        "classification",
        "severity",
        "status",
        "suspect_file",
        "root_cause",
        "fix_explanation",
        "fix_risk_level",
        "pr_url",
        "workflow_id",
    ):
        value = incident.get(field)
        if value is None:
            value = "<null>"
        # Truncate runaway free-text fields so the table stays readable.
        if isinstance(value, str) and len(value) > 400:
            value = value[:397] + "..."
        table.add_row(field, str(value))
    console.print(table)

    if runs:
        runs_table = Table(
            title="Agent runs",
            show_header=True,
            header_style="bold cyan",
        )
        runs_table.add_column("Agent")
        runs_table.add_column("Status")
        runs_table.add_column("Duration")
        for r in runs:
            duration = ""
            started = r.get("started_at") or ""
            finished = r.get("finished_at") or ""
            if started and finished:
                try:
                    from datetime import datetime

                    t0 = datetime.fromisoformat(started)
                    t1 = datetime.fromisoformat(finished)
                    duration = f"{(t1 - t0).total_seconds():.2f}s"
                except (ValueError, TypeError):
                    duration = ""
            runs_table.add_row(
                r.get("agent_name") or "<?>",
                r.get("status") or "<?>",
                duration,
            )
        console.print(runs_table)


def cmd_list_recent() -> None:
    """Last 10 incidents, oldest of those at the bottom."""
    if not _orchestrator_alive():
        console.print(
            "[yellow]⚠ Orchestrator not running — pick [1] first.[/yellow]"
        )
        return
    r = _http_get("/webhooks/alert")
    if r is None:
        console.print("[red]Could not reach orchestrator.[/red]")
        return
    try:
        items = r.json()
    except ValueError:
        items = []
    items = items[:10]
    if not items:
        console.print("[dim]No incidents yet.[/dim]")
        return

    table = Table(
        title="Recent incidents",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("ID")
    table.add_column("Classification")
    table.add_column("Status")
    table.add_column("PR URL", overflow="fold")
    for inc in items:
        full_id = inc.get("id") or ""
        short_id = full_id[:16] if full_id else "<?>"
        table.add_row(
            short_id,
            inc.get("classification") or "<none>",
            inc.get("status") or "<?>",
            inc.get("pr_url") or "-",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Browser openers (options 10 / 11)
# ---------------------------------------------------------------------------


def cmd_open_dashboard() -> None:
    console.print(f"[cyan]Opening {DASHBOARD_URL}...[/cyan]")
    try:
        webbrowser.open(DASHBOARD_URL)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not open browser: {exc}[/red]")


def cmd_open_latest_pr() -> None:
    if not _orchestrator_alive():
        console.print(
            "[yellow]⚠ Orchestrator not running — pick [1] first.[/yellow]"
        )
        return
    latest = _latest_incident()
    if latest is None:
        console.print("[dim]No incidents yet.[/dim]")
        return
    pr_url = latest.get("pr_url")
    if not pr_url:
        console.print("[yellow]No PR on the latest incident.[/yellow]")
        return
    console.print(f"[cyan]Opening {pr_url}...[/cyan]")
    try:
        webbrowser.open(pr_url)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not open browser: {exc}[/red]")


# ---------------------------------------------------------------------------
# Clean slate (option 12 / --reset)
# ---------------------------------------------------------------------------


def cmd_clean_slate(skip_confirm: bool = False) -> None:
    """Wipe sre.db, close every autosre/* PR, delete its branch, ping Slack.

    The single most important option for repeatable demos. Always
    confirms before destroying anything (unless --reset was passed).
    """
    if not skip_confirm:
        ok = Confirm.ask(
            "[bold red]This wipes the DB and closes all autosre PRs. "
            "Continue?[/bold red]",
            default=False,
        )
        if not ok:
            console.print("[dim]Cancelled.[/dim]")
            return

    settings = _settings()

    # Step 1: delete sre.db if it exists. The orchestrator recreates
    # tables on the next boot via init_db().
    db_path = ROOT / "sre.db"
    if db_path.exists():
        try:
            db_path.unlink()
            console.print(f"[green]✓[/green] Deleted {db_path}")
        except OSError as exc:
            console.print(f"[red]Could not delete {db_path}: {exc}[/red]")
    else:
        console.print(f"[dim]No {db_path} to delete[/dim]")

    # Step 2: close every open autosre/* PR via PyGithub.
    closed_prs = 0
    deleted_branches = 0
    if (
        settings is not None
        and settings.github_token
        and settings.github_repo
    ):
        try:
            from github import Github  # type: ignore

            gh = Github(settings.github_token)
            repo = gh.get_repo(settings.github_repo)
            for pr in repo.get_pulls(state="open"):
                head = pr.head.ref if pr.head else ""
                if not head.startswith("autosre/"):
                    continue
                try:
                    pr.edit(state="closed")
                    closed_prs += 1
                    console.print(
                        f"[green]✓[/green] Closed PR #{pr.number} "
                        f"({head})"
                    )
                except Exception as exc:  # noqa: BLE001
                    console.print(
                        f"[yellow]⚠ Could not close PR #{pr.number}: "
                        f"{exc}[/yellow]"
                    )

                # Best-effort branch delete. Some repos protect branches;
                # don't let one failure abort the whole reset.
                try:
                    ref = repo.get_git_ref(f"heads/{head}")
                    ref.delete()
                    deleted_branches += 1
                    console.print(
                        f"[green]✓[/green] Deleted branch {head}"
                    )
                except Exception as exc:  # noqa: BLE001
                    console.print(
                        f"[yellow]⚠ Could not delete branch {head}: "
                        f"{exc}[/yellow]"
                    )
        except ImportError:
            console.print(
                "[yellow]⚠ PyGithub not available — skipping PR "
                "cleanup.[/yellow]"
            )
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]⚠ GitHub cleanup failed: {exc}[/yellow]"
            )
    else:
        console.print(
            "[dim]GitHub not configured — skipping PR cleanup.[/dim]"
        )

    # Step 3: Slack ping (entirely optional).
    if settings is not None and settings.slack_webhook_url:
        from orchestrator.tools.slack_tool import SlackNotifier

        result = SlackNotifier(
            settings.slack_webhook_url, timeout_s=HTTP_TIMEOUT
        ).post_sync("🧹 Demo environment reset")
        if result.ok:
            console.print("[green]✓[/green] Slack notification sent")
        else:
            reason = result.error or f"HTTP {result.status_code}"
            console.print(
                f"[yellow]⚠ Slack notification failed: {reason}[/yellow]"
            )
    else:
        console.print("[dim]Slack not configured — skipping ping.[/dim]")

    # Single-line confirmation summary the presenter can screenshot or
    # grep out of CI logs. Printed in addition to the panel so it lands
    # in plain stdout even when rich formatting is stripped.
    db_marker = "wiped" if not db_path.exists() else "kept"
    summary_line = (
        f"✅ Clean slate complete — DB {db_marker}, "
        f"{closed_prs} PR(s) closed, {deleted_branches} branch(es) deleted."
    )
    console.print(
        Panel(
            Text.from_markup(
                f"[bold]Database:[/bold] {db_marker}\n"
                f"[bold]PRs closed:[/bold] {closed_prs}\n"
                f"[bold]Branches deleted:[/bold] {deleted_branches}\n"
                f"[bold]Ready for next take.[/bold]"
            ),
            title="🧹 Clean slate complete",
            border_style="green",
        )
    )
    console.print(f"[bold green]{summary_line}[/bold green]")


# ---------------------------------------------------------------------------
# Menu dispatch + main loop
# ---------------------------------------------------------------------------


# Set of all valid menu inputs; used by the input loop to re-prompt on
# anything else without crashing.
VALID_CHOICES = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13",
    "q", "Q", "quit", "exit",
}


def handle_menu_choice(choice: str) -> bool:
    """Run the action for *choice* and return True iff the loop should
    keep running.

    Anything outside the known set just re-prompts (returns True).
    Quit returns False after offering to stop services.
    """
    choice = (choice or "").strip()
    if choice not in VALID_CHOICES:
        console.print(f"[yellow]Unknown option: {choice!r}[/yellow]")
        return True

    if choice == "1":
        _start_orchestrator()
    elif choice == "2":
        _stop_everything()
        console.print("[green]✓ Everything stopped.[/green]")
    elif choice == "3":
        cmd_status()
    elif choice == "4":
        _run_trigger("null_deref")
    elif choice == "5":
        _run_trigger("regression")
    elif choice == "6":
        _run_trigger("config_error")
    elif choice == "7":
        _run_trigger("unknown")
    elif choice == "8":
        cmd_show_latest()
    elif choice == "9":
        cmd_list_recent()
    elif choice == "10":
        cmd_open_dashboard()
    elif choice == "11":
        cmd_open_latest_pr()
    elif choice == "12":
        cmd_clean_slate()
    elif choice == "13":
        cmd_demo_sequence()
    elif choice in {"q", "Q", "quit", "exit"}:
        # Offer to stop services before quitting if any are still up.
        if _orchestrator_alive() or _victim_alive():
            stop = Confirm.ask(
                "Services still running. Stop them before quitting?",
                default=True,
            )
            if stop:
                _stop_everything()
        return False

    return True


def interactive_loop() -> None:
    """Render the menu, prompt, dispatch, repeat. Survives Ctrl+C."""
    while True:
        console.print(_render_header())
        _render_menu()
        try:
            choice = Prompt.ask("Choose an option")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C / Ctrl+D at the prompt: clean up and exit.
            console.print()
            if _orchestrator_alive() or _victim_alive():
                console.print("[dim]Stopping services...[/dim]")
                _stop_everything()
            return

        try:
            keep_going = handle_menu_choice(choice)
        except KeyboardInterrupt:
            # Ctrl+C during a long action (e.g. live poller): bail out
            # of that action but return to the menu rather than quitting.
            console.print("\n[yellow]Interrupted.[/yellow]")
            keep_going = True
        except Exception as exc:  # noqa: BLE001 — last line of defense
            console.print(
                f"[red]Unexpected error: {exc}[/red] "
                "[dim](returning to menu)[/dim]"
            )
            keep_going = True

        if not keep_going:
            return

        console.print()  # blank line before redrawing


# ---------------------------------------------------------------------------
# argparse entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser. Public so tests can import it without running ``main``."""
    parser = argparse.ArgumentParser(
        prog="demo.py",
        description="Demo control CLI for autonomous-sre.",
    )
    parser.add_argument(
        "--trigger",
        choices=["null_deref", "regression", "config_error", "unknown"],
        help="Fire one incident, poll to terminal, exit.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe DB + close autosre PRs (no confirm), exit.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print component status table and exit.",
    )
    parser.add_argument(
        "--demo-sequence",
        action="store_true",
        help=(
            "Run the full scripted demo: null_deref → regression → "
            "config_error, each polled to terminal status, with a final "
            "summary table. The hands-off mode for the recording."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point. With no flags, launches the interactive menu."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Install a SIGINT handler so Ctrl+C at any time cleans up.
    def _on_sigint(_signum, _frame):  # type: ignore[no-untyped-def]
        # Don't reuse this handler's body for the interactive loop —
        # KeyboardInterrupt there is caught by Python natively. This is
        # just for non-interactive mode.
        console.print("\n[yellow]Interrupted — cleaning up...[/yellow]")
        _stop_everything()
        raise SystemExit(130)

    signal.signal(signal.SIGINT, _on_sigint)

    if args.status:
        cmd_status()
        return 0

    if args.reset:
        cmd_clean_slate(skip_confirm=True)
        return 0

    if args.demo_sequence:
        # Like --trigger, the scripted sequence needs an orchestrator.
        # cmd_demo_sequence() will start one if needed, but doing it
        # eagerly here gives a cleaner failure message.
        if not _orchestrator_alive():
            if not _start_orchestrator():
                return 1
        cmd_demo_sequence()
        return 0

    if args.trigger:
        # In non-interactive mode the presenter still needs the
        # orchestrator running. Try to start it if it isn't already.
        if not _orchestrator_alive():
            if not _start_orchestrator():
                return 1
        _run_trigger(args.trigger)
        # Keep the orchestrator subprocess alive after the script
        # exits? No — atexit will stop it, which matches the "fire and
        # forget" expectation of a scripted demo.
        return 0

    # No flags: interactive menu.
    interactive_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
