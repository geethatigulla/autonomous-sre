#!/usr/bin/env bash
# End-to-end smoke test: orchestrator + victim_app + webhook + Triager.
#
# Boots the orchestrator on localhost:8000, optionally boots the victim
# on localhost:9000 (skipped when the bug is config_error, which makes
# the victim crash on startup), fires a deliberately-broken request,
# then polls the orchestrator until the pipeline reaches a terminal
# state. Prints a one-page summary and tears everything down on exit.
#
# Usage: bash scripts/smoke_test.sh [null_deref|regression|config_error]
# Default: null_deref
#
# Idempotent: any leftover process bound to :8000 or :9000 is killed
# before starting. Safe to re-run after a crash.

set -u  # do NOT use -e — we want to print the summary even if curl/jq
        # has hiccups mid-run.

BUG=${1:-null_deref}
echo "🐛 Running smoke test for: $BUG"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ORCH_PORT=8000
VICTIM_PORT=9000
ORCH_LOG="$(mktemp -t autonomous-sre-orch.XXXXXX.log)"
VICTIM_LOG="$(mktemp -t autonomous-sre-victim.XXXXXX.log)"

# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

_kill_port() {
    local port="$1"
    # Use lsof when available (mac + most Linux), fall back to fuser.
    if command -v lsof >/dev/null 2>&1; then
        local pids
        pids="$(lsof -ti :"$port" 2>/dev/null || true)"
        if [ -n "$pids" ]; then
            kill -9 $pids 2>/dev/null || true
        fi
    elif command -v fuser >/dev/null 2>&1; then
        fuser -k "$port"/tcp 2>/dev/null || true
    fi
}

cleanup() {
    if [ -n "${ORCH_PID:-}" ]; then
        kill "$ORCH_PID" 2>/dev/null || true
    fi
    if [ -n "${VICTIM_PID:-}" ]; then
        kill "$VICTIM_PID" 2>/dev/null || true
    fi
    # Belt-and-suspenders: anything still listening on the ports goes.
    _kill_port "$ORCH_PORT"
    _kill_port "$VICTIM_PORT"
}
trap cleanup EXIT INT TERM

# Pre-flight: clear stale processes so the script is idempotent.
_kill_port "$ORCH_PORT"
_kill_port "$VICTIM_PORT"
sleep 1

# Pre-flight: remove any sre.db in the repo root. The smoke test uses a
# throwaway $TMPDIR DB, but if the repo root contains a stale sre.db
# from a previous interactive run with an older schema, any subsequent
# `rm -f sre.db && uvicorn ...` invocation (e.g. by a developer) would
# otherwise pick that file up. Removing it here keeps the schema fresh
# for both modes.
rm -f "$ROOT/sre.db"

# ---------------------------------------------------------------------------
# Map the user-facing bug name to the victim_app's BUG_MODE env value
# and to the classification we expect to see on the resulting incident.
# ---------------------------------------------------------------------------
case "$BUG" in
    null_deref)
        VICTIM_BUG_MODE=null_deref
        EXPECTED_CLASS=null_deref
        ;;
    regression|recent_regression)
        VICTIM_BUG_MODE=regression
        EXPECTED_CLASS=recent_regression
        ;;
    config_error)
        VICTIM_BUG_MODE=config_error
        EXPECTED_CLASS=config_error
        ;;
    *)
        echo "❌ Unknown bug mode: $BUG (expected null_deref|regression|config_error)"
        exit 2
        ;;
esac

# ---------------------------------------------------------------------------
# Start orchestrator
# ---------------------------------------------------------------------------

# Use a throwaway SQLite DB so reruns don't drown in old incidents.
# Living under $TMPDIR (or /tmp) keeps the workspace clean and avoids
# odd filesystem semantics on mounted volumes that don't support
# SQLite locking.
SMOKE_DB="$(mktemp -t autonomous-sre-smoke.XXXXXX.db)"
rm -f "$SMOKE_DB"

DATABASE_URL="sqlite:///$SMOKE_DB" \
PYTHONPATH="$ROOT" \
    "$ROOT/.venv/bin/uvicorn" orchestrator.main:app \
        --host 127.0.0.1 --port "$ORCH_PORT" \
        --log-level info \
        > "$ORCH_LOG" 2>&1 &
ORCH_PID=$!

# ---------------------------------------------------------------------------
# Wait for orchestrator to come up
# ---------------------------------------------------------------------------

_wait_for() {
    local url="$1"
    local label="$2"
    local tries=0
    while [ "$tries" -lt 30 ]; do
        if curl -sf "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.3
        tries=$((tries + 1))
    done
    echo "❌ $label did not start in time"
    echo "----- $label log -----"
    if [ "$label" = "Orchestrator" ]; then
        tail -n 40 "$ORCH_LOG"
    else
        tail -n 40 "$VICTIM_LOG"
    fi
    return 1
}

if _wait_for "http://127.0.0.1:$ORCH_PORT/health" "Orchestrator"; then
    echo "✅ Orchestrator started on :$ORCH_PORT"
else
    exit 1
fi

# ---------------------------------------------------------------------------
# Per-bug trigger logic
# ---------------------------------------------------------------------------
# config_error: don't start victim_app at all (it would crash on
# startup). Instead, POST a synthetic alert directly to /webhooks/alert
# describing the startup-time KeyError.
# null_deref / regression: start victim_app and fire a real bad request.
#
# All branches end with the incident already POSTed and an alert
# response ready to inspect.

if [ "$BUG" = "config_error" ]; then
    echo "⚡ Posting synthetic startup-failure alert (victim_app won't boot under config_error)..."
    curl -s -o /dev/null -w "   orchestrator HTTP status: %{http_code}\n" \
        -X POST "http://127.0.0.1:$ORCH_PORT/webhooks/alert" \
        -H 'content-type: application/json' \
        -d '{
            "endpoint": "STARTUP",
            "method": "STARTUP",
            "exception_class": "KeyError",
            "exception_message": "REDIS_URL",
            "traceback": "Traceback (most recent call last):\n  File \"victim_app/app.py\", line 39, in <module>\n    REDIS_URL = os.environ[\"REDIS_URL\"]\n  File \"/usr/lib/python3.11/os.py\", line 685, in __getitem__\n    raise KeyError(key) from None\nKeyError: '"'"'REDIS_URL'"'"'",
            "request_body": "{}",
            "source_service": "victim_app"
        }'
else
    BUG_MODE="$VICTIM_BUG_MODE" \
    ORCHESTRATOR_WEBHOOK_URL="http://127.0.0.1:$ORCH_PORT/webhooks/alert" \
    PYTHONPATH="$ROOT/victim_app" \
        "$ROOT/.venv/bin/uvicorn" app:app \
            --host 127.0.0.1 --port "$VICTIM_PORT" \
            --app-dir "$ROOT/victim_app" \
            --log-level warning \
            > "$VICTIM_LOG" 2>&1 &
    VICTIM_PID=$!

    if _wait_for "http://127.0.0.1:$VICTIM_PORT/healthz" "Victim app"; then
        echo "✅ Victim app started on :$VICTIM_PORT (BUG_MODE=$VICTIM_BUG_MODE)"
    else
        exit 1
    fi

    if [ "$BUG" = "null_deref" ]; then
        echo "⚡ Sending bad request to victim app (null url)..."
        curl -s -o /dev/null -w "   victim_app HTTP status: %{http_code}\n" \
            -X POST "http://127.0.0.1:$VICTIM_PORT/shorten" \
            -H 'content-type: application/json' \
            -d '{"url": null}'
    else
        # regression: /shorten succeeds (returns wrong key shape), then
        # /expand crashes with KeyError when it reads the renamed field.
        echo "⚡ Sending valid /shorten request to victim app..."
        SHORTEN_RESP=$(curl -s -X POST "http://127.0.0.1:$VICTIM_PORT/shorten" \
            -H 'content-type: application/json' \
            -d '{"url": "https://example.com/regression"}')
        echo "   shorten response: $SHORTEN_RESP"
        # The buggy /shorten returns {"result": "...", "original": ...}.
        # We need a real short code to feed /expand. Extract whichever
        # key we can find ("short" if the bug is fixed, "result" if not).
        SHORT_CODE=$(echo "$SHORTEN_RESP" | .venv/bin/python -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("short") or d.get("result") or "")
except Exception:
    print("")
')
        if [ -z "$SHORT_CODE" ]; then
            echo "   (could not parse short code from /shorten — using placeholder)"
            SHORT_CODE="deadbe"
        fi
        echo "⚡ Sending /expand/$SHORT_CODE to trigger the regression crash..."
        curl -s -o /dev/null -w "   victim_app HTTP status: %{http_code}\n" \
            "http://127.0.0.1:$VICTIM_PORT/expand/$SHORT_CODE"
    fi
fi

echo "⏳ Waiting for triage..."
sleep 10

RESPONSE=$(curl -s "http://127.0.0.1:$ORCH_PORT/webhooks/alert")

echo ""
echo "📋 Latest incident:"
echo "$RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
if not data:
    print("  (no incidents)")
    sys.exit(1)
i = data[0] if isinstance(data, list) else data
fields = ["id", "exception_class", "exception_message", "classification", "severity", "triager_confidence", "status", "workflow_id"]
for f in fields:
    v = i.get(f)
    if v is None:
        v = "<null>"
    elif isinstance(v, str) and len(v) > 80:
        v = v[:77] + "..."
    print("  " + f + ": " + str(v))
reasoning = i.get("triager_reasoning") or ""
if reasoning:
    print("  reasoning: " + reasoning[:200])
'

echo ""
echo "🔎 Validating..."
VALIDATION=$(echo "$RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
if not data:
    print("FAIL: no incidents")
    sys.exit(1)
i = data[0] if isinstance(data, list) else data
ok_status = i.get("status") in ("classified", "pending_implementation", "investigating", "investigated", "pending_fix", "escalated", "fix_drafted", "fix_failed", "verified", "fix_rejected", "verification_failed", "pending_release", "released", "release_failed", "investigation_failed")
ok_class = i.get("classification") is not None and i.get("classification") != ""
ok_sev = i.get("severity") is not None
if ok_status and ok_class and ok_sev:
    print("PASS: classification=" + str(i.get("classification")) + ", severity=" + str(i.get("severity")) + ", status=" + str(i.get("status")))
else:
    print("FAIL: status=" + str(i.get("status")) + ", classification=" + str(i.get("classification")) + ", severity=" + str(i.get("severity")))
    sys.exit(1)
')
VALIDATION_EXIT=$?

echo "$VALIDATION"

if [ $VALIDATION_EXIT -ne 0 ]; then
    echo "❌ Smoke test FAILED at triage validation."
    exit 1
fi

# ---------------------------------------------------------------------------
# Investigation poll — wait up to 60s for status to advance past triage.
# ---------------------------------------------------------------------------

INCIDENT_ID=$(echo "$RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
if not data:
    sys.exit(1)
i = data[0] if isinstance(data, list) else data
print(i.get("id", ""))
')

if [ -z "$INCIDENT_ID" ]; then
    echo "❌ Could not extract incident id from response."
    exit 1
fi

echo ""
echo "⏳ Waiting for investigation to complete (up to 60s)..."
INV_RESPONSE=""
INV_STATUS=""
# The pipeline flows investigated → fix_drafted →
# verified/fix_rejected/verification_failed → pending_release in one
# async task, so we accept any of those as "investigation finished".
for i in $(seq 1 30); do
    INV_RESPONSE=$(curl -s "http://127.0.0.1:$ORCH_PORT/webhooks/alert/$INCIDENT_ID")
    INV_STATUS=$(echo "$INV_RESPONSE" | .venv/bin/python -c '
import sys, json
try:
    data = json.load(sys.stdin)
    i = data.get("incident") if isinstance(data, dict) else None
    if not i:
        print("")
    else:
        print(i.get("status", ""))
except Exception:
    print("")
')
    case "$INV_STATUS" in
        investigated|fix_drafted|fix_failed|verified|fix_rejected|verification_failed|pending_release|investigation_failed|released|release_failed)
            break
            ;;
    esac
    sleep 2
done

echo ""
echo "🔍 Investigation:"
echo "$INV_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
i = data.get("incident") if isinstance(data, dict) else {}
if not i:
    print("  (no incident detail)")
    sys.exit(0)
sf = i.get("suspect_file") or "<null>"
sl = i.get("suspect_line")
rc = i.get("root_cause") or ""
status = i.get("status") or ""
print("   status: " + str(status))
print("   classification: " + str(i.get("classification") or "<null>"))
print("   suspect_file: " + str(sf))
print("   suspect_line: " + str(sl if sl is not None else "<null>"))
print("   root_cause: " + (rc[:200] + ("..." if len(rc) > 200 else "")))
raw = i.get("investigation_result") or ""
top_desc = ""
top_strategy = ""
top_conf = None
try:
    parsed = json.loads(raw) if raw else {}
    hyps = parsed.get("hypotheses") or []
    if hyps:
        h = hyps[0]
        top_desc = h.get("description") or ""
        top_strategy = h.get("fix_strategy") or ""
        top_conf = h.get("confidence")
except Exception:
    pass
print("   top hypothesis: " + top_desc[:120] + " | strategy: " + top_strategy[:120] + " | confidence: " + str(top_conf))
'

if [ "$INV_STATUS" = "investigation_failed" ]; then
    echo ""
    echo "❌ Investigation failed. AgentRun error_message:"
    echo "$INV_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
runs = data.get("agent_runs") or []
inv = [r for r in runs if r.get("agent_name") == "investigator"]
if not inv:
    print("   (no investigator AgentRun found)")
    sys.exit(0)
err = (inv[-1].get("error_message") or "")[-500:]
print("   " + err)
'
    exit 1
fi

# Did investigation produce the expected scalars?
INV_OK=$(echo "$INV_RESPONSE" | .venv/bin/python -c '
import os, sys, json
data = json.load(sys.stdin)
i = data.get("incident") if isinstance(data, dict) else {}
if not i:
    print("FAIL: no incident detail")
    sys.exit(1)
sf = i.get("suspect_file") or ""
raw = i.get("investigation_result") or ""
hyps = []
try:
    parsed = json.loads(raw) if raw else {}
    hyps = parsed.get("hypotheses") or []
except Exception:
    pass
top_h = hyps[0] if hyps else {}
if not sf:
    conf = top_h.get("confidence", 0)
    repo = os.environ.get("GITHUB_REPO") or "<unset>"
    print("FAIL: Investigator did not resolve suspect_file. This usually")
    print("       means GitHub read failed for all candidate paths.")
    print("  Top hypothesis confidence: " + str(conf))
    print("  Hint: check that repo " + repo + " has the suspect file at a")
    print("         discoverable path (basename match is enough — the")
    print("         GitHub tool will walk the tree to find it).")
    sys.exit(1)
if not hyps:
    print("FAIL: suspect_file=" + repr(sf) + " hypotheses=0")
    sys.exit(1)
print("OK: suspect_file=" + sf + " hypotheses=" + str(len(hyps)))
')
INV_OK_EXIT=$?

if [ $INV_OK_EXIT -ne 0 ]; then
    echo ""
    echo "❌ Investigation block missing required fields: $INV_OK"
    exit 1
fi

# ---------------------------------------------------------------------------
# Fix Author + Verifier poll — wait up to 90s for the pipeline to reach a
# terminal post-verification state.
# ---------------------------------------------------------------------------

echo ""
echo "⏳ Waiting for fix author + verifier (up to 90s)..."
VER_RESPONSE=""
VER_STATUS=""
for i in $(seq 1 45); do
    VER_RESPONSE=$(curl -s "http://127.0.0.1:$ORCH_PORT/webhooks/alert/$INCIDENT_ID")
    VER_STATUS=$(echo "$VER_RESPONSE" | .venv/bin/python -c '
import sys, json
try:
    data = json.load(sys.stdin)
    i = data.get("incident") if isinstance(data, dict) else None
    if not i:
        print("")
    else:
        print(i.get("status", ""))
except Exception:
    print("")
')
    case "$VER_STATUS" in
        released|release_failed|pending_release|fix_rejected|verification_failed|fix_failed)
            break
            ;;
    esac
    sleep 2
done

echo ""
echo "📝 Proposed Fix:"
echo "$VER_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
i = data.get("incident") if isinstance(data, dict) else {}
if not i:
    print("   (no incident detail)")
    sys.exit(0)
sf = i.get("suspect_file") or "<null>"
risk = i.get("fix_risk_level") or "<null>"
conf = i.get("fix_confidence")
conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else str(conf)
expl = (i.get("fix_explanation") or "")
diff = (i.get("proposed_patch") or "")
print("   file: " + str(sf))
print("   risk: " + str(risk) + ", confidence: " + conf_str)
print("   explanation: " + (expl[:200] + ("..." if len(expl) > 200 else "")))
print("   diff preview (first 20 lines):")
for ln in (diff.splitlines()[:20] if diff else ["<no diff>"]):
    print("     " + ln)
'

echo ""
echo "🏁 Race Summary:"
echo "$VER_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
i = data.get("incident") if isinstance(data, dict) else {}
raw = i.get("verification_result") or ""
ver = {}
try:
    ver = json.loads(raw) if raw else {}
except Exception:
    pass
winning = ver.get("winning_strategy") or "<unknown>"
candidates = ver.get("all_candidates") or []
verdict_parts = []
for c in candidates:
    strat = c.get("strategy") or "?"
    v = c.get("verdict") or "?"
    verdict_parts.append(strat + "=" + str(v))
winner_dur = ver.get("duration_seconds")
print("   winning strategy: " + str(winning))
print("   candidates: " + str(len(candidates)))
print("   verdicts: " + (" | ".join(verdict_parts) if verdict_parts else "<none>"))
if isinstance(winner_dur, (int, float)):
    print("   winner duration: " + (str(round(winner_dur, 3)) + "s"))
else:
    print("   winner duration: <none>")
'

echo ""
echo "🧪 Verification:"
echo "$VER_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
i = data.get("incident") if isinstance(data, dict) else {}
if not i:
    print("   (no incident detail)")
    sys.exit(0)
raw = i.get("verification_result") or ""
ver = {}
try:
    ver = json.loads(raw) if raw else {}
except Exception:
    pass
verdict = ver.get("verdict") or "<none>"
tests_passed = ver.get("tests_passed")
dur = ver.get("duration_seconds")
print("   verdict: " + str(verdict))
print("   tests_passed: " + str(tests_passed))
print("   duration: " + (str(dur) + "s" if dur is not None else "<none>"))
'

VER_VERDICT=$(echo "$VER_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
i = data.get("incident") if isinstance(data, dict) else {}
raw = i.get("verification_result") or ""
ver = {}
try:
    ver = json.loads(raw) if raw else {}
except Exception:
    pass
print(ver.get("verdict") or "")
')

if [ "$VER_VERDICT" != "verified" ]; then
    echo ""
    echo "❌ Smoke test FAILED at verification with verdict=$VER_VERDICT, status=$VER_STATUS."
    # Surface the AgentRun error_message of whichever agent stumbled
    # so the operator doesn't have to grep the orchestrator log.
    echo "$VER_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
runs = data.get("agent_runs") or []
# After the fix race, AgentRun names are strategy-suffixed
# (fix_author_defensive, verifier_permissive, etc.). Surface
# every fix_author_* / verifier_* row so the operator can see
# which strategy stumbled.
for prefix in ("fix_author_", "verifier_"):
    matching = [r for r in runs if (r.get("agent_name") or "").startswith(prefix)]
    if not matching:
        print(f"   no {prefix}* AgentRun found")
        continue
    for row in matching:
        name = row.get("agent_name")
        err = (row.get("error_message") or "")[-500:]
        if err:
            print(f"   {name} error: {err}")
        else:
            st = row.get("status")
            print(f"   {name} status: {st}")
'
    exit 1
fi

# ---------------------------------------------------------------------------
# Releaser poll — wait up to 60s for status to advance to released or
# release_failed. The Releaser opens a real GitHub PR and (optionally)
# posts a Slack notification, which can take a couple of seconds.
# ---------------------------------------------------------------------------

echo ""
echo "⏳ Waiting for release (up to 60s)..."
REL_RESPONSE="$VER_RESPONSE"
REL_STATUS="$VER_STATUS"
for i in $(seq 1 30); do
    REL_RESPONSE=$(curl -s "http://127.0.0.1:$ORCH_PORT/webhooks/alert/$INCIDENT_ID")
    REL_STATUS=$(echo "$REL_RESPONSE" | .venv/bin/python -c '
import sys, json
try:
    data = json.load(sys.stdin)
    i = data.get("incident") if isinstance(data, dict) else None
    if not i:
        print("")
    else:
        print(i.get("status", ""))
except Exception:
    print("")
')
    case "$REL_STATUS" in
        released|release_failed)
            break
            ;;
    esac
    sleep 2
done

echo ""
echo "📤 Release:"
echo "$REL_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
i = data.get("incident") if isinstance(data, dict) else {}
if not i:
    print("   (no incident detail)")
    sys.exit(0)
print("   status: " + str(i.get("status") or "<null>"))
print("   classification: " + str(i.get("classification") or "<null>"))
print("   PR URL: " + str(i.get("pr_url") or "failed"))
print("   branch: " + str(i.get("pr_branch") or "<null>"))
print("   Slack posted: " + str(i.get("slack_posted")))
'

PR_URL=$(echo "$REL_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
i = data.get("incident") if isinstance(data, dict) else {}
print(i.get("pr_url") or "")
')

if [ "$REL_STATUS" = "released" ] && [ -n "$PR_URL" ] && [[ "$PR_URL" == https://github.com/* ]]; then
    echo ""
    echo "✅ Smoke test passed for $BUG (full pipeline through release)."
    exit 0
else
    echo ""
    echo "❌ Smoke test FAILED at release with status=$REL_STATUS, pr_url=$PR_URL."
    echo "$REL_RESPONSE" | .venv/bin/python -c '
import sys, json
data = json.load(sys.stdin)
runs = data.get("agent_runs") or []
rel = [r for r in runs if r.get("agent_name") == "releaser"]
if not rel:
    print("   no releaser AgentRun found")
else:
    last = rel[-1]
    err = (last.get("error_message") or "")[-1000:]
    if err:
        print("   releaser error:")
        for line in err.splitlines():
            print("     " + line)
    else:
        print("   releaser status: " + str(last.get("status")))
'
    exit 1
fi
