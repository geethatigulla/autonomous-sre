"""Investigator agent: deep-reasoning root-cause analysis with tool use.

The Investigator is the second agent in the pipeline, invoked by the
state machine when the Triager classifies an incident as ``null_deref``
(other classifications continue to use the stub pipeline until later
prompts wire them in).

Unlike the Triager, the Investigator uses OpenAI's tool/function-calling
API: the model decides when to read a file, list commits, or web-search,
and the agent dispatches those tool calls against
:class:`~orchestrator.tools.github_tool.GitHubTool` and
:class:`~orchestrator.tools.search_tool.WebSearchTool`. Each tool call
is opened under its own child span so the trace tree shows exactly what
the model investigated.

Hard rules
----------
* Tool calls capped at ``settings.investigator_max_tool_calls`` (default
  5). When the cap is hit, a system message tells the model the budget
  is exhausted and we loop one more time for a final JSON answer.
* The final assistant message must contain only a JSON object matching
  the schema in :data:`InvestigatorAgent.SYSTEM_PROMPT`. If the model
  emits prose or a malformed payload we coerce a degenerate "fallback"
  hypothesis with low confidence so the downstream Fix Author still has
  something to work with.
* When tools are disabled (no GitHub token / no Tavily key) the agent
  still produces a hypothesis from the traceback alone.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TYPE_CHECKING

from orchestrator.agents.base import BaseAgent
from orchestrator.config import get_settings
from orchestrator.timeutil import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from sqlmodel import Session

    from orchestrator.models import Incident
    from orchestrator.tools.github_tool import GitHubTool
    from orchestrator.tools.search_tool import WebSearchTool
    from orchestrator.tracing import SpanContext


log = logging.getLogger("orchestrator.agents.investigator")


_TOOL_INPUT_TRUNCATE = 500
_TOOL_OUTPUT_TRUNCATE = 500
# Per-tool result cap fed back to the LLM as the tool message content.
# Big enough to be useful, small enough that a couple of tool calls
# don't blow out the chat completion context window.
_TOOL_PAYLOAD_TO_LLM = 4000


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


class InvestigatorAgent(BaseAgent):
    """Root-cause analyzer with iterative GitHub + web tool use."""

    name = "investigator"
    # Heavy model — root-cause reasoning benefits from the larger
    # context and stronger tool-use behaviour vs. gpt-4o-mini.
    model = "gpt-4o"

    # Classification-specific hints appended to the base system prompt
    # so the model knows what KIND of bug to look for. The architecture
    # stays bug-type-agnostic — only the prompt is specialized.
    CLASSIFICATION_HINTS: dict[str, str] = {
        "null_deref": (
            "This is classified as a null/None dereference. Focus on: "
            "which variable became None, which input shape triggered it, "
            "what guard is missing."
        ),
        "recent_regression": (
            "This is classified as a recent regression. CRITICAL: use "
            "list_recent_commits on the suspect file to find what "
            "changed. Look for renamed keys, changed return shapes, "
            "removed fields."
        ),
        "config_error": (
            "This is classified as a config/environment error. Focus on: "
            "which env var or config key is being read, what's the "
            "expected vs actual name, where in the codebase it's "
            "referenced. Look for KeyError on os.environ or config dict."
        ),
    }

    BASE_SYSTEM_PROMPT = """You are a Senior SRE Investigator. You receive a classified production incident and must determine the ROOT CAUSE by examining source code and public information.

Available tools:
- github_read_file(path, ref="main"): fetch a file from the repo
- github_read_file_at_lines(path, start, end, ref="main"): fetch specific line range
- github_list_recent_commits(path=null, limit=10): see recent commits, optionally filtered by file
- web_search(query): search the web for error messages, library docs, known issues

Approach:
1. Read the traceback. Identify the deepest frame IN OUR REPO (skip frames in stdlib, site-packages). That's the suspect file:line.
2. Read the suspect file. Understand what the code is doing at the crash point.
3. Form 1-2 hypotheses. Be specific: which variable, which assumption, what input shape.
4. If you suspect a recent regression, list_recent_commits on that file.
5. If the exception class is unusual, web_search for it.
6. STOP investigating once you have enough to write a fix. Don't over-research.

Hard cap: 5 tool calls TOTAL. Quality over thoroughness.

When you have enough info, respond with a final message containing ONLY this JSON (no other text, no markdown fences):
{
  "suspect_file": "path/in/repo.py",
  "suspect_line": 47,
  "suspect_function": "shorten_url",
  "root_cause": "1-3 sentences explaining WHY the bug exists",
  "hypotheses": [
    {
      "description": "What's wrong",
      "evidence": "What in the code/trace/search supports this",
      "fix_strategy": "Concrete approach: add null check, change key name, set env var, etc.",
      "confidence": 0.0
    }
  ],
  "investigation_log": ["step 1: read traceback identified app.py:47", "step 2: ...", ...]
}

IMPORTANT: When you set suspect_file, use the path EXACTLY as it appears in the repo — the same string you successfully passed to github_read_file or saw in github_list_recent_commits. Do not prepend or invent directory prefixes. If your first read_file call returned an empty/error result, try the basename only (e.g. "app.py", not "src/app.py" or "victim_app/app.py")."""

    # Kept as a backwards-compatible class attribute. Callers that
    # import :data:`InvestigatorAgent.SYSTEM_PROMPT` keep working; the
    # runtime prompt is composed per-incident via
    # :meth:`_build_system_prompt`.
    SYSTEM_PROMPT = BASE_SYSTEM_PROMPT

    TOOL_SCHEMAS: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "github_read_file",
                "description": (
                    "Fetch the full contents of a file from the project's "
                    "GitHub repo. Returns up to 8000 characters."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Repo-relative path, e.g. 'victim_app/app.py'.",
                        },
                        "ref": {
                            "type": "string",
                            "description": "Branch, tag, or commit SHA. Defaults to 'main'.",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "github_read_file_at_lines",
                "description": (
                    "Fetch a specific 1-indexed inclusive line range from a "
                    "file in the project's GitHub repo. Prefer this when you "
                    "already know the crash line."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start": {"type": "integer", "minimum": 1},
                        "end": {"type": "integer", "minimum": 1},
                        "ref": {"type": "string"},
                    },
                    "required": ["path", "start", "end"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "github_list_recent_commits",
                "description": (
                    "List the most recent commits in the repo, optionally "
                    "filtered to a single file path. Use this to spot recent "
                    "regressions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Optional file path filter.",
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "Search the web for the given query. Useful for unusual "
                    "exception classes, library bugs, and known issues."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    ]

    def __init__(
        self,
        openai_client: Any,
        tracer: Any,
        github_tool: "GitHubTool",
        search_tool: "WebSearchTool",
    ) -> None:
        super().__init__(openai_client, tracer)
        self.github = github_tool
        self.search = search_tool

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def _dispatch_tool(
        self,
        name: str,
        arguments: dict,
        span: "SpanContext",
    ) -> str:
        """Run a tool call and return the JSON-encoded result string.

        Every tool call opens ``tool.<name>`` as a child of the
        investigator span so the trace tree shows the full investigation
        narrative. Tool inputs and outputs are recorded as span events
        (truncated to 500 chars) for replay / debugging.
        """
        with span.child_span(
            f"tool.{name}", metadata={"tool": name}
        ) as tool_span:
            tool_span.event(
                "tool_input",
                {
                    "name": name,
                    "arguments": _truncate(
                        json.dumps(arguments, default=str),
                        _TOOL_INPUT_TRUNCATE,
                    ),
                },
            )

            try:
                if name == "github_read_file":
                    result = await self.github.read_file(
                        path=str(arguments.get("path", "")),
                        ref=str(arguments.get("ref") or "main"),
                        span=tool_span,
                    )
                elif name == "github_read_file_at_lines":
                    result = await self.github.read_file_at_lines(
                        path=str(arguments.get("path", "")),
                        start=int(arguments.get("start", 1)),
                        end=int(arguments.get("end", 1)),
                        ref=str(arguments.get("ref") or "main"),
                        span=tool_span,
                    )
                elif name == "github_list_recent_commits":
                    raw_path = arguments.get("path")
                    result = await self.github.list_recent_commits(
                        path=str(raw_path) if raw_path else None,
                        limit=int(arguments.get("limit", 10)),
                        span=tool_span,
                    )
                elif name == "web_search":
                    result = await self.search.search(
                        query=str(arguments.get("query", "")),
                        max_results=int(arguments.get("max_results", 5)),
                        span=tool_span,
                    )
                else:
                    tool_span.event(
                        "unknown_tool",
                        {"name": name},
                    )
                    result = {"error": f"unknown tool: {name}"}
            except Exception as e:  # noqa: BLE001 — tools must not crash the agent
                tool_span.event(
                    "tool_exception",
                    {"error": str(e)[:300], "type": type(e).__name__},
                )
                result = {"error": f"{type(e).__name__}: {e}"}

            # Serialize for the LLM. Strings are kept raw (we already
            # truncated them inside the tool); everything else goes
            # through json.dumps.
            if isinstance(result, str):
                payload_for_llm = result
            else:
                try:
                    payload_for_llm = json.dumps(result, default=str)
                except Exception:
                    payload_for_llm = str(result)
            payload_for_llm = _truncate(payload_for_llm, _TOOL_PAYLOAD_TO_LLM)

            # Tool-result span event (truncated separately from the
            # payload we actually feed back to the model).
            tool_span.event(
                "tool_output",
                {
                    "name": name,
                    "result_preview": _truncate(
                        payload_for_llm, _TOOL_OUTPUT_TRUNCATE
                    ),
                    "result_chars": len(payload_for_llm),
                },
            )
            return payload_for_llm

    # ------------------------------------------------------------------
    # Final-JSON parsing / validation
    # ------------------------------------------------------------------

    _JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

    def _parse_final_json(self, raw: str) -> dict:
        """Best-effort extraction of the JSON object from a final message."""
        if not raw:
            return {}
        candidate = raw.strip()
        # Strip markdown fences if the model emitted them despite the
        # instruction.
        m = self._JSON_FENCE.search(candidate)
        if m:
            candidate = m.group(1).strip()
        # If there's leading prose, try to find the first {...} object.
        if not candidate.startswith("{"):
            start = candidate.find("{")
            if start >= 0:
                candidate = candidate[start:]
        try:
            return json.loads(candidate)
        except Exception:
            # Truncate to last closing brace and retry — sometimes the
            # model trails off with whitespace or a stray fence.
            last = candidate.rfind("}")
            if last > 0:
                try:
                    return json.loads(candidate[: last + 1])
                except Exception:
                    return {}
            return {}

    def _validate_result(self, result: dict, incident: "Incident") -> dict:
        """Normalize the parsed JSON; substitute defaults for missing pieces.

        Returns a dict that is always safe to persist: ``suspect_file``
        is a string (possibly empty), ``hypotheses`` is a list with at
        least one entry, ``investigation_log`` is a list of strings,
        ``root_cause`` is a string.
        """
        validated: dict = {}

        sf = result.get("suspect_file")
        validated["suspect_file"] = sf if isinstance(sf, str) else ""

        sl = result.get("suspect_line")
        try:
            validated["suspect_line"] = int(sl) if sl is not None else None
        except (TypeError, ValueError):
            validated["suspect_line"] = None

        validated["suspect_function"] = (
            result.get("suspect_function") if isinstance(result.get("suspect_function"), str) else ""
        )
        validated["root_cause"] = (
            result.get("root_cause") if isinstance(result.get("root_cause"), str) else ""
        )

        raw_hypotheses = result.get("hypotheses")
        clean_hypotheses: list[dict] = []
        if isinstance(raw_hypotheses, list):
            for h in raw_hypotheses:
                if not isinstance(h, dict):
                    continue
                desc = h.get("description") if isinstance(h.get("description"), str) else ""
                ev = h.get("evidence") if isinstance(h.get("evidence"), str) else ""
                strat = h.get("fix_strategy") if isinstance(h.get("fix_strategy"), str) else ""
                try:
                    conf = float(h.get("confidence", 0.0))
                except (TypeError, ValueError):
                    conf = 0.0
                conf = max(0.0, min(1.0, conf))
                clean_hypotheses.append(
                    {
                        "description": desc,
                        "evidence": ev,
                        "fix_strategy": strat,
                        "confidence": conf,
                    }
                )

        if not clean_hypotheses:
            # Fallback so downstream Fix Author always has something.
            clean_hypotheses = [
                {
                    "description": (
                        f"{incident.exception_class} during {incident.endpoint} — "
                        "specific root cause unverified."
                    ),
                    "evidence": (incident.exception_message or "")[:300],
                    "fix_strategy": (
                        "Add a defensive guard at the crash site and re-run the "
                        "failing request to confirm."
                    ),
                    "confidence": 0.2,
                }
            ]
        validated["hypotheses"] = clean_hypotheses

        raw_log = result.get("investigation_log")
        log_entries: list[str] = []
        if isinstance(raw_log, list):
            for item in raw_log:
                if isinstance(item, str):
                    log_entries.append(item)
                else:
                    log_entries.append(str(item))
        validated["investigation_log"] = log_entries

        return validated

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _build_system_prompt(self, classification: str | None) -> str:
        """Compose the system prompt with a classification-specific hint.

        Architecture stays bug-type-agnostic; only the textual hint
        changes. When ``classification`` is unknown / unmapped we return
        the base prompt unchanged so behaviour is identical to the
        pre-hint version.
        """
        base = self.BASE_SYSTEM_PROMPT
        hint = self.CLASSIFICATION_HINTS.get(classification or "")
        if hint:
            return f"{base}\n\nCLASSIFICATION HINT: {hint}"
        return base

    def _build_user_prompt(self, incident: "Incident") -> str:
        traceback_excerpt = (incident.traceback or "")[-4000:]
        request_body_excerpt = (incident.request_body or "")[:2000]
        return (
            "INCIDENT REPORT\n\n"
            f"Service: {incident.source_service}\n"
            f"Endpoint: {incident.endpoint}\n"
            f"Exception: {incident.exception_class}: {incident.exception_message}\n\n"
            f"Triager classification: {incident.classification or 'unknown'} "
            f"(severity {incident.severity or '?'}, "
            f"confidence {incident.triager_confidence or 0.0:.2f})\n"
            f"Triager reasoning: {incident.triager_reasoning or '(none)'}\n\n"
            f"Request body:\n{request_body_excerpt}\n\n"
            f"Traceback:\n{traceback_excerpt}\n\n"
            "Investigate the root cause. Use tools sparingly — at most 5 calls."
        )

    async def run(
        self,
        *,
        incident: "Incident",
        span: "SpanContext",
        session: "Session",
    ) -> dict:
        settings = get_settings()
        max_tool_calls = int(settings.investigator_max_tool_calls or 5)

        span.event(
            "investigating",
            {
                "incident_id": incident.id,
                "exception_class": incident.exception_class,
                "github_enabled": not getattr(self.github, "disabled", True),
                "search_enabled": getattr(self.search, "enabled", False),
                "max_tool_calls": max_tool_calls,
            },
        )

        # Tell the LLM up front when tools are unavailable — the system
        # prompt promises ``github_read_file`` etc. exist, so we have to
        # be explicit if they don't.
        availability_notes: list[str] = []
        if getattr(self.github, "disabled", True):
            availability_notes.append(
                "NOTE: github_* tools are disabled in this environment "
                "(no token or repo configured). Reason from the traceback only."
            )
        if not getattr(self.search, "enabled", False):
            availability_notes.append(
                "NOTE: web_search is disabled in this environment. "
                "Do not call it."
            )

        # Compose the system prompt with a classification-specific hint
        # so the model has a focused angle for THIS incident class.
        system_prompt = self._build_system_prompt(incident.classification)
        if availability_notes:
            system_prompt = (
                system_prompt + "\n\n" + "\n".join(availability_notes)
            )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_user_prompt(incident)},
        ]

        tool_calls_made = 0
        final_message: Any = None

        # Loop: at most ``max_tool_calls + 2`` LLM round-trips. The +2
        # covers (a) the initial planning turn before any tool, and (b)
        # the forced final turn after the budget warning.
        max_iterations = max_tool_calls + 2
        for iteration in range(max_iterations):
            span.event(
                "llm_iteration",
                {
                    "iteration": iteration,
                    "tool_calls_made": tool_calls_made,
                },
            )

            try:
                message = await self._call_llm_with_tools(
                    messages=messages,
                    tools=self.TOOL_SCHEMAS,
                    span=span,
                )
            except Exception as e:  # noqa: BLE001
                span.event(
                    "llm_error",
                    {"error": str(e)[:300], "type": type(e).__name__},
                )
                final_message = None
                break

            tool_calls = getattr(message, "tool_calls", None) or []
            content = getattr(message, "content", None) or ""

            # Append the assistant turn to the running message list. We
            # need ``tool_calls`` if present so subsequent tool messages
            # reference the right ID.
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                # Final answer (or the model gave up).
                final_message = message
                break

            # Run each tool call.
            for tc in tool_calls:
                name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                try:
                    arguments = json.loads(raw_args)
                    if not isinstance(arguments, dict):
                        arguments = {}
                except Exception:
                    arguments = {}

                result_text = await self._dispatch_tool(name, arguments, span)
                tool_calls_made += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    }
                )

                if tool_calls_made >= max_tool_calls:
                    span.event(
                        "tool_budget_exhausted",
                        {"tool_calls_made": tool_calls_made},
                    )
                    break

            if tool_calls_made >= max_tool_calls:
                # Force the model to produce final JSON next turn.
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "TOOL CALL BUDGET EXHAUSTED — produce final JSON now. "
                            "Do not call any more tools. Respond with the JSON "
                            "object described in the system prompt and nothing else."
                        ),
                    }
                )
                try:
                    message = await self._call_llm_with_tools(
                        messages=messages,
                        tools=self.TOOL_SCHEMAS,
                        span=span,
                        tool_choice="none",
                    )
                except Exception as e:  # noqa: BLE001
                    span.event(
                        "llm_error",
                        {"error": str(e)[:300], "type": type(e).__name__},
                    )
                    final_message = None
                    break
                content = getattr(message, "content", None) or ""
                messages.append({"role": "assistant", "content": content})
                final_message = message
                break

        # ----- Parse + validate the final JSON ------------------------
        raw_content = ""
        if final_message is not None:
            raw_content = getattr(final_message, "content", None) or ""

        parsed = self._parse_final_json(raw_content)
        if not parsed:
            span.event(
                "final_json_parse_failed",
                {"raw_chars": len(raw_content)},
            )
        validated = self._validate_result(parsed, incident)

        # Resolve suspect_file against the actual repo. The LLM
        # frequently guesses the wrong prefix (e.g. "victim_app/app.py"
        # when the file lives at "app.py"); if we persist that, the Fix
        # Author's read fails and the pipeline lands at fix_failed.
        llm_suspect = validated.get("suspect_file") or ""
        if llm_suspect and not getattr(self.github, "disabled", True):
            try:
                resolver = getattr(self.github, "_resolve_path", None)
                resolved = (
                    await resolver(llm_suspect, ref="main", span=span)
                    if resolver
                    else None
                )
            except Exception as e:  # noqa: BLE001 — resolution failure is best-effort
                span.event(
                    "suspect_file_resolution_error",
                    {"error": str(e)[:300], "type": type(e).__name__},
                )
                resolved = None

            if resolved:
                if resolved != llm_suspect:
                    span.event(
                        "suspect_file_resolved",
                        {"llm_said": llm_suspect, "actual": resolved},
                    )
                validated["suspect_file"] = resolved
            else:
                # Try one more time with just the basename.
                basename = llm_suspect.rsplit("/", 1)[-1]
                if basename and basename != llm_suspect:
                    try:
                        resolver = getattr(self.github, "_resolve_path", None)
                        resolved = (
                            await resolver(basename, ref="main", span=span)
                            if resolver
                            else None
                        )
                    except Exception:  # noqa: BLE001
                        resolved = None
                if resolved:
                    span.event(
                        "suspect_file_resolved_via_basename",
                        {"llm_said": llm_suspect, "actual": resolved},
                    )
                    validated["suspect_file"] = resolved
                else:
                    span.event(
                        "suspect_file_unresolvable",
                        {"llm_said": llm_suspect},
                    )
                    validated["suspect_file"] = ""
                    # Couldn't verify the file at all — knock confidence
                    # down on every hypothesis so the Fix Author / state
                    # machine can react.
                    for h in validated.get("hypotheses") or []:
                        try:
                            h["confidence"] = min(
                                float(h.get("confidence", 0.0) or 0.0),
                                0.3,
                            )
                        except (TypeError, ValueError):
                            h["confidence"] = 0.3

        # Promote a top-level "confidence" for convenience: the max
        # across hypotheses.
        if validated["hypotheses"]:
            validated["confidence"] = max(
                h.get("confidence", 0.0) for h in validated["hypotheses"]
            )
        else:
            validated["confidence"] = 0.0

        span.event(
            "investigation_complete",
            {
                "suspect_file": validated.get("suspect_file", ""),
                "suspect_line": validated.get("suspect_line"),
                "hypotheses": len(validated.get("hypotheses") or []),
                "tool_calls_made": tool_calls_made,
                "confidence": validated.get("confidence"),
            },
        )

        # ----- Persist to the incident --------------------------------
        try:
            incident.investigation_result = json.dumps(validated)
        except Exception:
            incident.investigation_result = json.dumps(
                {"error": "could not serialize result"}
            )
        incident.suspect_file = validated.get("suspect_file") or None
        incident.suspect_line = validated.get("suspect_line")
        incident.root_cause = validated.get("root_cause") or None
        incident.status = "investigated"
        incident.updated_at = utcnow()
        session.add(incident)
        session.commit()
        session.refresh(incident)

        return validated


__all__ = ["InvestigatorAgent"]
