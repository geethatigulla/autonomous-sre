"""Runtime configuration loaded from environment variables via pydantic-settings."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


TraceBackend = Literal["auto", "omium", "local"]


def _env_file() -> Optional[str]:
    """Which dotenv file to read. ``SRE_ENV_FILE=""`` disables it (tests)."""
    value = os.environ.get("SRE_ENV_FILE", ".env")
    return value or None


class Settings(BaseSettings):
    """Top-level runtime settings for the orchestrator.

    All values are read from environment variables (or a local ``.env`` file)
    via ``pydantic-settings``. Anything required at import time should have a
    safe default so the app can still boot in degraded mode for demos.
    """

    model_config = SettingsConfigDict(
        env_file=_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- LLM ---------------------------------------------------------------
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")

    # ---- GitHub ------------------------------------------------------------
    github_token: Optional[str] = Field(default=None, alias="GITHUB_TOKEN")
    github_repo: Optional[str] = Field(default=None, alias="GITHUB_REPO")

    # ---- Investigation -----------------------------------------------------
    tavily_api_key: Optional[str] = Field(default=None, alias="TAVILY_API_KEY")
    # Hard cap on tool calls the Investigator may issue in a single
    # incident. Keeps token usage bounded even if the LLM enters a
    # loop. Five is enough for "read the suspect file, read recent
    # commits, maybe one web search, done."
    investigator_max_tool_calls: int = Field(
        default=5, alias="INVESTIGATOR_MAX_TOOL_CALLS"
    )

    # ---- Notifications -----------------------------------------------------
    slack_webhook_url: Optional[str] = Field(default=None, alias="SLACK_WEBHOOK_URL")

    # ---- Persistence -------------------------------------------------------
    database_url: str = Field(
        default="sqlite:///./sre.db",
        alias="DATABASE_URL",
    )

    # ---- Concurrency -------------------------------------------------------
    # Upper bound on incident pipelines running at the same time. Each one
    # issues several LLM calls and runs pytest sandboxes, so a burst of
    # alerts queues up instead of stampeding the provider rate limits.
    max_concurrent_incidents: int = Field(
        default=4, ge=1, alias="MAX_CONCURRENT_INCIDENTS"
    )

    # ---- Omium tracing -----------------------------------------------------
    # Empty string == not configured. The tracing wrapper degrades gracefully
    # to local JSONL logging if the key is missing.
    omium_api_key: Optional[str] = Field(default=None, alias="OMIUM_API_KEY")
    omium_api_base: Optional[str] = Field(default=None, alias="OMIUM_API_BASE")
    omium_project: str = Field(default="autonomous-sre", alias="OMIUM_PROJECT")

    # "auto"  -> try Omium, fall back to local on any error (default)
    # "omium" -> only Omium; surfaces errors loudly
    # "local" -> only local JSONL; never touches the Omium SDK
    trace_backend: TraceBackend = Field(default="auto", alias="TRACE_BACKEND")

    # Where the local JSONL trace fallback lives. Relative paths resolve
    # against the working directory.
    local_trace_path: str = Field(
        default="orchestrator/.local_traces.jsonl",
        alias="LOCAL_TRACE_PATH",
    )

    @property
    def omium_configured(self) -> bool:
        """True only if we have a non-empty API key."""
        return bool(self.omium_api_key and self.omium_api_key.strip())

    def config_report(self) -> dict[str, dict]:
        """Describe which settings are present, for startup logs and /ready.

        Returns ``{name: {"ok": bool, "required": bool, "detail": str}}``.
        Never includes secret values.
        """

        def has(value: Optional[str]) -> bool:
            return bool(value and value.strip())

        repo_ok = has(self.github_repo) and "/" in (self.github_repo or "")             and self.github_repo.strip() != "owner/repo"
        return {
            "OPENAI_API_KEY": {
                "ok": has(self.openai_api_key),
                "required": True,
                "detail": "LLM provider for the Triager, Investigator, Fix Authors and Releaser",
            },
            "GITHUB_TOKEN": {
                "ok": has(self.github_token),
                "required": True,
                "detail": "reads source and opens pull requests",
            },
            "GITHUB_REPO": {
                "ok": repo_ok,
                "required": True,
                "detail": "target repo in owner/repo form"
                + ("" if repo_ok else " (unset, malformed, or still the placeholder)"),
            },
            "TAVILY_API_KEY": {
                "ok": has(self.tavily_api_key),
                "required": False,
                "detail": "web search during investigation",
            },
            "SLACK_WEBHOOK_URL": {
                "ok": has(self.slack_webhook_url),
                "required": False,
                "detail": "notification when a PR opens",
            },
            "OMIUM_API_KEY": {
                "ok": self.omium_configured,
                "required": False,
                "detail": "remote tracing (local JSONL is always written)",
            },
        }

    def missing_required(self) -> list[str]:
        """Names of required settings that are not configured."""
        return [
            name
            for name, info in self.config_report().items()
            if info["required"] and not info["ok"]
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor — cached so repeated lookups are cheap."""
    return Settings()
