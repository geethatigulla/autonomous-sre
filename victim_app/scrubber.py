"""Outbound PII & secret scrubber for the victim app's crash reports.

This module is the *first* line of defense for sensitive data leaving the
monitored application. The exception-reporting middleware in
:mod:`victim_app.monitor` runs every alert payload through
:func:`scrub_dict` *before* POSTing to the orchestrator — so secrets,
credentials, and PII never travel beyond the victim app's process at all.

Design constraints (intentional):

* **Pure regex + stdlib only.** No Presidio, no spaCy, no external
  dependencies. The production upgrade path is Microsoft Presidio or a
  managed DLP service — keep the public ``scrub`` / ``scrub_dict``
  surface stable so swapping the implementation later is a one-file
  change.
* **Never raises.** A crash report scrubber that crashes is strictly
  worse than no scrubber at all: it eats the original incident. Every
  pattern is wrapped in ``try/except``; a failing pattern is skipped,
  not fatal.
* **Conservative redaction.** A developer reading a scrubbed crash report
  must still see a useful traceback. We redact *values*, not structure,
  and rely on keyword-anchored matches where possible. The credit-card
  detector runs a Luhn check to avoid mangling random long digit runs
  (timestamps, IDs, order numbers).
* **Pattern ordering matters.** The specific patterns
  (``private_key``, ``jwt``, ``bearer_token``) run *before* the more
  greedy generic ``api_key`` pattern so a JWT isn't mis-classified as
  an API key. The order is encoded in :data:`_PATTERNS`.

Every match is replaced with the literal placeholder
``[REDACTED:<type>]`` — short enough to keep tracebacks readable, and
greppable for downstream auditing.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Pattern


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------

_PLACEHOLDER = "[REDACTED:{kind}]"


def _ph(kind: str) -> str:
    return _PLACEHOLDER.format(kind=kind)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
#
# Order is significant — see module docstring. The two custom callables
# (password / credit card) are handled inline by _apply_pattern below
# because they need slightly more than a flat regex.replace.


# Private key blocks. We deliberately swallow everything between the
# BEGIN / END fences (including newlines) — the entire body is sensitive
# and there's no debugging value in seeing half of an RSA key.
_RE_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    re.IGNORECASE,
)

# JWT: three base64url segments separated by dots, header typically
# starts with "eyJ" (base64 of `{"`). Length floor avoids false positives
# on short version strings like "v1.2.3".
_RE_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"
)

# Authorization headers — both "Authorization: Bearer ..." and bare
# "Bearer ..." forms. We keep the "Bearer " marker so the scrubbed log
# still tells you *that* an auth token was present.
_RE_BEARER = re.compile(
    r"(?i)(Bearer\s+)([A-Za-z0-9._\-/+=]{8,})"
)

# Provider-specific API key shapes. These are anchored to vendor
# prefixes so they almost never false-positive.
_RE_API_KEY_PREFIXED = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_\-]{16,}"         # OpenAI / OpenAI-compatible
    r"|ghp_[A-Za-z0-9]{20,}"          # GitHub personal access token
    r"|gho_[A-Za-z0-9]{20,}"          # GitHub OAuth
    r"|ghs_[A-Za-z0-9]{20,}"          # GitHub server-to-server
    r"|github_pat_[A-Za-z0-9_]{20,}"  # GitHub fine-grained PAT
    r"|omium_[A-Za-z0-9_\-]{16,}"     # Omium
    r"|tvly-[A-Za-z0-9_\-]{16,}"      # Tavily
    r"|xoxb-[A-Za-z0-9\-]{10,}"       # Slack bot
    r"|xoxp-[A-Za-z0-9\-]{10,}"       # Slack user
    r"|AKIA[0-9A-Z]{16}"              # AWS access key
    r")\b"
)

# Generic "long token after a key-like word" — handles JSON, env vars,
# headers, key=value query strings, and YAML. We keep the key visible so
# the structure of the message is preserved.
#
# Examples that match:
#   api_key=abcd1234efgh5678ijkl
#   "apikey": "abcd1234efgh5678ijkl"
#   X-Api-Key: abcd1234efgh5678ijkl
#   access_token = "abcd1234efgh5678ijkl"
_RE_API_KEY_GENERIC = re.compile(
    r"(?ix)"
    r"("
    r"  (?:api[_\-]?key|apikey|access[_\-]?token|x[_\-]api[_\-]key|auth[_\-]?token)"
    r"  \s*[:=]\s*"
    r"  [\"']?"
    r")"
    r"([A-Za-z0-9_\-]{16,})"
    r"([\"']?)"
)

# Password / secret / passwd / pwd — redact the *value*, keep the key.
# Supports JSON ("password": "x"), key=value (password=x), and YAML
# (password: x). The keyword may be quoted as a JSON key, so we allow
# an optional closing quote between it and the colon. The capturing
# groups split (prefix)(value)(suffix) so the replacement keeps the
# surrounding quotes intact when present.
_RE_PASSWORD = re.compile(
    r"(?ix)"
    r"("
    r"  (?:password|passwd|pwd|secret)"
    r"  [\"']?"           # optional closing quote of a JSON-style key
    r"  \s*[:=]\s*"
    r"  [\"']?"           # optional opening quote of the value
    r")"
    r"([^\s,;}\"']+)"
    r"([\"']?)"
)

# Standard-ish email address. Intentionally not RFC-5322 strict — we'd
# rather over-match a few odd usernames than miss real emails.
_RE_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# IPv4. Off by default — IPs in stack traces are useful for debugging
# (which host crashed?), so this only fires when scrub() is asked for it.
_RE_IPV4 = re.compile(
    r"\b(?:25[0-5]|2[0-4]\d|[01]?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d?\d)){3}\b"
)

# Credit card candidate — 13-19 digit runs optionally separated by
# spaces or dashes in groups of four. The Luhn check below is what
# actually decides whether to redact.
_RE_CC_CANDIDATE = re.compile(
    r"\b(?:\d[ \-]?){12,18}\d\b"
)


# ---------------------------------------------------------------------------
# Luhn check (credit-card validation)
# ---------------------------------------------------------------------------


def _luhn_ok(digits: str) -> bool:
    """Return True if ``digits`` (string of 0-9 only) passes the Luhn check.

    Used to keep us from redacting random long numeric strings that
    happen to be 13-16 digits — order IDs, timestamps, etc.
    """
    if not digits or not digits.isdigit():
        return False
    total = 0
    # Process from rightmost digit
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48  # ord('0') == 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_credit_cards(text: str) -> tuple[str, bool]:
    """Replace Luhn-valid card-number candidates with [REDACTED:credit_card]."""
    found = False

    def _sub(m: re.Match[str]) -> str:
        nonlocal found
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found = True
            return _ph("credit_card")
        return raw

    return _RE_CC_CANDIDATE.sub(_sub, text), found


# ---------------------------------------------------------------------------
# Per-pattern application
# ---------------------------------------------------------------------------


def _sub_simple(
    rx: Pattern[str], placeholder: str, text: str
) -> tuple[str, bool]:
    """Replace every match of ``rx`` with ``placeholder``.

    Returns (new_text, found_anything).
    """
    found = False

    def _sub(_m: re.Match[str]) -> str:
        nonlocal found
        found = True
        return placeholder

    return rx.sub(_sub, text), found


def _sub_bearer(text: str) -> tuple[str, bool]:
    """Replace bearer tokens but keep the literal "Bearer " marker."""
    found = False

    def _sub(m: re.Match[str]) -> str:
        nonlocal found
        found = True
        return f"{m.group(1)}{_ph('bearer_token')}"

    return _RE_BEARER.sub(_sub, text), found


def _sub_password(text: str) -> tuple[str, bool]:
    """Redact the value half of password-like key/value pairs."""
    found = False

    def _sub(m: re.Match[str]) -> str:
        nonlocal found
        found = True
        return f"{m.group(1)}{_ph('password')}{m.group(3)}"

    return _RE_PASSWORD.sub(_sub, text), found


def _sub_api_key_generic(text: str) -> tuple[str, bool]:
    """Redact long tokens that appear after key-like words."""
    found = False

    def _sub(m: re.Match[str]) -> str:
        nonlocal found
        found = True
        return f"{m.group(1)}{_ph('api_key')}{m.group(3)}"

    return _RE_API_KEY_GENERIC.sub(_sub, text), found


# Ordered list of (redaction_type, callable). Callables take ``text`` and
# return ``(new_text, found_anything)``. Order is load-bearing — see the
# module docstring.
_PATTERNS: list[tuple[str, Any]] = [
    ("private_key",  lambda t: _sub_simple(_RE_PRIVATE_KEY, _ph("private_key"), t)),
    # bearer BEFORE jwt — a JWT used as a bearer token should be
    # classified as "bearer_token" (the credential context is what
    # matters for the operator audit line). The bare jwt pattern then
    # mops up any unprefixed JWTs that appear elsewhere in the payload.
    ("bearer_token", _sub_bearer),
    ("jwt",          lambda t: _sub_simple(_RE_JWT, _ph("jwt"), t)),
    ("password",     _sub_password),
    ("api_key",      lambda t: _sub_simple(_RE_API_KEY_PREFIXED, _ph("api_key"), t)),
    ("api_key",      _sub_api_key_generic),
    ("credit_card",  _redact_credit_cards),
    ("email",        lambda t: _sub_simple(_RE_EMAIL, _ph("email"), t)),
]

_IPV4_PATTERN: tuple[str, Any] = (
    "ip_address",
    lambda t: _sub_simple(_RE_IPV4, _ph("ip_address"), t),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrub(text: Any, *, include_ip: bool = False) -> tuple[str, list[str]]:
    """Redact secrets/PII from ``text``.

    Args:
        text: The string to scrub. Falsy inputs (``None``, ``""``, etc.)
            return ``("", [])``. Non-string inputs are coerced via
            ``str()`` so the function never crashes on the caller.
        include_ip: When True, also redact IPv4 addresses. Default off —
            IPs in stack traces are useful for debugging.

    Returns:
        ``(scrubbed_text, redaction_types)`` where ``redaction_types`` is
        a list of *distinct* placeholder types found (e.g.
        ``["api_key", "email"]``). The list is deduplicated but not sorted;
        :func:`scrub_dict` is responsible for sorting across the whole
        payload.
    """
    if not text:
        return "", []

    if not isinstance(text, str):
        # Coerce — we'd rather scrub the repr of a bytes/bytearray than
        # blow up. The middleware passes strings in practice.
        try:
            text = str(text)
        except Exception:  # noqa: BLE001 — last-resort defense
            return "", []

    found_types: list[str] = []
    seen: set[str] = set()

    patterns: Iterable[tuple[str, Any]] = _PATTERNS
    if include_ip:
        patterns = list(_PATTERNS) + [_IPV4_PATTERN]

    for kind, fn in patterns:
        try:
            new_text, hit = fn(text)
        except Exception:  # noqa: BLE001 — a failing pattern must not be fatal
            # A misbehaving regex (or pathological input) is skipped, not
            # raised. The remaining patterns still run.
            continue
        if hit and kind not in seen:
            seen.add(kind)
            found_types.append(kind)
        text = new_text

    return text, found_types


# Dict keys whose VALUE should be redacted regardless of what the value
# itself looks like. This is the recursive-container counterpart to the
# inline _RE_PASSWORD regex: when the secret is *only* identified by the
# enclosing dict key (e.g. {"password": "p@ss"}) the string scrubber has
# no in-band keyword to anchor on, so we anchor on the key instead.
_SENSITIVE_KEY_RX = re.compile(
    r"(?i)(password|passwd|pwd|secret|api[_\-]?key|access[_\-]?token|auth[_\-]?token|authorization)"
)


def scrub_dict(
    payload: Any, *, include_ip: bool = False
) -> tuple[Any, list[str]]:
    """Recursively scrub every string in a JSON-like structure.

    Walks ``dict``, ``list``, and ``tuple`` containers and applies
    :func:`scrub` to every string leaf. Non-string leaves
    (numbers, booleans, ``None``) are passed through untouched.

    In addition to in-band regex matching, this function applies a
    structural rule: when a dict key is itself password-like
    (``password``, ``api_key``, ``authorization``, etc.) the
    corresponding string value is redacted *as a whole* even if its
    content has no recognizable secret shape. This catches secrets that
    only become identifiable from their surrounding context (e.g.
    ``{"password": "hunter2"}`` — "hunter2" alone is just a word).

    Returns ``(scrubbed_payload, sorted_unique_redaction_types)``. The
    payload structure is preserved; only string values change.

    Like :func:`scrub`, this function never raises. If recursion hits
    something it can't handle (e.g. a circular reference), that node is
    returned as-is and the walk continues elsewhere.
    """
    found: set[str] = set()

    def _classify_key(key: Any) -> str | None:
        """Return the redaction type for a sensitive-looking dict key."""
        if not isinstance(key, str):
            return None
        m = _SENSITIVE_KEY_RX.search(key)
        if not m:
            return None
        hit = m.group(1).lower()
        if "password" in hit or "passwd" in hit or "pwd" in hit or "secret" in hit:
            return "password"
        if "authorization" in hit:
            return "bearer_token"
        # api_key / access_token / auth_token all roll up to api_key.
        return "api_key"

    def _walk(node: Any, depth: int = 0, key_hint: str | None = None) -> Any:
        # Bail out of pathological deep structures rather than blow the
        # stack. JSON sent over a webhook is in practice very shallow.
        if depth > 64:
            return node

        try:
            if isinstance(node, str):
                if key_hint and node:
                    # Whole-value redaction driven by the parent key.
                    found.add(key_hint)
                    return _ph(key_hint)
                cleaned, hits = scrub(node, include_ip=include_ip)
                for h in hits:
                    found.add(h)
                return cleaned
            if isinstance(node, dict):
                out: dict[Any, Any] = {}
                for k, v in node.items():
                    out[k] = _walk(v, depth + 1, _classify_key(k))
                return out
            if isinstance(node, list):
                return [_walk(v, depth + 1) for v in node]
            if isinstance(node, tuple):
                return tuple(_walk(v, depth + 1) for v in node)
        except Exception:  # noqa: BLE001 — never propagate
            return node
        return node

    try:
        scrubbed = _walk(payload)
    except Exception:  # noqa: BLE001 — absolute last resort
        return payload, []

    return scrubbed, sorted(found)


__all__ = ["scrub", "scrub_dict"]
