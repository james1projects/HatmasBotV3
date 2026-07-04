# Morning Report — overnight-2026-07-04 (Reliability night)

**Branch:** `overnight-2026-07-04` (worktree at `.claude/worktrees/overnight-2026-07-04`)
**Nothing merged — you merge after reading this.**

Goal you set before bed: keep hatmasbot up and hatmaster.tv always
available, with a graceful offline page when it isn't.

## TL;DR

The bot now restarts itself after a crash and explains every outage in
`data/crash.log`. The dashboard and `check_stream.bat` can see at a
glance which subsystem is sick. Visitors to hatmaster.tv get a branded
"Market closed" page instead of Cloudflare error 1033 once you deploy
the worker (5 minutes, steps below). Three of the six planned fixes
turned out to be already-solved problems — verified and documented
instead of "fixed".

## Two things only you can do (both quick)

1. **Switch to the supervised launcher.** After merging, start the bot
   with `run_bot.bat` (or install auto-start at logon:
   `powershell -ExecutionPolicy Bypass -File tools\install_bot_task.ps1`,
   verified on this machine's PS 5.1). Crash -> auto-restart with
   5s→300s backoff + traceback in `data/crash.log`. Typing `quit` or
   Ctrl+C still stops everything for real.
2. **Deploy the offline-fallback worker** (needs your Cloudflare
   login): follow `workers/offline-fallback/README.md` — dashboard
   copy-paste + route, no tooling needed. Verification recipe
   included. Until deployed, nothing changes for visitors.

## Commits

### 69fc642 — Crash-restarting supervisor + persistent crash log
- `tools/supervisor.py` relaunches `main.py` on non-zero exit
  (backoff 5s doubling to 300s, reset after 10 min stable); exits with
  the bot on clean quit/Ctrl+C so intentional stops stay stopped.
- `core/crash_log.py` appends timestamped tracebacks + restart events
  to `data/crash.log` (1 MB rotation to `crash.log.1`, stdlib-only,
  never raises on the error path).
- `main.py` now returns exit code 1 on unhandled exception (records
  the traceback first) and 0 on quit/Ctrl+C — that's how the
  supervisor tells crash from intent. **Risk:** low; the change is
  confined to the top-level except block and `__main__`.
- Verified: scratchpad harness ran a fake main.py through
  crash → restart → crash → restart → clean-exit; backoff doubled
  1s→2s; crash.log captured both tracebacks and all events; re-ran
  after the review fix (spawn-failure logging), same result.

### cac2e19 — GET /health + dashboard badge + readiness probe
- `/health` on the local dashboard port (8069) self-reports: shared
  SQLite connection (SELECT 1), token validation snapshot (new
  `TokenManager.status()`, stale after 2x the 50-min validation
  interval), EventSub websocket session count (defensive read of the
  same TwitchIO-internal map the channel-points workaround already
  uses), Cloudflare tunnel (bot child process, else `sc query
  cloudflared` cached 30s). 200 healthy / 503 with per-check JSON.
- Control panel: "ALL SYSTEMS UP" / "DOWN: <subsystems>" badge under
  the H1, 30s poll, palette state colors only.
- `check_stream_ready.py` gains a `bot_health` probe (shows up in
  `check_stream.bat`).
- **Risk:** low-medium; /health is read-only, but note
  `last_event_time` is now set on every chat message (one float
  assignment).
- Verified: served the real WebServer on a spare port; asserted the
  503 path (bare server) and 200 path (stubbed subsystems) including
  per-check fields. Live detail: the cloudflared **Windows service**
  was RUNNING during the test, so the tunnel check passed for real.

### ba7adb3 — Cloudflare Worker offline fallback (NOT deployed)
- `workers/offline-fallback/worker.js|wrangler.toml|README.md`.
  Passthrough proxy on `hatmaster.tv/*`; on origin-down statuses
  (502/503/504/521/522/523/530) serves a self-contained branded
  "Market closed" page for HTML GETs only — API/asset requests keep
  raw statuses so JSON consumers never get HTML. 503 + Retry-After:120
  + no-store.
- Page mirrors the 404 page design with theme.css tokens inlined
  (origin assets are unreachable exactly when the page shows).
- Verified: extracted the embedded HTML, rendered it in a local
  preview; computed styles match brand (#202C39 bg, Bebas display,
  Inter body, bronze mono code); copy/links checked in the
  accessibility tree. Twitch link -> twitch.tv/hatmaster (from
  landing.html).

### (final commit) — Review fix + this report
- Supervisor logs `FATAL: could not launch bot` to crash.log before
  dying if the spawn itself fails (config problem ≠ crash; no retry
  loop, but now diagnosable after the console window is gone).

## Verified as already fine (no code changed)

- **401-reactive token refresh** — already fully implemented:
  `TokenManager.handle_401()` with refresh-and-retry-once at 12 call
  sites (core/bot.py, economy, smite predictions/title, stream_status,
  voicelines). The "missing feature" claim was a false alarm.
- **WAL idle checkpoint** — unnecessary: nothing disables SQLite's
  default autocheckpoint (1000 pages ≈ 4 MB); live WAL measured 4.6 MB
  = bounded and healthy; crash recovery replays WAL automatically.
- **Streamloots reconnect backoff** — already there: 5→120s
  exponential with reset on success, 4xx config-error fail-fast,
  15-min dead-connection watchdog; prompt reconnect on clean EOF is a
  deliberate, commented choice (don't miss cards).

## Review + tests

- Local fleet (qwen3.5:35b) reviewed the full night diff in three
  chunks: 9 findings, **8 rejected on verification** (asyncio "races"
  that are single-threaded, a PS 5.1 parameter claim disproven by
  running it on this machine, unreachable null paths, deliberate
  defensive excepts), 1 accepted (supervisor spawn-failure logging,
  applied + re-tested). Consistent with the ~80% false-alarm rate.
- Test suites: test_trading_hardening, test_web_session (11),
  test_web_trade (20), test_priority_request (18) — all exit 0.
  test_economy — exit 0 ("All tests completed!"). Gotcha for future
  runs: it prints an emoji, so with redirected output it wedges on a
  cp1252 UnicodeEncodeError unless `PYTHONIOENCODING=utf-8` is set
  (fine in an interactive console).
- Every touched Python file py_compile-clean, trailing newlines
  verified.

## Also worth knowing

- The scheduled task triggers at **logon**, not boot — the bot needs
  your session (OBS websocket) anyway. Auto-logon would be the next
  step if you ever want bot-up-before-you-sit-down.
- "Always available" beyond this: the PC still sleeps per your power
  policy, and that's by design tonight (you chose the graceful-page
  option). If you later want true 24/7, the decision points are:
  always-on PC vs. moving the read-only site off-box.
- The stale `overnight-2026-07-03` worktree (merged yesterday) was
  pruned during setup.
