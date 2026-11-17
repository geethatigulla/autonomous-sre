"""Tests for the outbound PII / secret scrubber (victim_app.scrubber).

These tests are deliberately conservative: the scrubber's contract is
"redact obvious secrets, don't mangle normal text, never raise." The
tests pin both halves of that contract — we check that real secrets
are gone, AND that structural keywords (key names, "File", line
numbers, exception class) survive.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `victim_app` importable when pytest is invoked from the repo root
# without an editable install. Mirrors what the other tests do.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from victim_app.scrubber import scrub, scrub_dict


# ---------------------------------------------------------------------------
# scrub() — string-level patterns
# ---------------------------------------------------------------------------


def test_scrubs_openai_key():
    raw_key = "sk-proj-abc123ABC456DEF789ghi012JKL345"
    text = f"OpenAI request failed with key={raw_key} :("
    cleaned, kinds = scrub(text)
    assert raw_key not in cleaned
    assert "[REDACTED:api_key]" in cleaned
    assert "api_key" in kinds


def test_scrubs_github_token():
    raw_key = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    text = f"git push failed: token={raw_key}"
    cleaned, kinds = scrub(text)
    assert raw_key not in cleaned
    assert "[REDACTED:api_key]" in cleaned
    assert "api_key" in kinds


def test_scrubs_password_in_json():
    raw = '{"username": "alice", "password": "hunter2"}'
    cleaned, kinds = scrub(raw)
    # Key visible, value gone.
    assert '"password"' in cleaned
    assert "hunter2" not in cleaned
    assert "[REDACTED:password]" in cleaned
    assert "password" in kinds
    # And we didn't blow away the unrelated field.
    assert "alice" in cleaned


def test_scrubs_email():
    text = "user contacted us from alice@example.com about a 500"
    cleaned, kinds = scrub(text)
    assert "alice@example.com" not in cleaned
    assert "[REDACTED:email]" in cleaned
    assert "email" in kinds


def test_scrubs_bearer_token():
    # Use a realistic JWT-ish token to exercise the bearer matcher.
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abc-DEF_123"
    text = f"Authorization: Bearer {jwt}"
    cleaned, kinds = scrub(text)
    assert jwt not in cleaned
    # "Bearer " marker is preserved so the trace still tells you what
    # kind of credential was redacted.
    assert "Bearer" in cleaned
    assert "[REDACTED:bearer_token]" in cleaned
    assert "bearer_token" in kinds


def test_credit_card_luhn_valid_redacted():
    # 4111 1111 1111 1111 is the canonical Visa test card — Luhn-valid.
    text = "charging card 4111111111111111 for $9.99"
    cleaned, kinds = scrub(text)
    assert "4111111111111111" not in cleaned
    assert "[REDACTED:credit_card]" in cleaned
    assert "credit_card" in kinds


def test_credit_card_luhn_invalid_not_redacted():
    # A 16-digit run that FAILS Luhn — must survive. This catches
    # false positives on order IDs, timestamps, etc.
    bogus = "1234567890123456"
    text = f"order id {bogus} created at t=...."
    cleaned, kinds = scrub(text)
    assert bogus in cleaned, "Non-Luhn digit run should NOT be redacted"
    assert "credit_card" not in kinds


# ---------------------------------------------------------------------------
# scrub_dict() — recursive container walk
# ---------------------------------------------------------------------------


def test_scrub_dict_recursive():
    payload = {
        "outer_secret": "sk-abcdefghijklmnopqrstuv",
        "nested": {
            "headers": {
                "Authorization": "Bearer eyJabcdef.eyJghijkl.MNOpqr-stu",
            },
            "user": {
                "email": "bob@example.org",
                "password": "p@ssw0rd",
            },
        },
        "list_field": [
            "contact me at carol@example.com",
            {"api_key": "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        ],
        "harmless_int": 7,
        "harmless_bool": True,
        "harmless_none": None,
    }

    cleaned, kinds = scrub_dict(payload)

    # Everything sensitive is gone.
    serialized = repr(cleaned)
    assert "sk-abcdefghijklmnopqrstuv" not in serialized
    assert "eyJabcdef.eyJghijkl.MNOpqr-stu" not in serialized
    assert "bob@example.org" not in serialized
    assert "carol@example.com" not in serialized
    assert "p@ssw0rd" not in serialized
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in serialized

    # Structure preserved.
    assert cleaned["harmless_int"] == 7
    assert cleaned["harmless_bool"] is True
    assert cleaned["harmless_none"] is None
    assert isinstance(cleaned["list_field"], list)
    assert isinstance(cleaned["nested"]["user"], dict)

    # Redaction types collected and sorted / unique.
    assert kinds == sorted(set(kinds))
    for expected in ("api_key", "bearer_token", "email", "password"):
        assert expected in kinds


def test_scrubber_never_raises():
    # Garbage soup — bytes, very long strings, weird unicode, deeply
    # nested junk. The function must return cleanly for every case.

    # None / empty
    assert scrub(None) == ("", [])
    assert scrub("") == ("", [])

    # Bytes-like (the scrubber coerces to str via repr; the point is
    # that it must NOT raise on the caller).
    cleaned, _ = scrub(b"\x00\x01\x02 some bytes")  # type: ignore[arg-type]
    assert isinstance(cleaned, str)

    # Very long string
    long_text = "x" * 250_000 + " email me at a@b.co"
    cleaned, kinds = scrub(long_text)
    assert isinstance(cleaned, str)
    assert "email" in kinds

    # Weird unicode + surrogate-ish things
    cleaned, _ = scrub("héllo wörld 🤖 ☃ \ud83d")
    assert isinstance(cleaned, str)

    # scrub_dict on garbage
    out, kinds = scrub_dict({"a": None, "b": 1, "c": [None, 1, "ok"]})
    assert out["a"] is None and out["b"] == 1
    assert isinstance(out["c"], list)

    # Non-dict input shouldn't crash scrub_dict either — it should
    # walk it as best it can and return *something*.
    out, kinds = scrub_dict("just a string with alice@example.com")
    # When the top-level is a string scrub_dict returns the scrubbed
    # string itself (the function is best-effort, not type-strict).
    assert "alice@example.com" not in repr(out)


def test_traceback_still_readable():
    # A realistic-looking Python traceback. After scrubbing, the
    # File/line/exception structure must survive — only the email
    # should disappear. This is the "developer can still debug"
    # contract.
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/victim_app/app.py", line 42, in shorten\n'
        '    user = lookup(request.json["email"])\n'
        '  File "/app/victim_app/users.py", line 11, in lookup\n'
        '    raise ValueError(f"no user for alice@example.com")\n'
        "ValueError: no user for alice@example.com\n"
    )
    cleaned, kinds = scrub(tb)

    assert "alice@example.com" not in cleaned
    assert "[REDACTED:email]" in cleaned
    assert "email" in kinds

    # Structural anchors all survive — these are what a human (or an
    # LLM agent) needs to reason about the crash.
    assert "Traceback (most recent call last):" in cleaned
    assert "File " in cleaned
    assert "line 42" in cleaned
    assert "line 11" in cleaned
    assert "in shorten" in cleaned
    assert "in lookup" in cleaned
    assert "ValueError" in cleaned
    # Only one redaction type fired — the traceback shape is intact.
    assert kinds == ["email"]


# ---------------------------------------------------------------------------
# Bonus: a couple of specificity / ordering guardrails. These aren't in
# the spec list but they're cheap to keep and they pin the contract
# from the module docstring ("specific patterns win over greedy ones").
# ---------------------------------------------------------------------------


def test_private_key_block_redacted():
    pk = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAKj...veryFakeKey...IDAQAB\n"
        "-----END RSA PRIVATE KEY-----"
    )
    cleaned, kinds = scrub(pk)
    assert "veryFakeKey" not in cleaned
    assert "[REDACTED:private_key]" in cleaned
    assert "private_key" in kinds


def test_ipv4_off_by_default_on_when_requested():
    text = "request came from 10.20.30.40"
    cleaned, kinds = scrub(text)
    # Off by default — IPs in stack traces are useful for debugging.
    assert "10.20.30.40" in cleaned
    assert "ip_address" not in kinds

    cleaned2, kinds2 = scrub(text, include_ip=True)
    assert "10.20.30.40" not in cleaned2
    assert "[REDACTED:ip_address]" in cleaned2
    assert "ip_address" in kinds2
