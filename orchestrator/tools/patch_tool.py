"""In-memory unified-diff applier used by the Releaser.

The Releaser opens a real GitHub PR. To do that it has to materialize
the *new* file content from the unified diff the Fix Author produced —
GitHub's ``update_file`` endpoint wants the full new blob, not a diff.
The Verifier already proved the diff applies cleanly against the
sandbox copy of ``victim_app/`` via ``git apply``; here we mirror that
guarantee in pure Python so we don't need a writable filesystem.

We rely on the :mod:`unidiff` library to parse the diff, but the actual
hunk application is hand-rolled so we can produce precise error
messages when context lines don't line up (PyGithub will silently 422 on
a bad blob otherwise). The helper deliberately supports only the
single-file diff shape the Fix Author emits — multi-file patches raise.

Strict vs. fuzzy
----------------
:func:`apply_unified_diff` first tries an exact, line-by-line strict
match (:func:`_apply_strict`). When the live file on GitHub has drifted
slightly relative to what Fix Author + Verifier saw (a stray whitespace
edit, a blank line landing first, etc.), strict raises and we fall back
to :func:`_apply_fuzzy`: each hunk searches a small ``±fuzz`` window
around its declared anchor for the position where the largest fraction
of its context lines match (whitespace-tolerant via ``rstrip``). If at
least 70 % of the context matches, the hunk's additions/removals are
spliced in at the best-match position and subsequent hunks' anchors are
adjusted by the running ``offset`` delta. Below the threshold we raise
with the same actionable shape strict would have used.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

try:  # ``unidiff`` is a hard requirement (see requirements.txt) but be defensive.
    from unidiff import PatchSet  # type: ignore[import-not-found]

    _UNIDIFF_IMPORTABLE = True
except Exception:  # pragma: no cover — defensive import
    PatchSet = None  # type: ignore[assignment]
    _UNIDIFF_IMPORTABLE = False


__all__ = ["PatchApplyError", "apply_unified_diff"]


# Fraction of a hunk's context lines that must match at the best
# candidate position before we accept the fuzzy splice. 0.7 = 70 %.
_FUZZY_THRESHOLD = 0.7


class PatchApplyError(Exception):
    """Raised when a unified diff cannot be applied to its source.

    The message is constructed to be actionable in a span event /
    AgentRun error_message: it always names the hunk index and either
    the line number that failed or the structural problem encountered.
    """


def _split_keep_endings(text: str) -> List[str]:
    """Split ``text`` into lines, preserving the original line endings.

    This matters for diff application: a file that ended without a
    trailing newline must round-trip without one. We don't use
    ``str.splitlines(keepends=True)`` directly because we need explicit
    control over the final ``"\\n"``-or-not state when rejoining.
    """
    if text == "":
        return []
    lines = text.splitlines(keepends=True)
    return lines


def _join_keep_endings(lines: List[str]) -> str:
    """Inverse of :func:`_split_keep_endings`."""
    return "".join(lines)


def _normalize_diff_input(diff_string: str) -> str:
    """Validate + lightly normalize the diff before handing it to ``unidiff``.

    ``unidiff`` is picky about the trailing newline on the very last
    line of a diff — many models emit one but a couple don't. Add one
    if missing to keep the parser happy.
    """
    if not isinstance(diff_string, str) or not diff_string.strip():
        raise PatchApplyError("diff is empty")
    return diff_string if diff_string.endswith("\n") else diff_string + "\n"


def _parse_single_file_patch(diff_text: str):
    """Parse a diff and return the single :class:`PatchedFile` it must contain.

    Raises :class:`PatchApplyError` if parsing fails or the patch
    contains zero / more than one file (the Fix Author always emits
    exactly one file, and a multi-file patch would silently apply only
    half of itself under PyGithub's ``update_file``).
    """
    if not _UNIDIFF_IMPORTABLE:  # pragma: no cover — exercised by import smoke
        raise PatchApplyError(
            "unidiff library is not importable — install `unidiff`"
        )

    try:
        patch = PatchSet(diff_text)
    except Exception as exc:  # noqa: BLE001 — we re-wrap as PatchApplyError
        raise PatchApplyError(f"diff failed to parse: {exc}") from exc

    if len(patch) == 0:
        raise PatchApplyError("diff parsed but contained zero files")
    if len(patch) > 1:
        names = [
            (getattr(p, "target_file", None) or getattr(p, "source_file", None) or "?")
            for p in patch
        ]
        raise PatchApplyError(
            f"diff contains {len(patch)} files; apply_unified_diff supports exactly one: {names}"
        )

    return patch[0]


def apply_unified_diff(
    original_content: str,
    diff_string: str,
    *,
    fuzz: int = 3,
) -> str:
    """Apply a single-file unified diff to ``original_content``.

    Strict matching is attempted first; on a context mismatch we retry
    with fuzzy matching that searches ``±fuzz`` lines around each hunk's
    declared anchor and accepts the splice when at least 70 % of the
    context lines match (whitespace-tolerant).

    Returns the new file content. Raises :class:`PatchApplyError` only
    if both strict and fuzzy reject the diff — the resulting error is
    the *fuzzy* failure (more informative) chained off the strict one.
    """
    # First, run validation once so a malformed diff fails fast with the
    # same error in either strategy.
    diff_text = _normalize_diff_input(diff_string)
    _parse_single_file_patch(diff_text)

    try:
        return _apply_strict(original_content, diff_string)
    except PatchApplyError as strict_err:
        # Fall through to fuzzy — strict_err is preserved as the chain.
        try:
            return _apply_fuzzy(original_content, diff_string, fuzz=fuzz)
        except PatchApplyError as fuzzy_err:
            raise fuzzy_err from strict_err


def _apply_strict(original_content: str, diff_string: str) -> str:
    """Apply a single-file unified diff to ``original_content`` strictly.

    This is the original line-by-line applier — it expects each context
    line in the diff to match the source exactly (modulo a small ±3
    line drift forgiveness, mirroring ``patch`` / ``git apply``'s
    default behaviour). When the source has drifted further than that,
    raise so :func:`apply_unified_diff` can fall back to fuzzy.
    """
    diff_text = _normalize_diff_input(diff_string)
    patched_file = _parse_single_file_patch(diff_text)

    src_lines = _split_keep_endings(original_content)
    # We rebuild the output in a single pass over the source, splicing
    # hunks in at their declared positions. Hunks are guaranteed to
    # arrive sorted by source line in any well-formed unified diff.
    out: List[str] = []
    src_idx = 0  # 0-based cursor into src_lines

    for hunk_no, hunk in enumerate(patched_file, start=1):
        # ``hunk.source_start`` is 1-based; "0" appears for pure
        # additions to an empty file, in which case we treat the
        # anchor as the very beginning.
        anchor_1 = int(hunk.source_start or 1)
        anchor_0 = max(anchor_1 - 1, 0)

        # Copy any source lines that sit BEFORE this hunk verbatim.
        if anchor_0 < src_idx:
            raise PatchApplyError(
                f"hunk #{hunk_no}: source_start={anchor_1} precedes cursor at "
                f"{src_idx + 1}; hunks must be sorted and non-overlapping"
            )
        out.extend(src_lines[src_idx:anchor_0])
        src_idx = anchor_0

        # Walk the hunk line-by-line. ``unidiff`` exposes each diff
        # line as an object with ``is_context`` / ``is_added`` /
        # ``is_removed`` booleans and a ``.value`` string with the
        # original line ending preserved (or absent for the very last
        # line of a no-newline-at-eof file).
        for diff_line in hunk:
            value = diff_line.value
            if diff_line.is_context:
                actual = src_lines[src_idx] if src_idx < len(src_lines) else None
                if actual is None or _strip_eol(actual) != _strip_eol(value):
                    # Try a small forgiving window: some diffs drift by
                    # 1 line when an upstream commit lands first. Look
                    # ahead up to 3 lines and re-anchor if we find an
                    # exact match.
                    drift = _try_drift_match(src_lines, src_idx, value, window=3)
                    if drift is None:
                        raise PatchApplyError(
                            f"hunk #{hunk_no}: context mismatch at source line "
                            f"{src_idx + 1}; expected {value!r}, got "
                            f"{(actual or '')!r}"
                        )
                    # Copy through the drifted lines as-is and continue.
                    out.extend(src_lines[src_idx : src_idx + drift])
                    src_idx += drift
                out.append(src_lines[src_idx])
                src_idx += 1
            elif diff_line.is_removed:
                actual = src_lines[src_idx] if src_idx < len(src_lines) else None
                if actual is None or _strip_eol(actual) != _strip_eol(value):
                    drift = _try_drift_match(src_lines, src_idx, value, window=3)
                    if drift is None:
                        raise PatchApplyError(
                            f"hunk #{hunk_no}: removal mismatch at source line "
                            f"{src_idx + 1}; expected to remove {value!r}, got "
                            f"{(actual or '')!r}"
                        )
                    out.extend(src_lines[src_idx : src_idx + drift])
                    src_idx += drift
                # Skip the removed line in the source.
                src_idx += 1
            elif diff_line.is_added:
                out.append(_with_eol_like(value, src_lines, src_idx))
            else:  # pragma: no cover — unknown hunk-line kind
                raise PatchApplyError(
                    f"hunk #{hunk_no}: unknown diff line kind: {diff_line!r}"
                )

    # Append any trailing source lines past the final hunk.
    if src_idx < len(src_lines):
        out.extend(src_lines[src_idx:])

    return _join_keep_endings(out)


def _apply_fuzzy(
    original_content: str,
    diff_string: str,
    *,
    fuzz: int = 3,
) -> str:
    """Apply a single-file unified diff with fuzzy context matching.

    For each hunk:

    1. Compute the declared 0-indexed anchor adjusted for the running
       ``offset`` (the cumulative net lines added/removed by earlier
       hunks in the same diff).
    2. Scan candidate positions in ``[anchor - fuzz, anchor + fuzz]`` and
       pick the one where the most hunk context lines match the source
       (ties broken by proximity to the declared anchor).
    3. If the best candidate's match count is below ``70 %`` of the
       hunk's context lines, raise :class:`PatchApplyError`.
    4. Otherwise splice the hunk's adds/removes at that position. The
       splice preserves the source file's whitespace (context lines are
       copied verbatim from the file, not the diff).

    Multi-file diffs are rejected by :func:`_parse_single_file_patch`.
    """
    diff_text = _normalize_diff_input(diff_string)
    patched_file = _parse_single_file_patch(diff_text)

    lines = _split_keep_endings(original_content)
    offset = 0  # cumulative delta from earlier hunks in this diff

    for hunk_no, hunk in enumerate(patched_file, start=1):
        # Declared 0-indexed anchor adjusted for prior hunks' splices.
        declared_0 = max(int(hunk.source_start or 1) - 1, 0) + offset

        n_context = sum(1 for line in hunk if _is_context(line))
        # If a hunk is pure-addition with zero context (e.g. inserting
        # into an empty file), nothing to score — accept the anchor as
        # provided. Otherwise enforce the threshold.
        threshold = max(1, int(round(n_context * _FUZZY_THRESHOLD))) if n_context else 0

        window_start = max(0, declared_0 - fuzz)
        window_end = min(len(lines), declared_0 + fuzz + 1)

        best_score = -1
        best_position = declared_0
        # Iterate the window. ``range(start, end)`` is empty for files
        # shorter than the window — fall back to scoring at the source
        # end so the failure mode is a clean threshold rejection rather
        # than a silent no-op.
        candidates = list(range(window_start, max(window_end, window_start + 1)))
        for candidate in candidates:
            score = _score_hunk_position(lines, candidate, hunk)
            if score > best_score:
                best_score = score
                best_position = candidate

        if n_context and best_score < threshold:
            # Include both ``fuzzy match failed`` (the new error vocab)
            # and ``context mismatch`` (kept for backwards compat with
            # callers that already grep for the old phrase) so a single
            # message satisfies both consumers.
            raise PatchApplyError(
                f"hunk #{hunk_no}: fuzzy match failed — context mismatch. "
                f"declared line {hunk.source_start}, best score "
                f"{best_score}/{n_context} context lines matched. "
                f"Need {threshold} ({int(_FUZZY_THRESHOLD * 100)}%) to apply."
            )

        new_lines, delta = _apply_hunk_at(lines, best_position, hunk)
        lines = new_lines
        offset += delta

    return _join_keep_endings(lines)


def _is_context(diff_line) -> bool:
    """Return True if ``diff_line`` is an unchanged context line."""
    return bool(getattr(diff_line, "is_context", False))


def _diff_line_text(diff_line) -> str:
    """Return the raw text of a unidiff line (its ``.value`` field).

    Centralized so we can swap the source if the upstream API ever
    changes. ``.value`` already excludes the leading ``+`` / ``-`` /
    ``" "`` marker — exactly the file-level text we want to compare.
    """
    return str(getattr(diff_line, "value", "") or "")


def _score_hunk_position(lines: List[str], position: int, hunk) -> int:
    """Count how many of the hunk's context lines match ``lines`` starting at ``position``.

    Removed lines also advance the cursor (they should exist in the
    original) but only context-line matches count toward the score, so
    the threshold check stays anchored to the same denominator the
    caller used. Added lines don't advance the cursor — they're net new
    content. Whitespace is compared via ``rstrip()`` so trailing spaces
    and EOL variants don't sink an otherwise valid match.
    """
    score = 0
    cursor = position
    for diff_line in hunk:
        if cursor >= len(lines):
            break
        if _is_context(diff_line):
            expected = _diff_line_text(diff_line)
            actual = lines[cursor]
            if expected.rstrip() == actual.rstrip():
                score += 1
            cursor += 1
        elif getattr(diff_line, "is_removed", False):
            # Removed lines should also exist in the original; advance
            # the cursor whether or not they match so the next context
            # line lines up at the correct offset.
            cursor += 1
        # Added lines: don't advance — they're new content.
    return score


def _apply_hunk_at(
    lines: List[str],
    position: int,
    hunk,
) -> Tuple[List[str], int]:
    """Splice the hunk's adds/removes into ``lines`` starting at ``position``.

    Returns ``(new_lines, delta)`` where ``delta`` is the net number of
    lines added (positive) or removed (negative). Context lines are
    copied verbatim from the source so the file's actual whitespace is
    preserved across the splice.
    """
    result: List[str] = list(lines[:position])
    cursor = position
    delta = 0

    for diff_line in hunk:
        value = _diff_line_text(diff_line)
        if _is_context(diff_line):
            if cursor < len(lines):
                result.append(lines[cursor])
                cursor += 1
            else:
                # Source ran out before the hunk's context did — copy
                # the diff's text so we don't silently truncate. This
                # corresponds to a "edge" case where the LLM produced
                # context for lines that have since been removed.
                result.append(_with_eol_like(value, lines, cursor))
        elif getattr(diff_line, "is_removed", False):
            # Skip the line in the source. Even if the actual source
            # line doesn't match exactly (fuzzy mode), we honor the
            # diff's intent because the surrounding context scored
            # above threshold.
            if cursor < len(lines):
                cursor += 1
            delta -= 1
        elif getattr(diff_line, "is_added", False):
            result.append(_with_eol_like(value, lines, cursor))
            delta += 1

    # Append the rest of the source verbatim.
    result.extend(lines[cursor:])
    return result, delta


def _strip_eol(s: str) -> str:
    """Strip a trailing ``\\n`` / ``\\r\\n`` so context comparisons ignore EOL drift."""
    if s.endswith("\r\n"):
        return s[:-2]
    if s.endswith("\n"):
        return s[:-1]
    return s


def _with_eol_like(value: str, src_lines: List[str], idx: int) -> str:
    """Ensure ``value`` ends with a newline if the surrounding source uses them.

    Most diffs emit each added line with a trailing ``\\n``. If the
    parser hands us a line without one but the file otherwise uses
    newlines, we add one so the output isn't accidentally concatenated
    with the next source line.
    """
    if value.endswith("\n") or value.endswith("\r\n"):
        return value
    # If the source has any newline-terminated lines, default to "\n".
    has_newline = any(line.endswith("\n") for line in src_lines)
    if has_newline:
        return value + "\n"
    return value


def _try_drift_match(
    src_lines: List[str],
    start_idx: int,
    target_value: str,
    *,
    window: int,
) -> Optional[int]:
    """Look ahead ``window`` lines from ``start_idx`` for an exact match of ``target_value``.

    Returns the number of lines to advance past (so the caller can copy
    them through verbatim) such that ``src_lines[start_idx + offset]``
    equals ``target_value``. ``None`` if no match found.
    """
    target = _strip_eol(target_value)
    for offset in range(1, window + 1):
        idx = start_idx + offset
        if idx >= len(src_lines):
            return None
        if _strip_eol(src_lines[idx]) == target:
            return offset
    return None
