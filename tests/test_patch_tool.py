"""Tests for the in-memory unified-diff applier used by the Releaser.

The Releaser materializes the new file content from the Fix Author's
unified diff in pure Python so it can commit a full blob to GitHub via
``repo.update_file``. These tests pin down the three behaviours the
Releaser relies on:

1. A well-formed insertion diff applies cleanly.
2. A diff whose context lines don't match the source raises
   ``PatchApplyError`` with a helpful message.
3. The exact unified diff our smoke test sees from the Fix Author
   applies cleanly against the real ``victim_app/app.py``, producing a
   file that contains the inserted null guard.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from orchestrator.tools.patch_tool import (  # noqa: E402
    PatchApplyError,
    apply_unified_diff,
)


# ---------------------------------------------------------------------------
# 1. Simple insertion
# ---------------------------------------------------------------------------


def test_apply_diff_simple_insertion():
    original = textwrap.dedent(
        """\
        line1
        line2
        line3
        line4
        """
    )
    diff = textwrap.dedent(
        """\
        --- a/foo.py
        +++ b/foo.py
        @@ -1,4 +1,5 @@
         line1
         line2
        +inserted
         line3
         line4
        """
    )
    out = apply_unified_diff(original, diff)
    assert out == "line1\nline2\ninserted\nline3\nline4\n"


def test_apply_diff_multiple_added_lines():
    original = textwrap.dedent(
        """\
        def foo(x):
            y = x.strip()
            return y
        """
    )
    diff = textwrap.dedent(
        """\
        --- a/foo.py
        +++ b/foo.py
        @@ -1,3 +1,5 @@
         def foo(x):
        +    if x is None:
        +        raise ValueError("x required")
             y = x.strip()
             return y
        """
    )
    out = apply_unified_diff(original, diff)
    assert "if x is None" in out
    assert 'raise ValueError("x required")' in out
    # Original two lines should still be present.
    assert "y = x.strip()" in out
    assert "return y" in out


# ---------------------------------------------------------------------------
# 2. Context mismatch raises PatchApplyError
# ---------------------------------------------------------------------------


def test_apply_diff_with_context_mismatch():
    original = textwrap.dedent(
        """\
        line1
        line2
        line3
        """
    )
    bad_diff = textwrap.dedent(
        """\
        --- a/foo.py
        +++ b/foo.py
        @@ -1,3 +1,4 @@
         wrong_context_a
         wrong_context_b
        +inserted
         wrong_context_c
        """
    )
    with pytest.raises(PatchApplyError) as exc_info:
        apply_unified_diff(original, bad_diff)
    msg = str(exc_info.value)
    assert "hunk #1" in msg
    # The message should tell us what we expected vs. got.
    assert "context mismatch" in msg or "expected" in msg


def test_apply_diff_empty_diff_raises():
    with pytest.raises(PatchApplyError):
        apply_unified_diff("anything\n", "")


def test_apply_diff_multi_file_raises():
    diff = textwrap.dedent(
        """\
        --- a/foo.py
        +++ b/foo.py
        @@ -1,1 +1,2 @@
         only line
        +added
        --- a/bar.py
        +++ b/bar.py
        @@ -1,1 +1,2 @@
         only line
        +added
        """
    )
    with pytest.raises(PatchApplyError):
        apply_unified_diff("only line\n", diff)


# ---------------------------------------------------------------------------
# 3. The actual smoke-test diff applies to the real victim_app/app.py
# ---------------------------------------------------------------------------


# Hard-coded buggy seed file (a representative snapshot of what the
# null_deref smoke test sees). Pinning it here keeps the test
# deterministic even if victim_app/app.py evolves over time.
_VICTIM_APP_SNAPSHOT = textwrap.dedent(
    '''
    """Tiny seed app for sandbox tests."""

    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
    from typing import Optional

    app = FastAPI()


    class ShortenRequest(BaseModel):
        url: Optional[str] = None


    @app.post("/shorten")
    async def shorten(payload: ShortenRequest) -> dict:
        url = payload.url
        # BUG: no null guard before .strip()
        normalized = url.strip().lower()
        return {"short": normalized[:6], "original": normalized}


    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "healthy"}
    '''
).lstrip()


_SMOKE_TEST_DIFF = textwrap.dedent(
    """\
    --- a/app.py
    +++ b/app.py
    @@ -14,6 +14,8 @@ class ShortenRequest(BaseModel):
     @app.post("/shorten")
     async def shorten(payload: ShortenRequest) -> dict:
         url = payload.url
    +    if url is None:
    +        raise HTTPException(status_code=422, detail="url is required")
         # BUG: no null guard before .strip()
         normalized = url.strip().lower()
         return {"short": normalized[:6], "original": normalized}
    """
)


def test_apply_diff_the_actual_smoke_test_diff():
    out = apply_unified_diff(_VICTIM_APP_SNAPSHOT, _SMOKE_TEST_DIFF)
    # The new content must contain the inserted guard...
    assert "if url is None" in out
    assert 'raise HTTPException(status_code=422, detail="url is required")' in out
    # ...without removing the original lines.
    assert "normalized = url.strip().lower()" in out
    assert 'return {"short": normalized[:6], "original": normalized}' in out
    # And the file should still end with the healthz handler.
    assert "/healthz" in out
    # Sanity: the diff added exactly two lines.
    assert len(out.splitlines()) == len(_VICTIM_APP_SNAPSHOT.splitlines()) + 2


# ---------------------------------------------------------------------------
# 4. Fuzzy fallback — drift, whitespace, total mismatch
# ---------------------------------------------------------------------------


def test_apply_diff_fuzzy_matches_off_by_two_lines():
    """A diff whose declared line is +2 off the actual content position.

    The strict path's small drift window catches ±3 lines for exact
    matches, but here we go further: the diff anchor is wrong AND the
    surrounding content is line-shifted, which is what real GitHub
    drift looks like. Fuzzy mode must search the ±3 window and re-anchor.
    """
    # 100 lines: line_0 … line_99 with newline terminators.
    original = "\n".join(f"line_{i}" for i in range(100)) + "\n"
    # Hunk claims source_start=74 (so lines 74..76 by 1-indexing →
    # line_73, line_74, line_75 in our naming). The actual context
    # ``line_75 / line_76 / line_77`` lives two lines later — at
    # 1-indexed source lines 76..78. Strict would have given up after
    # the drift_window=3 exact-match search; fuzzy must locate the
    # ``line_75 → line_76 → line_77`` triple via context scoring.
    diff = textwrap.dedent(
        """\
        --- a/x.py
        +++ b/x.py
        @@ -74,3 +74,4 @@
         line_75
         line_76
        +ADDED
         line_77
        """
    )
    result = apply_unified_diff(original, diff)
    assert "ADDED\n" in result, result
    assert result.count("ADDED") == 1
    # The original lines must still be present and in order.
    idx_75 = result.index("line_75\n")
    idx_added = result.index("ADDED\n")
    idx_77 = result.index("line_77\n")
    assert idx_75 < idx_added < idx_77


def test_apply_diff_fuzzy_tolerates_whitespace_drift():
    """Original has trailing spaces; diff has none. Fuzzy ``rstrip()`` matches."""
    # Trailing spaces after ``def foo():`` — common diff-drift culprit.
    original = "def foo():   \n    return None\n"
    diff = textwrap.dedent(
        """\
        --- a/x.py
        +++ b/x.py
        @@ -1,2 +1,3 @@
         def foo():
        +    # added comment
             return None
        """
    )
    result = apply_unified_diff(original, diff)
    assert "# added comment" in result, result
    # Strict's exact match would have rejected the trailing-space line;
    # fuzzy's rstrip-aware comparison should accept it.
    assert "def foo():   \n" in result or "def foo():\n" in result


def test_apply_diff_fails_when_context_completely_different():
    """Fuzzy must give up when no candidate scores above the 70% threshold."""
    original = "completely\nunrelated\ncontent\nhere\n"
    diff = textwrap.dedent(
        """\
        --- a/x.py
        +++ b/x.py
        @@ -10,3 +10,4 @@
         def foo():
         def bar():
        +    pass
         def baz():
        """
    )
    with pytest.raises(PatchApplyError) as exc_info:
        apply_unified_diff(original, diff)
    msg = str(exc_info.value).lower()
    assert "fuzzy match failed" in msg or "context" in msg
