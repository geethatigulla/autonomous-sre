"""Thin async wrapper around PyGithub for reading code and recent history.

PyGithub is synchronous, so every public method here defers the blocking
network call to ``asyncio.to_thread``. Each method accepts an optional
:class:`~orchestrator.tracing.SpanContext` and, when provided, opens a
child span named ``github.<method>`` with input/output character counts
recorded as span events. Exceptions never propagate — callers (the
Investigator agent) treat empty returns as "the tool didn't help" and
proceed with whatever evidence they already have.

If construction fails (empty token, malformed repo name, transient
network error) the tool flips ``self.disabled = True`` and every method
short-circuits to a typed empty result. This is the "graceful
degradation" path the Investigator's system prompt is aware of — the
LLM is told the tool is unavailable in its next turn and is expected to
still emit *some* hypothesis purely from the traceback.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

try:  # PyGithub is a hard requirement but be defensive on import.
    from github import Auth, Github
    from github.GithubException import (
        GithubException,
        RateLimitExceededException,
        UnknownObjectException,
    )

    _GITHUB_IMPORTABLE = True
except Exception:  # pragma: no cover - defensive
    Github = None  # type: ignore[assignment]
    Auth = None  # type: ignore[assignment]
    GithubException = Exception  # type: ignore[assignment,misc]
    RateLimitExceededException = Exception  # type: ignore[assignment,misc]
    UnknownObjectException = Exception  # type: ignore[assignment,misc]
    _GITHUB_IMPORTABLE = False


log = logging.getLogger("orchestrator.tools.github")


# Truncation budgets — chosen so we fit comfortably inside a single
# tool message in the chat completion (the LLM sees the same string).
_FILE_CHAR_CAP = 8000
_PATCH_CHAR_CAP = 2000
_MESSAGE_CHAR_CAP = 200
_EVENT_TRUNCATE = 500  # characters logged on each span event (in/out)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Return ``(text[:limit], truncated?)``. Safe against ``None``."""
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class GitHubTool:
    """Async PyGithub wrapper used by the Investigator agent.

    Construction is total: any failure (missing token, bad repo, library
    unavailable) leaves the object in a ``disabled`` state. Every method
    checks that flag and returns an empty typed value rather than
    raising — see module docstring.
    """

    def __init__(self, token: Optional[str], repo_name: Optional[str], tracer: Any) -> None:
        self.tracer = tracer
        self.repo_name = repo_name or ""
        self._client: Any = None
        self._repo: Any = None
        self.disabled: bool = False
        # Cache of (requested_path, ref) -> resolved_path (or None).
        # Saves a round-trip when the LLM repeatedly asks for the same
        # mis-prefixed path within an investigation.
        self._path_cache: dict[tuple[str, str], Optional[str]] = {}
        # Set to the path that was actually resolved by the most recent
        # read_file / read_file_at_lines call. Downstream agents (Fix
        # Author) inspect this so they can persist the canonical path
        # back onto the incident.
        self.last_resolved_path: Optional[str] = None

        if not _GITHUB_IMPORTABLE:
            log.warning("PyGithub not importable — GitHubTool disabled")
            self.disabled = True
            return

        if not token or not token.strip():
            log.warning("GitHub token missing — GitHubTool disabled")
            self.disabled = True
            return

        if not repo_name or "/" not in repo_name:
            log.warning(
                "GitHub repo missing or malformed (%r) — GitHubTool disabled",
                repo_name,
            )
            self.disabled = True
            return

        try:
            # Use Auth.Token for forward-compat with PyGithub >= 2.x.
            auth = Auth.Token(token)
            self._client = Github(auth=auth)
            self._repo = self._client.get_repo(repo_name)
        except Exception as e:  # noqa: BLE001 — total construction
            log.warning(
                "GitHubTool init failed for repo %r: %s — disabling",
                repo_name,
                e,
            )
            self.disabled = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _span_event(self, span: Any, name: str, data: dict) -> None:
        """Record a span event if a span was supplied."""
        if span is None:
            return
        try:
            span.event(name, data)
        except Exception:  # pragma: no cover — tracing must never crash a tool
            log.debug("span.event(%s) failed", name, exc_info=True)

    def _open_child(self, span: Any, name: str, metadata: dict) -> Any:
        """Open ``span.child_span(name, metadata=...)`` if available.

        Returns a context manager. When ``span`` is ``None`` returns a
        no-op context manager so callers can `with` unconditionally.
        """
        if span is not None:
            try:
                return span.child_span(name, metadata=metadata)
            except Exception:  # pragma: no cover
                pass

        class _Noop:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, et, e, tb):
                return False

            def event(self_inner, *_args, **_kwargs):
                return None

        return _Noop()

    def _record_error(self, span_or_child: Any, err: Exception) -> None:
        """Record ``github_error`` (or ``github_rate_limit``) on the child span."""
        try:
            if isinstance(err, RateLimitExceededException):
                reset = None
                try:
                    # PyGithub exposes the rate limit on the underlying client.
                    if self._client is not None:
                        reset = str(self._client.get_rate_limit().core.reset)
                except Exception:
                    reset = None
                span_or_child.event(
                    "github_rate_limit",
                    {"reset": reset, "error": str(err)[:300]},
                )
                return
            span_or_child.event(
                "github_error",
                {"error": str(err)[:300], "type": type(err).__name__},
            )
        except Exception:  # pragma: no cover
            log.debug("could not record github error", exc_info=True)

    # ------------------------------------------------------------------
    # Synchronous PyGithub callees (run via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _sync_read_file(self, path: str, ref: str) -> str:
        contents = self._repo.get_contents(path, ref=ref)
        if isinstance(contents, list):  # directory — we don't recurse
            return ""
        try:
            data = contents.decoded_content.decode("utf-8", errors="replace")
        except Exception:
            data = ""
        return data

    def _sync_probe_path(self, path: str, ref: str) -> bool:
        """Return True if ``path`` exists in ``ref``. Raises only on
        non-404 errors so the caller can distinguish "missing" from
        "transient API failure"."""
        contents = self._repo.get_contents(path, ref=ref)
        # A directory listing also counts as "exists" but we don't try
        # to read it as a file; the caller will see this as a non-None
        # match and probe further if needed.
        return contents is not None

    def _sync_walk_for_basename(self, basename: str, ref: str) -> Optional[str]:
        """Walk the repo tree once and return the first path whose final
        segment equals ``basename``. Returns ``None`` if not found."""
        tree = self._repo.get_git_tree(ref, recursive=True)
        for item in getattr(tree, "tree", []) or []:
            item_path = getattr(item, "path", "") or ""
            if item_path == basename or item_path.endswith("/" + basename):
                return item_path
        return None

    def _sync_list_commits(self, path: Optional[str], limit: int) -> list[dict]:
        kwargs: dict[str, Any] = {}
        if path:
            kwargs["path"] = path
        results: list[dict] = []
        # PyGithub returns a PaginatedList; slicing is the recommended pattern.
        for commit in self._repo.get_commits(**kwargs)[:limit]:
            try:
                author = commit.commit.author.name if commit.commit.author else ""
                date = (
                    commit.commit.author.date.isoformat()
                    if commit.commit.author and commit.commit.author.date
                    else ""
                )
                message = commit.commit.message or ""
            except Exception:
                author, date, message = "", "", ""
            message, _ = _truncate(message, _MESSAGE_CHAR_CAP)
            results.append(
                {
                    "sha": commit.sha,
                    "author": author,
                    "date": date,
                    "message": message,
                }
            )
        return results

    def _sync_get_commit_diff(self, sha: str) -> dict:
        commit = self._repo.get_commit(sha)
        files_out: list[dict] = []
        for f in commit.files or []:
            patch_text = getattr(f, "patch", None) or ""
            patch_out, _ = _truncate(patch_text, _PATCH_CHAR_CAP)
            files_out.append(
                {
                    "filename": getattr(f, "filename", ""),
                    "patch": patch_out,
                    "additions": int(getattr(f, "additions", 0) or 0),
                    "deletions": int(getattr(f, "deletions", 0) or 0),
                }
            )
        msg, _ = _truncate(commit.commit.message or "", _MESSAGE_CHAR_CAP)
        return {"sha": commit.sha, "message": msg, "files": files_out}

    def _sync_list_prs(self, limit: int) -> list[dict]:
        prs = self._repo.get_pulls(state="all", sort="created", direction="desc")
        out: list[dict] = []
        for pr in prs[:limit]:
            try:
                merged_at = pr.merged_at.isoformat() if pr.merged_at else None
            except Exception:
                merged_at = None
            title, _ = _truncate(pr.title or "", _MESSAGE_CHAR_CAP)
            out.append(
                {
                    "number": pr.number,
                    "title": title,
                    "state": pr.state,
                    "merged_at": merged_at,
                    "author": pr.user.login if pr.user else "",
                    "url": pr.html_url,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    async def _resolve_path(
        self,
        requested_path: str,
        ref: str = "main",
        span: Any = None,
    ) -> Optional[str]:
        """Find the path that actually exists in the repo.

        The LLM frequently asks for ``victim_app/app.py`` when the file
        actually lives at the repo root as ``app.py`` (or vice versa).
        Rather than make the caller try variants, we try a small set of
        candidates here and fall back to walking the default tree by
        basename. The first match wins and is cached for the lifetime of
        this tool instance.

        Returns the resolved path, or ``None`` if nothing matched.
        """
        if self.disabled or not requested_path:
            return None

        cache_key = (requested_path, ref or "main")
        if cache_key in self._path_cache:
            return self._path_cache[cache_key]

        basename = requested_path.rsplit("/", 1)[-1]
        candidates: list[str] = [requested_path]
        if basename and basename != requested_path:
            candidates.append(basename)
        # Also try a couple of common subdirectories — cheap insurance
        # for the inverse mistake (LLM strips the prefix when it should
        # have kept it).
        for prefix in ("victim_app", "src", "app"):
            alt = f"{prefix}/{basename}"
            if alt not in candidates:
                candidates.append(alt)

        for candidate in candidates:
            try:
                await asyncio.to_thread(self._sync_probe_path, candidate, ref)
            except UnknownObjectException:
                continue
            except Exception as e:  # noqa: BLE001 — any other error: keep trying
                self._span_event(
                    span,
                    "path_probe_error",
                    {"candidate": candidate, "error": str(e)[:300]},
                )
                continue
            self._span_event(
                span,
                "path_resolved",
                {"requested": requested_path, "resolved": candidate},
            )
            self._path_cache[cache_key] = candidate
            return candidate

        # Last resort: walk the tree once and find by basename.
        try:
            found = await asyncio.to_thread(
                self._sync_walk_for_basename, basename, ref
            )
        except Exception as e:  # noqa: BLE001
            self._span_event(
                span,
                "path_walk_failed",
                {"error": str(e)[:300]},
            )
            found = None

        if found:
            self._span_event(
                span,
                "path_resolved_via_walk",
                {"requested": requested_path, "resolved": found},
            )
            self._path_cache[cache_key] = found
            return found

        self._span_event(
            span,
            "path_resolution_failed",
            {"requested": requested_path, "candidates_tried": candidates},
        )
        self._path_cache[cache_key] = None
        return None

    # ------------------------------------------------------------------
    # Public async surface
    # ------------------------------------------------------------------

    async def read_file(self, path: str, ref: str = "main", span: Any = None) -> str:
        """Return the file contents at ``path`` on ``ref`` (or empty on failure).

        Before reading, :meth:`_resolve_path` is consulted so callers
        that pass a slightly-wrong path (``victim_app/app.py`` when the
        repo actually has ``app.py`` at the root) still get content.
        The resolved path is stashed on ``self.last_resolved_path`` so
        downstream agents can persist the canonical path.
        """
        with self._open_child(
            span, "github.read_file", {"path": path, "ref": ref}
        ) as child:
            child.event(
                "input",
                {"path": path[:_EVENT_TRUNCATE], "ref": ref[:_EVENT_TRUNCATE]},
            )
            if self.disabled:
                child.event("github_disabled", {})
                self.last_resolved_path = None
                return ""

            resolved = await self._resolve_path(path, ref=ref, span=child)
            if resolved is None:
                child.event("file_not_found", {"requested": path})
                self.last_resolved_path = None
                return ""

            try:
                raw = await asyncio.to_thread(self._sync_read_file, resolved, ref)
            except UnknownObjectException as e:
                # Should be rare — _resolve_path just confirmed it
                # exists, but the ref could race. Treat as not found.
                self._record_error(child, e)
                child.event("not_found", {"path": resolved})
                self.last_resolved_path = None
                return ""
            except Exception as e:  # noqa: BLE001
                self._record_error(child, e)
                self.last_resolved_path = None
                return ""

            self.last_resolved_path = resolved
            truncated_text, was_truncated = _truncate(raw, _FILE_CHAR_CAP)
            if was_truncated:
                child.event(
                    "truncated",
                    {"original_chars": len(raw), "kept_chars": _FILE_CHAR_CAP},
                )
            child.event(
                "output",
                {
                    "chars": len(truncated_text),
                    "resolved_path": resolved,
                    "preview": truncated_text[:_EVENT_TRUNCATE],
                },
            )
            return truncated_text

    async def read_file_at_lines(
        self,
        path: str,
        start: int,
        end: int,
        ref: str = "main",
        span: Any = None,
    ) -> str:
        """Return only lines ``[start..end]`` (1-indexed, inclusive) of ``path``."""
        with self._open_child(
            span,
            "github.read_file_at_lines",
            {"path": path, "ref": ref, "start": start, "end": end},
        ) as child:
            child.event(
                "input",
                {"path": path[:_EVENT_TRUNCATE], "start": start, "end": end},
            )
            if self.disabled:
                child.event("github_disabled", {})
                self.last_resolved_path = None
                return ""

            resolved = await self._resolve_path(path, ref=ref, span=child)
            if resolved is None:
                child.event("file_not_found", {"requested": path})
                self.last_resolved_path = None
                return ""

            try:
                raw = await asyncio.to_thread(self._sync_read_file, resolved, ref)
            except Exception as e:  # noqa: BLE001
                self._record_error(child, e)
                self.last_resolved_path = None
                return ""

            self.last_resolved_path = resolved

            lines = raw.splitlines()
            # Coerce indices: clamp to [1, len]. Inclusive on both sides.
            try:
                s = max(1, int(start))
                e = min(len(lines), int(end))
            except (TypeError, ValueError):
                s, e = 1, len(lines)
            if s > e:
                child.event("empty_range", {"start": s, "end": e})
                return ""
            sliced = "\n".join(lines[s - 1 : e])
            truncated_text, was_truncated = _truncate(sliced, _FILE_CHAR_CAP)
            if was_truncated:
                child.event(
                    "truncated",
                    {
                        "original_chars": len(sliced),
                        "kept_chars": _FILE_CHAR_CAP,
                    },
                )
            child.event(
                "output",
                {
                    "chars": len(truncated_text),
                    "preview": truncated_text[:_EVENT_TRUNCATE],
                },
            )
            return truncated_text

    async def list_recent_commits(
        self,
        path: Optional[str] = None,
        limit: int = 10,
        span: Any = None,
    ) -> list[dict]:
        """Return up to ``limit`` recent commits, optionally filtered by ``path``."""
        with self._open_child(
            span,
            "github.list_recent_commits",
            {"path": path, "limit": limit},
        ) as child:
            child.event(
                "input",
                {"path": (path or "")[:_EVENT_TRUNCATE], "limit": int(limit)},
            )
            if self.disabled:
                child.event("github_disabled", {})
                return []
            try:
                commits = await asyncio.to_thread(
                    self._sync_list_commits, path, int(limit)
                )
            except Exception as e:  # noqa: BLE001
                self._record_error(child, e)
                return []
            preview = str([c.get("sha", "")[:7] for c in commits])[:_EVENT_TRUNCATE]
            child.event("output", {"count": len(commits), "preview": preview})
            return commits

    async def get_commit_diff(self, sha: str, span: Any = None) -> dict:
        """Return the diff (with truncated patches) for a single commit SHA."""
        with self._open_child(
            span, "github.get_commit_diff", {"sha": sha}
        ) as child:
            child.event("input", {"sha": (sha or "")[:_EVENT_TRUNCATE]})
            if self.disabled:
                child.event("github_disabled", {})
                return {}
            try:
                diff = await asyncio.to_thread(self._sync_get_commit_diff, sha)
            except Exception as e:  # noqa: BLE001
                self._record_error(child, e)
                return {}
            preview = ", ".join(f.get("filename", "") for f in diff.get("files", []))
            child.event(
                "output",
                {
                    "files": len(diff.get("files", [])),
                    "preview": preview[:_EVENT_TRUNCATE],
                },
            )
            return diff

    async def list_recent_prs(self, limit: int = 5, span: Any = None) -> list[dict]:
        """Return up to ``limit`` recent pull requests across all states."""
        with self._open_child(
            span, "github.list_recent_prs", {"limit": limit}
        ) as child:
            child.event("input", {"limit": int(limit)})
            if self.disabled:
                child.event("github_disabled", {})
                return []
            try:
                prs = await asyncio.to_thread(self._sync_list_prs, int(limit))
            except Exception as e:  # noqa: BLE001
                self._record_error(child, e)
                return []
            preview = str([p.get("number") for p in prs])[:_EVENT_TRUNCATE]
            child.event("output", {"count": len(prs), "preview": preview})
            return prs

    # ------------------------------------------------------------------
    # Write surface — used by the Releaser to open a real PR.
    # ------------------------------------------------------------------
    # These methods deliberately propagate exceptions (unlike the
    # read-side methods that swallow). The Releaser orchestrates the PR
    # workflow and needs to know precisely which step failed so it can
    # populate AgentRun.error_message accurately.

    def _sync_get_file(self, path: str, ref: str) -> tuple[str, str]:
        """Return ``(sha, decoded_content)`` for a file on a ref.

        Raises ``UnknownObjectException`` if the file is absent.
        """
        contents = self._repo.get_contents(path, ref=ref)
        if isinstance(contents, list):
            raise RuntimeError(
                f"path {path!r} on {ref!r} is a directory, not a file"
            )
        try:
            data = contents.decoded_content.decode("utf-8", errors="replace")
        except Exception:
            data = ""
        return contents.sha, data

    def _sync_get_branch_sha(self, branch: str) -> str:
        ref = self._repo.get_git_ref(f"heads/{branch}")
        return ref.object.sha

    def _sync_create_branch(self, branch: str, base_sha: str) -> str:
        ref = self._repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
        return ref.ref

    def _sync_branch_exists(self, branch: str) -> bool:
        try:
            self._repo.get_git_ref(f"heads/{branch}")
            return True
        except UnknownObjectException:
            return False
        except GithubException as exc:
            # PyGithub raises GithubException(404, ...) for missing refs
            # in some versions; treat 404 as "missing".
            status = getattr(exc, "status", None)
            if status == 404:
                return False
            raise

    def _sync_update_file(
        self,
        path: str,
        message: str,
        content: str,
        sha: str,
        branch: str,
    ) -> dict:
        result = self._repo.update_file(
            path=path,
            message=message,
            content=content,
            sha=sha,
            branch=branch,
        )
        commit = result.get("commit") if isinstance(result, dict) else None
        commit_sha = getattr(commit, "sha", None) if commit is not None else None
        return {"commit_sha": commit_sha}

    def _sync_find_pr_by_head(self, branch: str) -> Optional[dict]:
        # Repo owner/login is needed when filtering by head.
        try:
            owner_login = self._repo.owner.login
        except Exception:
            owner_login = self.repo_name.split("/", 1)[0] if "/" in self.repo_name else ""
        head_filter = f"{owner_login}:{branch}" if owner_login else branch
        prs = self._repo.get_pulls(state="open", head=head_filter)
        for pr in prs[:1]:
            return {
                "number": pr.number,
                "url": pr.html_url,
                "state": pr.state,
                "head": pr.head.ref if pr.head else "",
                "base": pr.base.ref if pr.base else "",
            }
        # Some servers ignore the head filter when the branch doesn't
        # belong to the same owner — fall back to a manual scan.
        try:
            for pr in self._repo.get_pulls(state="open")[:50]:
                if pr.head and pr.head.ref == branch:
                    return {
                        "number": pr.number,
                        "url": pr.html_url,
                        "state": pr.state,
                        "head": pr.head.ref,
                        "base": pr.base.ref if pr.base else "",
                    }
        except Exception:
            pass
        return None

    def _sync_create_pull(
        self, title: str, body: str, head: str, base: str
    ) -> dict:
        pr = self._repo.create_pull(title=title, body=body, head=head, base=base)
        return {
            "number": pr.number,
            "url": pr.html_url,
            "state": pr.state,
            "head": pr.head.ref if pr.head else head,
            "base": pr.base.ref if pr.base else base,
        }

    async def get_file_with_sha(
        self,
        path: str,
        ref: str = "main",
        span: Any = None,
    ) -> dict:
        """Return ``{"sha": str, "content": str, "path": str}`` for a file.

        Resolves the path against the repo tree the same way
        :meth:`read_file` does so the Releaser can target the canonical
        path even if the LLM persisted a slightly-wrong one. Raises if
        the file cannot be found or read.
        """
        if self.disabled:
            raise RuntimeError("GitHubTool is disabled (missing token/repo)")
        with self._open_child(
            span, "github.get_file_with_sha", {"path": path, "ref": ref}
        ) as child:
            resolved = await self._resolve_path(path, ref=ref, span=child)
            if resolved is None:
                child.event("file_not_found", {"requested": path})
                raise RuntimeError(
                    f"GitHub: could not find {path!r} on {ref!r}"
                )
            try:
                sha, content = await asyncio.to_thread(
                    self._sync_get_file, resolved, ref
                )
            except Exception as e:
                self._record_error(child, e)
                raise
            child.event(
                "output",
                {"resolved_path": resolved, "sha": sha[:10], "chars": len(content)},
            )
            return {"sha": sha, "content": content, "path": resolved}

    async def get_branch_sha(self, branch: str, span: Any = None) -> str:
        """Return the tip-commit SHA of ``branch``."""
        if self.disabled:
            raise RuntimeError("GitHubTool is disabled")
        with self._open_child(
            span, "github.get_branch_sha", {"branch": branch}
        ) as child:
            try:
                sha = await asyncio.to_thread(self._sync_get_branch_sha, branch)
            except Exception as e:
                self._record_error(child, e)
                raise
            child.event("output", {"branch": branch, "sha": sha[:10]})
            return sha

    async def ensure_branch(
        self,
        branch: str,
        base: str = "main",
        span: Any = None,
    ) -> dict:
        """Create ``branch`` off ``base`` if it doesn't exist already.

        Returns ``{"created": bool, "branch": str, "base_sha": str}``.
        Idempotent: if the branch already exists we don't try to reset
        it (Releaser retries land on top of the existing branch).
        """
        if self.disabled:
            raise RuntimeError("GitHubTool is disabled")
        with self._open_child(
            span,
            "github.ensure_branch",
            {"branch": branch, "base": base},
        ) as child:
            try:
                exists = await asyncio.to_thread(
                    self._sync_branch_exists, branch
                )
            except Exception as e:
                self._record_error(child, e)
                raise
            if exists:
                # We still return the base sha for symmetry — it's the
                # tip of ``base`` at this moment, useful for diagnostics.
                try:
                    base_sha = await asyncio.to_thread(
                        self._sync_get_branch_sha, base
                    )
                except Exception:
                    base_sha = ""
                child.event(
                    "branch_already_exists",
                    {"branch": branch, "base": base, "base_sha": base_sha[:10]},
                )
                return {"created": False, "branch": branch, "base_sha": base_sha}

            try:
                base_sha = await asyncio.to_thread(self._sync_get_branch_sha, base)
                await asyncio.to_thread(
                    self._sync_create_branch, branch, base_sha
                )
            except Exception as e:
                self._record_error(child, e)
                raise
            child.event(
                "branch_created",
                {"branch": branch, "base": base, "base_sha": base_sha[:10]},
            )
            return {"created": True, "branch": branch, "base_sha": base_sha}

    async def update_file_on_branch(
        self,
        path: str,
        message: str,
        content: str,
        sha: str,
        branch: str,
        span: Any = None,
    ) -> dict:
        """Commit ``content`` to ``path`` on ``branch``.

        ``sha`` must be the current blob sha of ``path`` on ``branch``
        (or on the base if the branch was freshly created from base).
        Returns the resulting commit metadata.
        """
        if self.disabled:
            raise RuntimeError("GitHubTool is disabled")
        with self._open_child(
            span,
            "github.update_file_on_branch",
            {"path": path, "branch": branch, "chars": len(content)},
        ) as child:
            try:
                result = await asyncio.to_thread(
                    self._sync_update_file,
                    path,
                    message,
                    content,
                    sha,
                    branch,
                )
            except Exception as e:
                self._record_error(child, e)
                raise
            child.event(
                "file_updated",
                {
                    "path": path,
                    "branch": branch,
                    "commit_sha": (result.get("commit_sha") or "")[:10],
                },
            )
            return result

    async def find_pr_by_head(
        self, branch: str, span: Any = None
    ) -> Optional[dict]:
        """Return an open PR with ``head=branch`` if one exists."""
        if self.disabled:
            return None
        with self._open_child(
            span, "github.find_pr_by_head", {"branch": branch}
        ) as child:
            try:
                pr = await asyncio.to_thread(self._sync_find_pr_by_head, branch)
            except Exception as e:
                self._record_error(child, e)
                return None
            if pr is None:
                child.event("no_existing_pr", {"branch": branch})
                return None
            child.event(
                "existing_pr_found",
                {"branch": branch, "number": pr.get("number"), "url": pr.get("url")},
            )
            return pr

    async def create_pull(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        span: Any = None,
    ) -> dict:
        """Open a PR from ``head`` into ``base`` and return its metadata."""
        if self.disabled:
            raise RuntimeError("GitHubTool is disabled")
        with self._open_child(
            span,
            "github.create_pull",
            {"head": head, "base": base, "title_chars": len(title)},
        ) as child:
            try:
                pr = await asyncio.to_thread(
                    self._sync_create_pull, title, body, head, base
                )
            except Exception as e:
                self._record_error(child, e)
                raise
            child.event(
                "pull_created",
                {
                    "number": pr.get("number"),
                    "url": pr.get("url"),
                    "head": pr.get("head"),
                    "base": pr.get("base"),
                },
            )
            return pr


__all__ = ["GitHubTool"]
