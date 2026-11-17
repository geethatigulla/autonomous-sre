"""FixAuthor agent: drafts a minimal unified diff that addresses the diagnosed root cause.

The FixAuthor is the third agent in the pipeline, invoked by the state
machine after the Investigator finishes (``incident.status ==
"investigated"``). Unlike the Investigator it does NOT use tool calling —
this is a single, focused LLM round-trip that takes the suspect file,
the Investigator's hypothesis, and the original incident, and emits a
JSON envelope containing a unified diff.

The output is *not* applied to anything here — that's the Verifier's
job. We persist the patch on ``incident.proposed_patch`` and hand off.

Hard rules
----------
* Single LLM call, JSON-mode response.
* The diff must contain ``---``, ``+++``, and ``@@`` markers (the
  minimal sanity check for a unified diff). If it doesn't, we raise so
  the state machine marks the agent run as ``error``.
* No tool use here. We read the suspect file once via the injected
  :class:`~orchestrator.tools.github_tool.GitHubTool` (the Investigator's
  read may be stale) and feed it to the model as part of the user prompt.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from orchestrator.agents.base import BaseAgent
from orchestrator.timeutil import utcnow
from orchestrator.tools.sandbox_tool import Sandbox


if TYPE_CHECKING:  # pragma: no cover
    from sqlmodel import Session

    from orchestrator.models import Incident
    from orchestrator.tools.github_tool import GitHubTool
    from orchestrator.tracing import SpanContext


log = logging.getLogger("orchestrator.agents.fix_author")


# Cap how much of the suspect file we feed to the model. 8000 chars is
# enough for our toy victim_app/app.py with room to spare; if the real
# file gets larger we'll truncate from the top (less interesting) to
# preserve the bottom (where the crash usually lives).
_FILE_PROMPT_CAP = 8000


_VALID_RISK = {"low", "medium", "high"}
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
# Verifier sandbox copies ``victim_app/`` as the git root — paths in diffs
# must be ``app.py``, not ``victim_app/app.py``.
_SANDBOX_SOURCE_DIR = "victim_app"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX_SOURCE_PATH = _REPO_ROOT / _SANDBOX_SOURCE_DIR


def _strip_fences(raw: str) -> str:
    r"""Strip markdown ``\`\`\`json ... \`\`\``` fences from a raw string.

    The system prompt forbids fences, but in practice models still emit
    them roughly 5 % of the time. Strip them cheaply rather than retry.
    """
    if not raw:
        return ""
    candidate = raw.strip()
    m = _JSON_FENCE_RE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    return candidate


def _sandbox_rel_path(path: str) -> str:
    """Map repo / LLM paths to paths relative to the verifier sandbox root."""
    if not path:
        return "app.py"
    normalized = path.replace("\\", "/").lstrip("./")
    prefix = f"{_SANDBOX_SOURCE_DIR}/"
    if normalized.startswith(prefix):
        normalized = normalized[len(prefix) :]
    if normalized.startswith("/"):
        normalized = normalized.lstrip("/")
    return normalized or "app.py"


def _normalize_diff_paths(diff: str) -> str:
    """Rewrite ``---`` / ``+++`` headers so ``git apply`` finds files in the sandbox."""
    if not diff:
        return diff
    out: list[str] = []
    for line in diff.splitlines():
        for marker in ("--- a/", "+++ b/"):
            if line.startswith(marker):
                rest = line[len(marker) :]
                line = marker + _sandbox_rel_path(rest)
                break
        out.append(line)
    return "\n".join(out) + ("\n" if diff.endswith("\n") else "")


def _build_null_deref_patch(
    file_content: str,
    *,
    rel_path: str,
    insert_before_line: Optional[int],
) -> str:
    """Deterministic null guard inserted immediately before ``url.strip()``."""
    lines = file_content.splitlines()
    insert_at: Optional[int] = None
    if insert_before_line and 1 <= insert_before_line <= len(lines):
        insert_at = insert_before_line - 1
    else:
        for i, line in enumerate(lines):
            if "url.strip()" in line.replace(" ", ""):
                insert_at = i
                break
    if insert_at is None:
        raise RuntimeError(
            "null_deref fallback: could not locate url.strip() in suspect file"
        )

    indent_match = re.match(r"^(\s*)", lines[insert_at])
    indent = indent_match.group(1) if indent_match else "        "
    guard = [
        f"{indent}# Guard: reject null url before .strip() (fixes null_deref)",
        f"{indent}if url is None:",
        f'{indent}    raise HTTPException(status_code=422, detail="url is required")',
    ]
    new_lines = lines[:insert_at] + guard + lines[insert_at:]
    old_text = "\n".join(lines) + ("\n" if file_content.endswith("\n") else "")
    new_text = "\n".join(new_lines) + ("\n" if file_content.endswith("\n") else "")
    rel = _sandbox_rel_path(rel_path)
    parts = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        lineterm="",
    )
    return "\n".join(parts) + "\n"


def _diff_looks_valid(diff: str) -> bool:
    """Cheap structural check for a unified diff.

    We don't try to parse hunks here — that's what ``git apply --check``
    is for inside the Verifier. We only enforce the three structural
    markers that distinguish a unified diff from prose / JSON / a raw
    code block.
    """
    if not diff or not isinstance(diff, str):
        return False
    return ("---" in diff) and ("+++" in diff) and ("@@" in diff)


class FixAuthorAgent(BaseAgent):
    """Generates a unified diff for the Verifier to apply.

    Parametrized by a ``strategy`` (one of :attr:`STRATEGY_PROMPTS`) so two
    instances can be raced in parallel under the parallel-fix orchestrator.
    The strategy bends the model toward a different "fix philosophy" —
    defensive (fail-fast / explicit validation) vs. permissive (graceful
    degradation / sensible defaults) — but every other part of the
    pipeline stays identical.
    """

    # Fixes need solid code reasoning. gpt-4o handles unified-diff
    # formatting more reliably than gpt-4o-mini in our experiments.
    model = "gpt-4o"

    # ---- Strategy prompts ------------------------------------------------
    # Each strategy is concatenated onto the shared ``_BASE_RULES`` and
    # the shared ``_OUTPUT_SCHEMA`` to produce the final system prompt.
    # Keeping the three parts separate makes it easy to add new
    # philosophies (e.g. "telemetry-first") later without rewriting the
    # output contract.
    # Classification-specific hints layered onto the strategy prompt so
    # the model knows what KIND of bug it's fixing. Architecture stays
    # bug-type-agnostic — only the textual hint changes per incident.
    CLASSIFICATION_HINTS: dict[str, str] = {
        "null_deref": (
            "The fix should add a guard against None/missing values."
        ),
        "recent_regression": (
            "The fix should restore a contract that was broken in a "
            "recent commit. Prefer reverting the offending change over "
            "wholesale rewrites."
        ),
        "config_error": (
            "The fix is likely in a config file (.env, yaml) or a code "
            "reference to an env var. Use the correct key name OR add a "
            "sensible default via os.environ.get(key, default). Do NOT "
            "change behavior of code that reads correct config."
        ),
    }

    STRATEGY_PROMPTS: dict[str, str] = {
        "defensive": (
            "You are a Senior Engineer with a DEFENSIVE PROGRAMMING philosophy.\n"
            "Your fixes prioritize: explicit validation, fail-fast, clear error messages.\n"
            "For null/missing inputs: raise an HTTPException(422) with a descriptive\n"
            "message BEFORE any operation that could fail.\n"
            "Add type hints if the function lacks them.\n"
            "Add a comment '# Guard:' explaining the check."
        ),
        "permissive": (
            "You are a Senior Engineer with a PERMISSIVE / FALLBACK philosophy.\n"
            "Your fixes prioritize: graceful degradation, sensible defaults,\n"
            "user-friendliness.\n"
            "For null/missing inputs: substitute a sensible default (empty string,\n"
            "None-safe op, or early return with a default response) rather than\n"
            "raising.\n"
            "Add a comment '# Fallback:' explaining the default."
        ),
    }

    _BASE_RULES = (
        "You are fixing production bugs. You receive:\n"
        "- The original incident (exception, traceback, request body)\n"
        "- The Investigator's root cause analysis and proposed fix_strategy\n"
        "- The current contents of the suspect file\n\n"
        "Your job: produce a UNIFIED DIFF that fixes the bug.\n\n"
        "Hard rules:\n"
        "1. Minimal change. Preserve behavior for valid inputs. Don't refactor.\n"
        "2. Match the existing code style (indentation, quotes, naming).\n"
        "3. Do NOT add new imports unless strictly necessary — reuse what's imported.\n"
        "4. Insert the new lines BEFORE the failing call, never after it.\n"
        "5. File paths in diff headers are relative to the victim_app package root\n"
        "   (use `app.py`, NOT `victim_app/app.py`).\n"
        "6. Do not duplicate existing lines in the hunk — only add/remove what changes.\n"
    )

    _OUTPUT_SCHEMA = (
        "Output format — respond with ONLY this JSON, no markdown fences:\n"
        "{\n"
        '  "file_path": "app.py",\n'
        '  "diff": "<unified diff with --- a/app.py +++ b/app.py headers and @@ hunks>",\n'
        '  "explanation": "1-2 sentences on what the fix does and why it works",\n'
        '  "test_should_pass": "name of the pytest test that should now pass",\n'
        '  "risk_level": "low" | "medium" | "high",\n'
        '  "confidence": 0.0 to 1.0\n'
        "}\n\n"
        "The diff MUST be valid unified diff format compatible with `patch -p1`\n"
        "or `git apply`. Triple-check line numbers and context lines."
    )

    # Kept as a backwards-compatible class attribute so any caller that
    # imports ``FixAuthorAgent.SYSTEM_PROMPT`` keeps working. The
    # strategy-specific prompt actually used at runtime is composed in
    # ``__init__`` and lives on the instance as ``self.SYSTEM_PROMPT``.
    SYSTEM_PROMPT = (
        _BASE_RULES
        + "\n"
        + STRATEGY_PROMPTS["defensive"]
        + "\n\n"
        + _OUTPUT_SCHEMA
    )

    def __init__(
        self,
        openai_client: Any,
        tracer: Any,
        github_tool: "GitHubTool",
        *,
        sandbox_source_dir: str = _SANDBOX_SOURCE_DIR,
        strategy: str = "defensive",
    ) -> None:
        super().__init__(openai_client, tracer)
        self.github = github_tool
        self.sandbox_source_dir = sandbox_source_dir
        if strategy not in self.STRATEGY_PROMPTS:
            raise ValueError(
                f"Unknown fix strategy: {strategy!r}; "
                f"expected one of {sorted(self.STRATEGY_PROMPTS)}"
            )
        self.strategy = strategy
        # Compose the strategy-specific system prompt on the instance so
        # parallel races between strategies don't fight over a shared
        # class attribute. ``_compose_system_prompt`` is also called
        # per-run with the incident's classification so the prompt picks
        # up the right CLASSIFICATION HINT block; the value set here is
        # the no-classification default kept for back-compat.
        self.SYSTEM_PROMPT = self._compose_system_prompt(None)

    def _compose_system_prompt(self, classification: Optional[str]) -> str:
        """Stack base rules + strategy + classification hint + schema.

        Layered so each piece stays independently editable: base rules
        are universal, strategy prompts capture the fix philosophy,
        classification hints specialize for THIS bug type, and the
        output schema enforces the JSON envelope shape.
        """
        hint = self.CLASSIFICATION_HINTS.get(classification or "", "")
        hint_block = (
            f"\nCLASSIFICATION HINT: {hint}\n" if hint else ""
        )
        return (
            self._BASE_RULES
            + "\n"
            + self.STRATEGY_PROMPTS[self.strategy]
            + hint_block
            + "\n"
            + self._OUTPUT_SCHEMA
        )

    @property
    def name(self) -> str:  # type: ignore[override]
        """Strategy-qualified agent name (e.g. ``fix_author_defensive``).

        Used in trace span names and AgentRun rows so dashboards can tell
        the two race participants apart at a glance.
        """
        return f"fix_author_{self.strategy}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_file_with_line_numbers(self, content: str) -> str:
        """Prefix each line with ``"{n:4d}: "`` so the model can target it.

        The diff schema requires accurate line numbers in the ``@@``
        hunks, and the cheapest way to give a chat-completion model a
        reliable line index is to bake it into the prompt.
        """
        if not content:
            return ""
        # Truncate from the top if the file is huge — the crash is
        # almost always near the bottom of the file in our toy app, and
        # the start of the file is usually imports.
        if len(content) > _FILE_PROMPT_CAP:
            content = content[-_FILE_PROMPT_CAP:]
        lines = content.splitlines()
        return "\n".join(f"{i + 1:4d}: {line}" for i, line in enumerate(lines))

    def _load_investigation(self, incident: "Incident") -> dict:
        raw = incident.investigation_result or ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _build_user_prompt(
        self,
        incident: "Incident",
        file_content: str,
        investigation: dict,
        top_hypothesis: dict,
    ) -> str:
        numbered = self._format_file_with_line_numbers(file_content)
        traceback_excerpt = (incident.traceback or "")[-3000:]
        request_body_excerpt = (incident.request_body or "")[:1500]

        hypotheses_summary = json.dumps(
            investigation.get("hypotheses") or [],
            indent=2,
            default=str,
        )[:3000]

        return (
            "INCIDENT REPORT\n\n"
            f"Service: {incident.source_service}\n"
            f"Endpoint: {incident.endpoint}\n"
            f"Exception: {incident.exception_class}: {incident.exception_message}\n\n"
            f"Request body:\n{request_body_excerpt}\n\n"
            f"Traceback:\n{traceback_excerpt}\n\n"
            "INVESTIGATOR ROOT CAUSE\n"
            f"{(incident.root_cause or '(none recorded)')}\n\n"
            "ALL HYPOTHESES (JSON):\n"
            f"{hypotheses_summary}\n\n"
            "TOP HYPOTHESIS (highest-confidence):\n"
            f"  description: {top_hypothesis.get('description', '')}\n"
            f"  evidence:    {top_hypothesis.get('evidence', '')}\n"
            f"  fix_strategy:{top_hypothesis.get('fix_strategy', '')}\n"
            f"  confidence:  {top_hypothesis.get('confidence', 0.0)}\n\n"
            f"SUSPECT FILE: {incident.suspect_file or '(unknown)'}\n"
            f"SUSPECT LINE: {incident.suspect_line if incident.suspect_line is not None else '(unknown)'}\n\n"
            "CURRENT FILE CONTENTS (with line numbers — use them in your diff hunks):\n"
            f"{numbered}\n\n"
            "Produce a unified diff that fixes this bug. Respond with ONLY the JSON "
            "object described in the system prompt. Do not include markdown fences."
        )

    async def _patch_applies_in_sandbox(
        self,
        diff: str,
        span: "SpanContext",
        *,
        file_content: Optional[str] = None,
        rel_path: str = "app.py",
    ) -> tuple[bool, str]:
        """Dry-run the diff in the same layout the Verifier uses."""
        source_dir: Optional[str] = None
        tmp_holder: Optional[tempfile.TemporaryDirectory[str]] = None
        rel = _sandbox_rel_path(rel_path)

        if file_content is not None:
            tmp_holder = tempfile.TemporaryDirectory(prefix="autonomous-sre-fa-")
            root = Path(tmp_holder.name) / self.sandbox_source_dir
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file_content, encoding="utf-8")
            source_dir = str(root)
        elif _SANDBOX_SOURCE_PATH.is_dir():
            source_dir = self.sandbox_source_dir
        else:
            return True, ""

        try:
            async with Sandbox(source_dir, self.tracer) as sb:
                ok, err = await sb.apply_patch(diff, span=span)
                return ok, err
        except Exception as exc:  # noqa: BLE001 — validation must not crash drafting
            log.warning("fix_author: sandbox validation failed: %s", exc)
            return False, str(exc)
        finally:
            if tmp_holder is not None:
                tmp_holder.cleanup()

    def _parse_llm_fix_payload(self, raw_content: str, incident: "Incident") -> dict:
        cleaned = _strip_fences(raw_content)
        try:
            parsed = json.loads(cleaned) if cleaned else {}
        except Exception as exc:
            raise RuntimeError(f"Fix Author returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"Fix Author JSON was not an object (got {type(parsed).__name__})"
            )

        file_path = parsed.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            file_path = incident.suspect_file or "app.py"
        file_path = _sandbox_rel_path(file_path)

        diff = parsed.get("diff") or ""
        if not isinstance(diff, str):
            diff = ""
        diff = _normalize_diff_paths(diff)

        explanation = parsed.get("explanation") or ""
        if not isinstance(explanation, str):
            explanation = ""
        test_should_pass = parsed.get("test_should_pass") or ""
        if not isinstance(test_should_pass, str):
            test_should_pass = ""

        risk_level = parsed.get("risk_level")
        if risk_level not in _VALID_RISK:
            risk_level = "medium"

        try:
            confidence = float(parsed.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        if not _diff_looks_valid(diff):
            raise RuntimeError(
                "Fix Author produced a diff without the expected unified-diff markers"
            )

        return {
            "file_path": file_path,
            "diff": diff,
            "explanation": explanation,
            "test_should_pass": test_should_pass,
            "risk_level": risk_level,
            "confidence": confidence,
        }

    async def _ensure_applicable_patch(
        self,
        *,
        incident: "Incident",
        file_content: str,
        suspect_rel: str,
        parsed: dict,
        user_prompt: str,
        span: "SpanContext",
    ) -> dict:
        """Normalize paths, validate with git apply, retry once, else deterministic fallback."""
        diff = parsed["diff"]
        applies, apply_err = await self._patch_applies_in_sandbox(
            diff, span, file_content=file_content, rel_path=suspect_rel
        )
        if applies:
            return parsed

        span.event(
            "patch_apply_check_failed",
            {"error": (apply_err or "")[:500], "diff_preview": diff[:300]},
        )

        retry_user = (
            f"{user_prompt}\n\n"
            "PREVIOUS PATCH WAS REJECTED by git apply. Error:\n"
            f"{(apply_err or 'unknown')[:1500]}\n\n"
            "Regenerate the JSON fix. Use paths relative to victim_app (e.g. app.py). "
            "Insert the None guard immediately BEFORE url.strip(), not after it. "
            "Do not duplicate context lines."
        )
        raw_retry = await self._call_llm(
            system=self.SYSTEM_PROMPT,
            user=retry_user,
            span=span,
            response_format={"type": "json_object"},
        )
        retry_parsed = self._parse_llm_fix_payload(raw_retry, incident)
        retry_parsed["file_path"] = suspect_rel
        applies, apply_err = await self._patch_applies_in_sandbox(
            retry_parsed["diff"],
            span,
            file_content=file_content,
            rel_path=suspect_rel,
        )
        if applies:
            span.event("patch_retry_succeeded", {})
            return retry_parsed

        if incident.classification == "null_deref":
            span.event("patch_using_null_deref_fallback", {})
            fallback_diff = _build_null_deref_patch(
                file_content,
                rel_path=suspect_rel,
                insert_before_line=incident.suspect_line,
            )
            applies, apply_err = await self._patch_applies_in_sandbox(
                fallback_diff,
                span,
                file_content=file_content,
                rel_path=suspect_rel,
            )
            if applies:
                parsed = dict(retry_parsed)
                parsed["diff"] = fallback_diff
                parsed["file_path"] = suspect_rel
                if not parsed.get("explanation"):
                    parsed["explanation"] = (
                        "Insert HTTP 422 guard before url.strip() when url is None."
                    )
                parsed["risk_level"] = "low"
                parsed["confidence"] = max(
                    float(parsed.get("confidence", 0.0) or 0.0), 0.85
                )
                return parsed

        raise RuntimeError(
            "Fix Author patch failed git apply in sandbox: "
            f"{(apply_err or 'unknown')[:500]}"
        )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run_returning(
        self,
        *,
        incident: "Incident",
        span: "SpanContext",
        session: "Session",
    ) -> dict:
        """Produce a fix without persisting ``proposed_patch`` to the incident.

        Used by the parallel-fix race so two candidates can be drafted
        concurrently without stomping on the canonical incident row —
        the racer picks the winner and writes the winning patch back.

        Side effect (still applied): if the GitHub tool resolves the
        suspect file to a different repo-relative path than the one the
        Investigator stored, we update ``incident.suspect_file`` so every
        downstream agent (and the dashboard) sees the same string. That
        update is idempotent — both racers converge to the same value.
        """
        # Re-compose the system prompt so it picks up THIS incident's
        # classification hint. The default value set in __init__ doesn't
        # know which bug type we'll be drafting for; recomposing here
        # keeps every race participant focused on the right kind of fix.
        self.SYSTEM_PROMPT = self._compose_system_prompt(
            incident.classification
        )

        span.event(
            "drafting_patch",
            {
                "incident_id": incident.id,
                "strategy": self.strategy,
                "classification": incident.classification,
                "suspect_file": incident.suspect_file,
                "suspect_line": incident.suspect_line,
            },
        )

        # ---- 1. Re-read the suspect file fresh ------------------------
        suspect = incident.suspect_file
        if not suspect:
            # Investigator couldn't pin down a path. Try to recover the
            # deepest in-repo frame from the traceback so we at least
            # have a candidate to feed the GitHub resolver.
            tb = incident.traceback or ""
            matches = re.findall(r'File "([^"]+\.py)"', tb)
            our_files = [
                m for m in matches
                if "site-packages" not in m
                and ".venv" not in m
                and not m.startswith("/usr/")
            ]
            if our_files:
                suspect = our_files[-1].rsplit("/", 1)[-1]
                span.event(
                    "suspect_file_recovered_from_traceback",
                    {"file": suspect},
                )
            else:
                raise RuntimeError(
                    "Fix Author: no suspect_file in incident and could "
                    "not extract one from the traceback"
                )

        file_content = await self.github.read_file(suspect, span=span)
        resolved_path = getattr(self.github, "last_resolved_path", None) or suspect
        if not file_content:
            raise RuntimeError(
                f"Fix Author cannot read {suspect} from repo "
                f"(resolved attempt: {resolved_path})"
            )

        # Persist the resolved path back onto the incident so the
        # Verifier / Releaser and the dashboard all see the same string.
        # Both race participants make the same update (idempotent).
        if resolved_path and resolved_path != incident.suspect_file:
            old = incident.suspect_file
            incident.suspect_file = resolved_path
            session.add(incident)
            session.commit()
            session.refresh(incident)
            span.event(
                "suspect_file_updated_to_resolved",
                {"old": old, "new": resolved_path},
            )
        suspect = resolved_path
        suspect_rel = _sandbox_rel_path(suspect)

        # ---- 2. Load Investigator's result ----------------------------
        investigation = self._load_investigation(incident)
        hypotheses = investigation.get("hypotheses") or []
        if hypotheses:
            top_hypothesis = max(
                hypotheses,
                key=lambda h: float(h.get("confidence", 0.0) or 0.0)
                if isinstance(h, dict)
                else 0.0,
            )
            if not isinstance(top_hypothesis, dict):
                top_hypothesis = {}
        else:
            top_hypothesis = {}

        span.event(
            "context_loaded",
            {
                "strategy": self.strategy,
                "file_chars": len(file_content),
                "hypotheses": len(hypotheses),
                "top_confidence": float(
                    top_hypothesis.get("confidence", 0.0) or 0.0
                ),
            },
        )

        # ---- 3. Build the user prompt ---------------------------------
        user_prompt = self._build_user_prompt(
            incident=incident,
            file_content=file_content,
            investigation=investigation,
            top_hypothesis=top_hypothesis,
        )

        # ---- 4. One LLM round-trip, JSON mode -------------------------
        raw_content = await self._call_llm(
            system=self.SYSTEM_PROMPT,
            user=user_prompt,
            span=span,
            response_format={"type": "json_object"},
        )

        # ---- 5. Parse, normalize paths, validate in sandbox ---------
        try:
            parsed = self._parse_llm_fix_payload(raw_content, incident)
        except RuntimeError as exc:
            span.event(
                "patch_json_parse_failed",
                {
                    "strategy": self.strategy,
                    "error": str(exc)[:300],
                    "raw_chars": len(raw_content),
                },
            )
            raise

        parsed["file_path"] = suspect_rel
        result = await self._ensure_applicable_patch(
            incident=incident,
            file_content=file_content,
            suspect_rel=suspect_rel,
            parsed=parsed,
            user_prompt=user_prompt,
            span=span,
        )
        file_path = result["file_path"]
        diff = result["diff"]
        risk_level = result["risk_level"]
        confidence = result["confidence"]

        span.event(
            "patch_drafted",
            {
                "strategy": self.strategy,
                "file": file_path,
                "diff_chars": len(diff),
                "risk": risk_level,
                "confidence": confidence,
            },
        )

        return result

    async def run(
        self,
        *,
        incident: "Incident",
        span: "SpanContext",
        session: "Session",
    ) -> dict:
        """Backwards-compatible entry point: draft + persist to the incident.

        Thin wrapper around :meth:`run_returning` that writes the winning
        patch fields onto the incident row. The state machine no longer
        calls this directly (it uses the parallel-fix race), but unit
        tests that drive a single Fix Author still rely on it.
        """
        result = await self.run_returning(
            incident=incident, span=span, session=session
        )

        incident.proposed_patch = result["diff"]
        incident.fix_explanation = result["explanation"]
        incident.fix_risk_level = result["risk_level"]
        incident.fix_confidence = result["confidence"]
        incident.updated_at = utcnow()
        # Status transition is owned by the state machine — we leave
        # ``incident.status`` alone so the SM stays the single writer.
        session.add(incident)
        session.commit()
        session.refresh(incident)

        return result


__all__ = ["FixAuthorAgent"]
