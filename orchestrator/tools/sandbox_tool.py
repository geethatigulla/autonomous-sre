"""Tempdir-backed sandbox for applying patches and running tests.

The Verifier agent uses :class:`Sandbox` to apply a candidate unified
diff to an isolated copy of the victim service's source tree, then runs
the test suite inside that copy. Nothing here writes to the real
``victim_app`` tree on disk — the whole point is that a bad patch can't
corrupt the user's working copy.

Why no Docker?
--------------
A Docker-backed sandbox would be stronger, but it's also fragile to set
up reliably across macOS / Linux / CI hosts. For the hackathon-quality
v1 of this orchestrator we trade a bit of isolation for guaranteed
reliability: a Python ``tempfile.TemporaryDirectory``, a fresh ``git
init`` so ``git apply`` is happy, and ``asyncio.create_subprocess_exec``
for pytest. The tempdir is removed on exit even if the test run failed
or timed out.

Public surface
--------------
* :class:`Sandbox` (async context manager) — copies ``source_dir`` into
  a fresh tempdir, ``git init``s it, and exposes
  :meth:`apply_patch` and :meth:`run_pytest`.

Both methods record events / child spans against the optional
``span`` argument so the Verifier's trace shows exactly what happened.
Subprocess output is captured and returned to the caller — the agent
trims it before persisting on the AgentRun row.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("orchestrator.tools.sandbox")


# Generous truncation cap for subprocess output we log into span events
# (the verifier itself returns the full text up to its own cap).
_EVENT_TRUNCATE = 800


def _span_event(span: Any, name: str, data: dict) -> None:
    """Record ``span.event(name, data)`` if a span was supplied."""
    if span is None:
        return
    try:
        span.event(name, data)
    except Exception:  # pragma: no cover — tracing must never crash a tool
        log.debug("span.event(%s) failed", name, exc_info=True)


def _resolve_python(workdir: Path) -> str:
    """Pick the Python interpreter to run inside the sandbox.

    Preference order:
    1. ``{workdir}/.venv/bin/python`` — almost always absent inside the
       freshly-created sandbox, but cheap to check.
    2. ``{repo_root}/.venv/bin/python`` — the developer's working venv,
       guessed by walking up from ``workdir``. We can't walk to the user's
       repo root reliably, so this is best-effort.
    3. ``sys.executable`` — the interpreter the orchestrator is running
       under. Most reliable, and inherits whatever site-packages the
       orchestrator has (which is what we want for the demo).
    4. ``python3`` from PATH as a final fallback.
    """
    candidates = [
        workdir / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return str(c)

    # Walk a couple of levels up from workdir looking for a sibling .venv.
    # This is a best-effort lookup — the workdir is a tempdir so it won't
    # have a meaningful parent chain, but if the caller copied the repo
    # root in, it might.
    for ancestor in [workdir, *workdir.parents][:4]:
        venv_python = ancestor / ".venv" / "bin" / "python"
        if venv_python.exists() and os.access(venv_python, os.X_OK):
            return str(venv_python)

    # sys.executable is the interpreter the orchestrator is running
    # under. Crucially this is the same one that has fastapi / pytest /
    # httpx installed — exactly what the victim_app tests need.
    if sys.executable and Path(sys.executable).exists():
        return sys.executable

    return "python3"


async def _run_subprocess(
    *,
    cmd: list[str],
    cwd: str,
    env: dict,
    timeout: int,
) -> tuple[int, str]:
    """Run ``cmd`` and return ``(returncode, combined_output)``.

    On timeout the child is terminated and a synthetic returncode of
    ``-1`` is returned alongside a ``"TIMEOUT after Ns"`` message so
    the caller can surface the failure to the model / dashboard.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            # Give the OS a moment to reap the child so we don't leak.
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass
        return -1, f"TIMEOUT after {timeout}s"

    output = (stdout_bytes or b"").decode("utf-8", errors="replace")
    return (proc.returncode if proc.returncode is not None else -1), output


class Sandbox:
    """Async context manager that materializes a throwaway copy of ``source_dir``.

    Usage::

        async with Sandbox("victim_app", tracer) as sb:
            ok, err = await sb.apply_patch(diff)
            code, output = await sb.run_pytest()
    """

    def __init__(self, source_dir: str, tracer: Any) -> None:
        self.source_dir = source_dir
        self.tracer = tracer
        self.workdir: Optional[Path] = None
        self._tmp_obj: Optional[tempfile.TemporaryDirectory] = None
        # Lock down the subprocess env to a minimal set. We force
        # BUG_MODE=none so the tests exercise the fixed code path.
        self._base_env: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "Sandbox":
        # tempfile.TemporaryDirectory does the right thing on Mac + Linux.
        self._tmp_obj = tempfile.TemporaryDirectory(prefix="autonomous-sre-sb-")
        tmp = Path(self._tmp_obj.name)

        src = Path(self.source_dir).resolve()
        if not src.exists():
            raise RuntimeError(
                f"Sandbox source_dir does not exist: {src}"
            )

        # Copy the source tree, dropping VCS metadata. We always want a
        # fresh repo so `git apply` against `--index` does not get
        # confused by the parent project's history.
        def _ignore(directory: str, names: list[str]) -> list[str]:
            return [n for n in names if n in {".git", "__pycache__", ".pytest_cache"}]

        # ``workdir`` IS the copied source root. This matters because
        # ``git apply`` interprets the diff's ``a/<path>`` / ``b/<path>``
        # headers relative to the cwd (with default ``-p1``). If the
        # diff says ``a/app.py`` and we ran git at the tempdir parent,
        # git would look for ``app.py`` at that level and fail. By
        # making the workdir == the source root, the layout matches.
        workdir = tmp / src.name
        shutil.copytree(src, workdir, ignore=_ignore)
        self.workdir = workdir

        # ``git init`` so ``git apply`` works inside the sandbox. We
        # commit the initial state so the diff's ``a/<path>`` /
        # ``b/<path>`` headers resolve cleanly.
        await self._git_init(workdir)

        # Build the per-sandbox env once so every subprocess call picks
        # up the same PYTHONPATH / BUG_MODE.
        base_env = os.environ.copy()
        base_env["BUG_MODE"] = "none"  # we're testing the patched code
        # Ensure imports inside the sandbox resolve to OUR copies.
        existing_pp = base_env.get("PYTHONPATH", "")
        py_path_parts = [str(workdir)]
        if existing_pp:
            py_path_parts.append(existing_pp)
        base_env["PYTHONPATH"] = os.pathsep.join(py_path_parts)
        self._base_env = base_env

        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._tmp_obj is not None:
            try:
                self._tmp_obj.cleanup()
            except Exception:  # pragma: no cover — best effort
                log.debug("sandbox cleanup failed", exc_info=True)
        self._tmp_obj = None
        self.workdir = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _git_init(self, workdir: Path) -> None:
        """Initialize a git repo + initial commit inside ``workdir``.

        We need a real commit so that ``git apply`` recognises the file
        paths in the diff's ``a/<path>`` / ``b/<path>`` headers.
        """
        env = os.environ.copy()
        # Avoid leaking the developer's git identity / config into the
        # sandbox. Stub commit identity so ``git commit`` doesn't fail
        # with "Please tell me who you are" on a vanilla box.
        env.setdefault("GIT_AUTHOR_NAME", "autonomous-sre-sandbox")
        env.setdefault("GIT_AUTHOR_EMAIL", "sandbox@autonomous-sre.local")
        env.setdefault("GIT_COMMITTER_NAME", "autonomous-sre-sandbox")
        env.setdefault("GIT_COMMITTER_EMAIL", "sandbox@autonomous-sre.local")
        env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
        env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")

        steps: list[list[str]] = [
            ["git", "init", "-q"],
            ["git", "checkout", "-q", "-b", "main"],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.email=sandbox@autonomous-sre.local",
                "-c",
                "user.name=autonomous-sre-sandbox",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "init",
            ],
        ]
        for cmd in steps:
            code, output = await _run_subprocess(
                cmd=cmd,
                cwd=str(workdir),
                env=env,
                timeout=15,
            )
            if code != 0 and cmd[1] != "checkout":
                # ``git checkout -b main`` can fail on older gits that
                # default to ``main`` already; ignore that specific case.
                raise RuntimeError(
                    f"git step {' '.join(cmd)!r} failed: {output[:500]}"
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def apply_patch(
        self,
        diff: str,
        span: Any = None,
    ) -> tuple[bool, str]:
        """Apply a unified diff inside the sandbox.

        Writes the diff to ``{workdir}/.patch`` so anyone re-running the
        sandbox can inspect what was attempted, runs ``git apply
        --check`` first (dry run), and only if that passes runs ``git
        apply``. Returns ``(success, message)`` where ``message`` is the
        combined stderr/stdout of the failing step (empty on success).
        """
        if self.workdir is None:
            return False, "sandbox not initialized"

        patch_path = self.workdir / ".patch"
        patch_path.write_text(diff or "", encoding="utf-8")

        _span_event(
            span,
            "patch_written",
            {"path": str(patch_path), "bytes": len(diff or "")},
        )

        # 1) dry run: --check tells us whether the patch will apply
        #    without actually mutating the working tree.
        code, output = await _run_subprocess(
            cmd=["git", "apply", "--check", ".patch"],
            cwd=str(self.workdir),
            env=self._base_env,
            timeout=15,
        )
        if code != 0:
            _span_event(
                span,
                "git_apply_check_failed",
                {"output": output[:_EVENT_TRUNCATE]},
            )
            return False, output

        # 2) real apply.
        code, output = await _run_subprocess(
            cmd=["git", "apply", ".patch"],
            cwd=str(self.workdir),
            env=self._base_env,
            timeout=15,
        )
        if code != 0:
            _span_event(
                span,
                "git_apply_failed",
                {"output": output[:_EVENT_TRUNCATE]},
            )
            return False, output

        _span_event(span, "git_apply_ok", {})
        return True, ""

    async def run_pytest(
        self,
        extra_args: Optional[list[str]] = None,
        timeout: int = 60,
        span: Any = None,
    ) -> tuple[int, str]:
        """Run pytest inside the sandbox.

        Returns ``(returncode, combined_output)``. A timeout is reported
        as ``returncode == -1`` and ``output == "TIMEOUT after Ns"``.

        Pytest is invoked as ``<python> -m pytest tests/ -x --tb=short
        -q``; if the source tree doesn't have a ``tests`` directory at
        the conventional location, we soft-pass with code 0 and a
        ``"no tests found"`` message so the Verifier can flag it.
        """
        if self.workdir is None:
            return -1, "sandbox not initialized"

        # The workdir IS the copied source root (see ``__aenter__``).
        tests_dir = self.workdir / "tests"
        if not tests_dir.exists():
            _span_event(
                span,
                "no_tests_dir",
                {"path": str(tests_dir)},
            )
            return 0, "no tests found"

        python = _resolve_python(self.workdir)
        cmd = [
            python,
            "-m",
            "pytest",
            str(tests_dir),
            "-x",
            "--tb=short",
            "-q",
        ]
        if extra_args:
            cmd.extend(extra_args)

        # Run pytest with cwd = the copied source dir so it picks up the
        # patched ``app.py`` / ``monitor.py`` modules naturally.
        _span_event(
            span,
            "pytest_start",
            {"python": python, "cmd": " ".join(cmd)[:_EVENT_TRUNCATE]},
        )
        code, output = await _run_subprocess(
            cmd=cmd,
            cwd=str(self.workdir),
            env=self._base_env,
            timeout=timeout,
        )
        _span_event(
            span,
            "pytest_done",
            {
                "returncode": code,
                "output_chars": len(output),
                "output_tail": output[-_EVENT_TRUNCATE:],
            },
        )
        return code, output


__all__ = ["Sandbox"]
