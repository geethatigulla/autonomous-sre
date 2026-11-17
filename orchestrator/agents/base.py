"""BaseAgent: shared scaffolding for every agent in the pipeline.

Every concrete agent (Triager, Investigator, FixAuthor, Verifier,
Releaser) inherits from :class:`BaseAgent` and overrides :meth:`run`.
The base class exposes a single helper, :meth:`_call_llm`, that wraps
``openai.AsyncOpenAI.chat.completions.create`` in a child span so:

1. Every LLM round-trip shows up in the trace tree with input/output
   character counts and token usage (when the SDK reports it).
2. Concrete agents stay free of tracing boilerplate.

The class is intentionally tiny — heavier abstractions tend to fight the
agent-specific prompts each subclass actually needs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from sqlmodel import Session

    from orchestrator.models import Incident
    from orchestrator.tracing import SpanContext, Tracer


class BaseAgent(ABC):
    """Abstract base for every agent in the pipeline.

    Subclasses set ``name`` (used in AgentRun rows + span names) and
    ``model`` (the OpenAI model identifier). The constructor takes a
    shared async OpenAI client and the process-wide tracer; both are
    injected so tests can swap in mocks without monkeypatching.
    """

    #: Short identifier used in AgentRun.agent_name and span names.
    name: str = "base"
    #: Default OpenAI model. Override per agent based on cost / capability.
    model: str = "gpt-4o-mini"

    def __init__(self, openai_client: Any, tracer: "Tracer") -> None:
        self.client = openai_client
        self.tracer = tracer

    @abstractmethod
    async def run(
        self,
        *,
        incident: "Incident",
        span: "SpanContext",
        session: "Session",
    ) -> dict:
        """Execute the agent against an incident.

        Subclasses MUST:
        * record significant events via ``span.event(...)``,
        * call :meth:`_call_llm` for every LLM round-trip (so spans are
          recorded automatically),
        * persist any mutations to ``incident`` back through ``session``.

        Returns a JSON-serializable dict that the state machine will
        stash in the corresponding ``AgentRun.output_summary``.
        """
        raise NotImplementedError

    async def _call_llm(
        self,
        system: str,
        user: str,
        span: "SpanContext",
        response_format: Optional[dict] = None,
    ) -> str:
        """Call the LLM under a child ``llm_call`` span.

        The span captures input character counts on entry and response
        size + token usage on completion. We deliberately don't log full
        prompts/responses here — they can be enormous and frequently
        contain PII pulled from production tracebacks. Subclasses that
        need richer logging should add their own ``span.event(...)``
        calls before/after invoking this helper.
        """
        with span.child_span("llm_call", metadata={"model": self.model}) as llm_span:
            llm_span.event(
                "prompt",
                {
                    "system_chars": len(system),
                    "user_chars": len(user),
                },
            )

            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if response_format is not None:
                kwargs["response_format"] = response_format

            response = await self.client.chat.completions.create(**kwargs)

            content = response.choices[0].message.content or ""
            tokens = None
            try:
                if response.usage is not None:
                    tokens = response.usage.total_tokens
            except Exception:
                tokens = None

            llm_span.event(
                "response",
                {
                    "content_chars": len(content),
                    "tokens": tokens,
                },
            )
            return content

    async def _call_llm_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        span: "SpanContext",
        tool_choice: str = "auto",
    ) -> Any:
        """Call the LLM in tool-calling mode and record usage on the span.

        Returns the raw ``message`` object from the first choice — the
        caller is responsible for inspecting ``.tool_calls`` and
        ``.content``. We keep this thin on purpose: the Investigator
        agent needs to loop on the response and that loop is much
        easier to reason about when the helper doesn't try to "do" the
        loop for it.

        Records the same ``prompt`` / ``response`` events as
        :meth:`_call_llm` so any agent using tool-calling shows up in
        the trace tree consistently. We also stamp the number of tool
        calls the assistant emitted, which is the headline metric for
        budget tracking.
        """
        with span.child_span(
            "llm_call",
            metadata={"model": self.model, "tool_calling": True},
        ) as llm_span:
            # Pre-call: approximate prompt size by summing the content
            # of every message. Tool messages can be huge so this is
            # the only honest measure we have without tokenizing.
            prompt_chars = 0
            for m in messages:
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(c, str):
                    prompt_chars += len(c)
            llm_span.event(
                "prompt",
                {
                    "messages": len(messages),
                    "prompt_chars": prompt_chars,
                    "tools": len(tools),
                },
            )

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            )

            choice = response.choices[0]
            message = choice.message

            content = getattr(message, "content", None) or ""
            tool_calls = getattr(message, "tool_calls", None) or []

            tokens = None
            try:
                if response.usage is not None:
                    tokens = response.usage.total_tokens
            except Exception:
                tokens = None

            llm_span.event(
                "response",
                {
                    "content_chars": len(content),
                    "tool_calls": len(tool_calls),
                    "tokens": tokens,
                    "finish_reason": getattr(choice, "finish_reason", None),
                },
            )
            return message


__all__ = ["BaseAgent"]
