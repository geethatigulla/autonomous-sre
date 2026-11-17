# Demo Runbook

Operator checklist for recording the autonomous-sre demo video. Print this
or keep it open on a second monitor — every step the presenter runs in front
of the camera is below.

The entire recorded flow is driven from one script: `scripts/demo.py`. There
are no curl commands, no environment juggling, no manual GitHub clicks.

---

## 1. Pre-flight (5 minutes before recording)

### 1.1 Repo is clean

```bash
cd autonomous-sre
git status            # must be clean, on the demo branch
git pull --ff-only
```

If `git status` shows changes, stash or commit them — a dirty tree
distracts judges who notice it in the terminal.

### 1.2 Environment variables are loaded

The orchestrator reads from `.env` at the repo root. Required keys:

| Key                   | Required | Purpose                                          |
|-----------------------|----------|--------------------------------------------------|
| `OPENAI_API_KEY`      | yes      | powers every agent                               |
| `GITHUB_TOKEN`        | yes      | reads code, opens / closes / deletes PRs         |
| `GITHUB_REPO`         | yes      | `owner/repo` the agents touch                    |
| `SLACK_WEBHOOK_URL`   | optional | recommended — shows live notifications on demo   |
| `OMIUM_API_KEY`       | optional | nicer traces; falls back to local JSONL          |
| `TAVILY_API_KEY`      | optional | web-search tool inside the Investigator          |

Quick verification:

```bash
python scripts/demo.py --status
```

You should see all four "API Keys" rows with green `✓ set`. Optional rows
in yellow `– optional` are fine.

### 1.3 Services can start

```bash
python scripts/demo.py --status
```

If the orchestrator is already running, it shows `● UP`. If not, that's
fine for now — we'll start it during the recording. The victim app showing
"ready (auto-started on trigger)" is the expected state between takes.

### 1.4 Clean slate (between takes — and right before recording)

```bash
python scripts/demo.py --reset
```

This:

- Deletes `sre.db` (orchestrator recreates tables on next boot)
- Closes every open PR whose branch starts with `autosre/`
- Deletes those branches
- Pings Slack (if configured) with "Demo environment reset"
- Prints a one-line summary: `✅ Clean slate complete — DB wiped, N PR(s) closed, M branch(es) deleted.`

Run this **between every take**. The demo video depends on the dashboard
starting from zero.

### 1.5 Browser primed for the opening shot

1. Open `http://127.0.0.1:8000/` after starting the orchestrator (step 2.1
   below — you'll do this on camera, but you can dry-run it once).
2. Confirm the empty state looks right:
   - Metrics row: `0`, `0%`, `0s`, `0`
   - "Current Incident" card: a small circle glyph and the message
     **Awaiting incidents — system idle**, with the hint underneath
   - "Agent Pipeline" panel: empty rail
   - "Live Thoughts" panel: empty terminal-style box, "Live" indicator green
   - "Recent Incidents" sidebar: **No incidents yet**
   - "Trigger Incident" button visible top-right (we'll use the CLI, not
     this button — but it should be there)
3. Close DevTools, hide bookmarks bar, full-screen the browser.

---

## 2. The recording — exact command sequence

The whole recording is two commands. That's the point.

### 2.1 Open the menu and start the system

```bash
python scripts/demo.py
```

This shows the menu. On camera, type:

```
1      ← Start orchestrator + dashboard (boots in ~5s)
3      ← Status check (this is the screenshot moment — all green ✓)
10     ← Opens the dashboard in your browser (or alt-tab to it)
```

Cut to dashboard for a 3-second beat of the calm empty state.
Cut back to terminal.

Type `q` to exit the menu (keep the orchestrator running by answering "no"
to the "stop services?" prompt).

### 2.2 Run the scripted sequence

```bash
python scripts/demo.py --demo-sequence
```

This runs all three bug types end-to-end with zero further input:

1. `null_deref` triggered → polled to `released` → summary printed
2. 3-second pause
3. `regression` triggered → polled to `released` → summary printed
4. 3-second pause
5. `config_error` triggered → polled to `released` → summary printed
6. **Final summary table**: 3 incidents, 3 classifications, 3 PR URLs, total
   elapsed time, followed by a plain-text PR URL listing for easy reading.

During the run, alt-tab to the dashboard — you'll watch each pipeline
animate in real time, PRs open, and Slack notifications fire. The terminal
keeps a one-line stage indicator updating live so you can cut between the
two surfaces freely.

### 2.3 Closing shot

Cut back to the terminal showing the final summary table with three green
`released` rows. That's the end of the recording.

---

## 3. Expected timings per bug type

These are average wall-clock durations from trigger to `released`, measured
on a typical demo run. The `--demo-sequence` poller times out at 4 minutes
per bug.

| Bug          | Typical time | Notes                                          |
|--------------|--------------|------------------------------------------------|
| `null_deref` | 60–90s       | shortest — fix is small and verification fast  |
| `regression` | 75–120s      | medium — Investigator reads recent commits     |
| `config_error` | 60–100s    | medium — synthetic alert, no victim_app boot   |
| **Total sequence** | **~4–6 min** | includes two 3-second pauses        |

If a bug exceeds ~3 minutes, see Recovery (section 5).

---

## 4. What the presenter should call out

For the voiceover (or live narration), the beats are:

1. "Status check — all green. Let's go." (opt [3] output)
2. "Empty dashboard. No incidents. The system is idle." (opening shot)
3. "I'm going to fire three bugs in sequence — null_deref, regression,
   config_error — completely hands-off."
4. As each pipeline animates: "Triager → Investigator → Implementer →
   Verifier → Releaser." Mention the actual classification + suspect file
   from the live summary.
5. "Three PRs, three merges, no humans in the loop." (final summary table)

---

## 5. Recovery — if something stalls mid-demo

### 5.1 An incident is stuck in a non-terminal state

The poller waits up to 4 minutes per incident. If you see "Stopped polling
after 4 minutes — incident still in status=X", check:

- **Orchestrator log**: `tail -50 .demo_orch.log`
- **OpenAI rate limits**: hit the OpenAI status page; if rate-limited, wait
  60s and retry the same bug with `python scripts/demo.py --trigger <bug>`.
- **GitHub rate limits**: `curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/rate_limit` — if exhausted, you have a problem;
  switch to a backup token.

### 5.2 The orchestrator won't start

```bash
tail -50 .demo_orch.log
```

Look for the actual error. Most common cause is a port collision — the
demo CLI's `[2]` "Stop everything" force-kills anything on :8000/:9000,
so run that and try `[1]` again.

### 5.3 The dashboard is blank / disconnected pill is red

Refresh the browser. The connection pill should go back to `connected`
within ~2 seconds. If not, the orchestrator died — see 5.2.

### 5.4 A PR didn't open

Check the incident detail:

```
python scripts/demo.py
> 8        (Show latest incident)
```

If `status` is `fix_drafted` or `verified` but `pr_url` is empty, the
Releaser failed. Check `.demo_orch.log` for a PyGithub traceback. Most
common cause: branch protection or a stale token.

### 5.5 You need to bail and restart cleanly

```bash
python scripts/demo.py --reset       # closes PRs, wipes DB
# Then:
python scripts/demo.py               # menu → [1] → [3] → continue
```

The full reset takes ~10 seconds. You can restart between takes without
restarting your laptop or rotating any keys.

---

## 6. Post-recording

1. `python scripts/demo.py --reset` — leave the repo clean for the next
   presenter (or your future self).
2. Stop the orchestrator: `python scripts/demo.py` → `[2]` → `q`.
3. Verify `git status` is clean (no stray `.demo_orch.log` etc.).
4. Push any code edits that came out of the dry run.

---

## 7. Quick reference

```bash
# Pre-flight
python scripts/demo.py --status         # confirm everything's green
python scripts/demo.py --reset          # clean slate

# Recording
python scripts/demo.py                  # menu: 1 → 3 → 10 → q
python scripts/demo.py --demo-sequence  # the hands-off main event

# Per-bug one-shots (if you need to re-shoot a single take)
python scripts/demo.py --trigger null_deref
python scripts/demo.py --trigger regression
python scripts/demo.py --trigger config_error
```

That's the whole thing. Keep this open. Good luck.
