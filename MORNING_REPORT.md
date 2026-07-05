# Morning Report — overnight-2026-07-04 (Reliability night + resolver + KDA)

**Branch:** `overnight-2026-07-04` (worktree at `.claude/worktrees/overnight-2026-07-04`)
**Nothing merged — you merge after reading this.**

Goal from your bedtime message: keep hatmasbot up and hatmaster.tv
always available, with a graceful offline page. Mid-run I found the
plan memory from your side chat (it landed in the memory index after
this session started) and picked up its two additions: the god-name
resolver and the KDA replay harness. Everything below is done.

## TL;DR

The bot now restarts itself after a crash and explains every outage in
`data/crash.log`. The dashboard and `check_stream.bat` can see at a
glance which subsystem is sick. Visitors to hatmaster.tv get a branded
"Market closed" page instead of Cloudflare error 1033 once you deploy
the worker (5 minutes, steps below). Chat can now typo, nickname, or
squash god names anywhere (!godrequest, !nominate, !buy) — 110/110 on
the misspelling eval with zero junk false-positives. The new KDA
replay harness immediately caught a real detector bug (a phantom
assist digit was silently vetoing kills — 2 of 7 lost on the Atlas
full-gameplay VOD) and the fix takes it to 7/7 with an A/B run over
all 91 archived clips showing zero regressions. Three of the six
originally-planned fixes turned out to be already-solved problems —
verified and documented instead of "fixed".

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

### 810f3f4 — Review fix + this report
- Supervisor logs `FATAL: could not launch bot` to crash.log before
  dying if the spawn itself fails (config problem ≠ crash; no retry
  loop, but now diagnosable after the console window is gone).

### e871557 — Tiered god-name resolver (side-chat item 5)
- `core/god_resolver.py`: one resolver behind every chat surface that
  takes a god name. Tiers: exact (normalized + space-squashed) →
  alias (`core/god_aliases.py`, 78 community nicknames — fleet-drafted,
  hand-pruned of invented ones) → unique prefix → unique contains →
  difflib fuzzy (0.75 cutoff, clear-winner margin, phonetic fold for
  "skilla"→Scylla) → local Ollama tier.
- The LLM tier is exactly what the plan asked for: strict 2.5s
  timeout, silently skipped when the model is cold or the GPU is busy
  with Smite/OBS, answers validated against the candidate list so a
  hallucination can't land. Config: `GOD_RESOLVER_LLM_*` in
  core/config.py. **Gotcha found live: thinking models (qwen3.6)
  return an empty response under a small token budget — default is
  qwen3-coder:30b with think:false.** Probed live: "the snake hair
  lady" → Medusa, junk → NONE.
- Wired into godrequest (`_match_god` + LLM last-resort in the
  !godrequest handler; priority_request inherits via delegation),
  god_pool `_resolve_god`, economy `_resolve_god_name` (**tiers 1-5
  only — the money path stays deterministic on purpose**).
- Eval: `tools/eval_god_resolver.py` + 122-case set. **110/110
  non-junk (exact 68 / alias 26 / fuzzy 11 / prefix 5), 0 of 12 junk
  inputs wrongly resolved.** Two fleet-authored eval expectations were
  wrong ("morri" = The Morrigan, not Morgan le Fay) — fixed.
- Icon library audit: all 82 gods present vs the saved wiki HTML.
  (Resave the wiki HTML periodically — the check is only as fresh as
  that file; see god-icon-library-gaps memory.)

### 88c5e4d — KDA replay harness + real detector fix (side-chat item 6)
- `tools/kda_replay_eval.py` replays the VOD detector over saved
  footage and diffs against the archived `.events.json`. Honesty note:
  those files are **prior detector output** (written by
  process_recordings.py), not hand labels — so this measures drift
  and gives a reviewable disagreement list, and doubles as a
  regression gate for any kda_reader/vod_detector change.
- **Real bug found and fixed:** a stable UI artifact reads the assist
  field as a phantom "6", survives the two-read baseline
  confirmation, and then every CORRECT read is vetoed as a "partial
  KDA decrease" — the Atlas full-gameplay VOD silently lost 2 of 7
  kills this way. New rule in tools/vod_detector.py
  (`REBASELINE_REQUIRED_READS = 3`): three consecutive identical
  reads disagreeing with the baseline only downward = the baseline
  was the misread; re-baseline, keeping increases as real events.
- **Before/after: 5/7 → 7/7 matched on Full Gameplay (≤0.1s
  timestamp precision); Atlas-1 1/1 unchanged.** Full sweep over all
  91 archived Atlas clips: remaining disagreements (12 missed / 27
  new, clustered at ~40s clip edges) were **A/B-verified as
  pre-existing** — the old code gives identical-or-worse results on
  every suspicious clip (Atlas-42's archived death is matched only
  by the new code).
- Known limitation (also pre-existing): the assist field still flaps
  on the phantom digit, which can emit spurious assist events; kills
  and deaths are verified clean. Root fix is reader-level assist
  digit filtering — flagged as a follow-up task chip along with the
  clip-edge phantom batches and a mirror-check of the LIVE detector
  (plugins/killdetector.py) for the same veto bug.

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

- Local fleet (qwen3.5:35b) reviewed the night's diffs in five
  chunks: 14 findings total, **11 rejected on verification** (asyncio
  "races" that are single-threaded, a PS 5.1 parameter claim
  disproven by running it on this machine, unreachable null paths,
  deliberate defensive excepts, "dead code" that isn't), 3 accepted
  and applied + re-verified: supervisor spawn-failure logging,
  punctuation stripping in the resolver ("kuku!!" now resolves), and
  a strictly-consecutive counter reset in the detector's re-baseline
  rule. Consistent with the ~80% false-alarm rate.
- Test suites (run twice: after the reliability work and again after
  the resolver wiring): test_economy, test_trading_hardening,
  test_web_session (11), test_web_trade (20), test_priority_request
  (18) — all exit 0. Gotcha for future runs: test_economy prints an
  emoji, so with redirected output it wedges on a cp1252
  UnicodeEncodeError unless `PYTHONIOENCODING=utf-8` is set (fine in
  an interactive console). Flagged as a task chip.
- Resolver eval re-run after every resolver change: 110/110, 0 junk
  false-positives each time. Full Gameplay replay re-run after the
  final detector tweak to confirm 7/7 held.
- Every touched Python file py_compile-clean, trailing newlines
  verified.

## Also worth knowing

- **ffmpeg is not installed on this PC** — that's the "ffmpeg-on-PATH
  mystery" solved. Tonight's KDA runs used the imageio-ffmpeg pip
  binary plus a temporary ffprobe shim. To run the replay harness
  plainly: `winget install Gyan.FFmpeg`, then
  `python tools/kda_replay_eval.py recordings/Atlas`.
- The scheduled task triggers at **logon**, not boot — the bot needs
  your session (OBS websocket) anyway. Auto-logon would be the next
  step if you ever want bot-up-before-you-sit-down.
- "Always available" beyond this: the PC still sleeps per your power
  policy, and that's by design tonight (you chose the graceful-page
  option). If you later want true 24/7, the decision points are:
  always-on PC vs. moving the read-only site off-box.
- The stale `overnight-2026-07-03` worktree (merged yesterday) was
  pruned during setup. Three follow-up task chips are waiting in the
  session: de-emoji the test harnesses, the clip-edge phantom event
  batches + live-detector mirror check, and nothing else.
- Process note: next time a side-chat plan exists, land it in memory
  BEFORE the overnight session starts (I found it mid-run).
