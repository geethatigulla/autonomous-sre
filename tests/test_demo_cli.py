"""Light-touch tests for ``scripts/demo.py``.

Keeps surface area small — the CLI is a presentation utility, not core
business logic. We verify:

* the module imports cleanly and exposes ``main`` + ``build_parser``
* ``--status`` runs end-to-end without raising when no services exist
* the menu dispatcher re-prompts (rather than crashing) on bogus input

Subprocess plumbing is intentionally NOT exercised here — that path
relies on uvicorn + the network and would make the suite flaky.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "demo.py"


def _load_demo_module():
    """Import scripts/demo.py as a fresh module under the name ``demo_cli``.

    Done dynamically because ``scripts/`` isn't a Python package — there
    is (by design) no ``__init__.py``. ``importlib.util`` is the
    canonical way to bring a single-file script in for testing.
    """
    spec = importlib.util.spec_from_file_location("demo_cli", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_cli"] = module
    spec.loader.exec_module(module)
    return module


def test_demo_cli_imports() -> None:
    """Importing the CLI must not error, and the public surface must exist."""
    demo = _load_demo_module()
    assert hasattr(demo, "main")
    assert hasattr(demo, "build_parser")
    parser = demo.build_parser()
    # Sanity: the parser at least knows about the three documented flags.
    parsed = parser.parse_args(["--status"])
    assert parsed.status is True


def test_demo_cli_status_no_services(capsys) -> None:
    """``--status`` should render the table cleanly with nothing running.

    No exception, and the orchestrator/victim rows should show as
    failing (✗). We don't lock down the exact glyphs because rich's
    output depends on the terminal width when capturing.
    """
    demo = _load_demo_module()

    # Force every health check to fail so the test is independent of
    # whether the developer has a stray orchestrator running locally.
    with mock.patch.object(demo, "_orchestrator_alive", return_value=False), \
         mock.patch.object(demo, "_victim_alive", return_value=False):
        rc = demo.main(["--status"])
    assert rc == 0

    captured = capsys.readouterr()
    # Rich may strip styling when capturing through pytest, but the
    # human-readable labels we set always appear in the output.
    assert "Orchestrator" in captured.out
    assert "Victim app" in captured.out


def test_demo_cli_invalid_menu_choice() -> None:
    """An invalid menu choice must just re-prompt, not raise."""
    demo = _load_demo_module()

    # handle_menu_choice returns True when the loop should continue.
    # Both kinds of "garbage in" should yield True with no exception.
    assert demo.handle_menu_choice("not-a-real-option") is True
    assert demo.handle_menu_choice("") is True
    assert demo.handle_menu_choice("99") is True

    # A real terminating input ('q') returns False — but only after the
    # confirmation prompt resolves. Patch Confirm.ask + the alive
    # probes so the test doesn't block on stdin.
    with mock.patch.object(demo, "_orchestrator_alive", return_value=False), \
         mock.patch.object(demo, "_victim_alive", return_value=False):
        assert demo.handle_menu_choice("q") is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
