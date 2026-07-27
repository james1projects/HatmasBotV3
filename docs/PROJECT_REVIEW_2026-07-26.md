# HatmasBot Project Review — 2026-07-26

Full-repo review: file structure, documentation health, code quality, and feature ideas.
Scope measured: ~32,900 lines of Python (main.py 511, core/ 13,135, plugins/ 18,462), 21 root docs, 62 tools, 33 overlays.

**Note:** this file belongs in `docs/` once that folder exists (see §3). It's at root so you find it.

---

## TL;DR

1. **One real bug found (verified, fix first):** a Twitch disconnect kills the bot *permanently* — the supervisor's auto-restart never fires. ~4-line fix in `main.py`. See §2.
2. **Root clutter is mostly solved by three moves:** `docs/` for the 19 root markdown files, `assets/` for the god-art folders, `archive/vegas_pipeline/` for the abandoned Sony Vegas toolchain (superseded by the DaVinci Resolve tools in July). Full proposal in §3.
3. **Docs verdict:** of 21 root docs, only 6 are current. 3 should merge into HATMASBOT.md, 9 archive, 1 delete. HATMASBOT.md itself has four shipped features with zero documentation (Discord bridge, /mod page, /events page, Resolve pipeline) and still describes the dead Vegas pipeline as current. §4.
4. **~116 GB reclaimable** without touching anything live: 109 MB of stale git worktrees, a 6.5 GB abandoned mp4 in `inbox/`, ~15 MB of dead DB backups, and `recordings/` (112 GB) needs a retention policy. §5.
5. **`review_findings.md` contains unresolved security findings** for FindIt (cross-profile item theft, F6) that were never marked fixed/open. Needs a triage pass. §6.
6. **Silent data-loss risk:** `data/digit_templates/` (195 hand-curated OCR templates the kill detector depends on) is gitignored — a fresh clone silently degrades the detector. §5.

---

## 2. Fix-first bug: bot never restarts after an unrecoverable Twitch failure

Verified in code, not speculation.

`main.py:477-488` runs `asyncio.wait([bot_task, shutdown_watcher], return_when=FIRST_COMPLETED)` and **never inspects the `done` set**. `asyncio.wait` does not re-raise task exceptions, so if `bot.start()` dies (EventSub WebSocket unrecoverable, auth failure, etc.):

- The exception is never retrieved → the `except Exception` at line 490 never fires
- `record_crash()` never runs, `exit_code` stays **0**
- `tools/supervisor.py:74-76` sees exit code 0 → "bot exited cleanly; supervisor done" → **stops instead of restarting**

The entire crash-recovery stack (crash log, exponential backoff, `run_bot.bat`) is bypassed by exactly the failure it was built for. Fix: check whether `bot_task` is in `done`, call `bot_task.result()` in a try/except, run `record_crash()`, set `exit_code = 1`.

Related: **67 `asyncio.create_task()` call sites, 0 exception handlers** (`add_done_callback` appears nowhere). Any background loop — economy backfill, YouTube scanner, playback monitor, detection loop — that dies does so silently. One shared `_spawn(coro, name)` helper in `core/bot.py` that logs task exceptions fixes all 67 mechanically.

---

## 3. Proposed file structure

Current root has ~35 loose files: 19 markdown docs, 10 .bat scripts, 2 saved web pages with their `_files/` dirs, stray JSON, and 4 art folders with spaces in their names. Proposal:

```
HatmasBot/
├── main.py                  # unchanged
├── README.md                # unchanged (the only root doc)
├── requirements.txt         # unchanged
├── run_bot.bat + 8 live .bat  # keep at root — Stream Deck needs them double-clickable
│                              # (delete cleanup_p2.bat — one-shot migration, already ran)
├── core/                    # unchanged
├── plugins/                 # unchanged
├── overlays/                # unchanged (delete _prototypes/, see §5)
├── public/                  # unchanged
├── factorio_mod/            # unchanged
├── tests/                   # + the 13 test_*.py currently stranded in tools/
│   └── fixtures/kda/        # <- data/test_fixtures/kda (gitignore already apologizes for its location)
├── docs/
│   ├── HATMASBOT.md         # master doc
│   ├── Commands.md          # single canonical command list (kill the other 2 copies)
│   ├── thumbnail_commands.md
│   ├── StreamingSpaceGame_Plan.md   # only in-flight plan
│   ├── archive/             # the 9 completed/dead docs (§4 table)
│   └── vendor/              # mixitupopenapi.json, TwitchIO doc HTML + _files
├── tools/                   # optionally split: media/ detector/ assets/ ops/
│                            # (lower priority — flat works if tests + dead scripts leave)
├── assets/                  # already exists; absorb the root art folders:
│   ├── god_icons_custom/    # <- "Custom God Icons" (1,912 files)
│   ├── god_cards_custom/    # <- "Custom God Cards" (delete the stray VoukoderPro .msi inside!)
│   ├── icons_inbox/         # <- Custom_Icons_Inbox
│   ├── portrait_reference/  # <- Portrait_Source
│   ├── smite2_wiki/         # <- "Gods - SMITE 2 Wiki.html" + _files (load-bearing: 4 tools parse it)
│   └── digit_templates/     # <- data/digit_templates — COMMIT these (see §5)
├── infra/
│   └── cloudflare/          # <- workers/offline-fallback
├── archive/                 # exists already, extend:
│   └── vegas_pipeline/      # <- vegas_scripts/, vegas_presets/, config/vegas_pipeline.json,
│                            #    jobs/, tools/process_vods.py, "2026-04-16 ...events.json"
└── data/                    # runtime-only after the moves above
```

**Path updates required by the moves** (all verified references):

| Move | Files to update |
| --- | --- |
| Custom God Icons → assets/ | `core/config.py:15`, `core/config_local.py:51-55`, `tools/build_thumbnail.py:149`, `tools/import_god_icons.py:91` |
| Custom God Cards → assets/ | `tools/build_thumbnail.py:150`, `tools/build_outro.py:22`, `build_thumbnail.bat` |
| Wiki HTML → assets/ | `download_god_icons.py:55-56`, `tools/check_stream_ready.py:69`, `tools/import_god_icons.py`, `tools/download_god_cards.py` |
| Portrait_Source → assets/ | `core/god_matcher.py:190-199`, `tools/capture_god_reference.py:67`, `tools/diagnose_god_detection.py:67` |
| digit_templates → assets/ | `core/kda_reader.py:297-306`, `core/digit_matcher.py:10`, `plugins/killdetector.py:315`, `.gitignore` |
| tools/test_*.py → tests/ | none expected (standalone scripts), but grep imports first |

`download_god_icons.py` stays at root for now: three tools import it by hardcoded path (`REPO_ROOT / "download_god_icons.py"`) and `core/god_matcher.py:159` prints its invocation to users. Move it to `tools/` only as part of a deliberate pass that updates all four references.

Suggested order: (1) docs move — zero code impact; (2) deletions from §5 — zero code impact; (3) asset moves + path updates — one commit, test with `check_stream.bat` + a thumbnail build; (4) tools/tests split; (5) optional tools/ subfoldering.

---

## 4. Documentation: file-by-file verdicts

Cross-checked against code (plan docs verified implemented or not).

| File | Modified | Verdict | Why |
| --- | --- | --- | --- |
| HATMASBOT.md | 07-17 | **KEEP + maintenance pass** | Master doc, but header says v2.8.1 while content is v2.10.1; File Structure lists `plugins/smite.py`/`economy.py` (now packages); zero coverage of Discord, /mod, /events, Resolve pipeline; still documents Vegas as current; documents 7 `/overlay/economy_*` routes that were never registered (real URLs are `/overlays/economy_*.html` via static mount) |
| README.md | 07-17 | **KEEP** | Accurate; bump "v2.8" banner, add spacegame/findit to plugin tree |
| Commands.md | 07-17 | **MAKE CANONICAL** | Same command table exists in 3 places (here, README, HATMASBOT.md), drifting independently. Keep one, point the others at it — or auto-generate it (§7, idea 1) |
| thumbnail_commands.md | 07-19 | **KEEP** | Newest doc; ahead of HATMASBOT.md (3gods, build_guide presets) |
| StreamingSpaceGame_Plan.md | 06-19 | **KEEP** | Only in-flight plan: Phases 1-2 built (`plugins/spacegame.py` says so), 3-6 open |
| 7-6-2026-TODO.md | 07-06 | **KEEP pending your check** | Launch checklist; whether Stripe went live-mode and the /community refund blurb shipped is unverifiable from code — only you know. If done, archive |
| research_findings.md | 07-03 | KEEP, move to `plugins/findit/docs/` | FindIt R&D (YOLOE/DINOv3), still unimplemented |
| review_findings.md | 07-03 | KEEP, **triage required** | 22 security findings (F1-F22), none marked fixed/open. F6 = cross-profile item theft. Mitigated by FindIt being default-OFF, but unresolved |
| Command_Line_Tools.md | 05-04 | **MERGE into HATMASBOT.md** | Documents 17 of 62 tools; everything since May is missing. Actively misleading |
| Discord_Integration_Plan.md | 06-14 | MERGE into HATMASBOT.md, then archive | Phases 1/2/4 shipped (`plugins/discord_bridge.py`); this is the only doc of a live feature |
| Crossplatform_Commands_Plan.md | 06-12 | MERGE into HATMASBOT.md, then archive | Fully built (`plugins/custom_commands.py`, `public/mod.html`); only record of the /mod page |
| HATMAS_MARKET_AIRTIGHT_DESIGN.md | 05-23 | **ARCHIVE** | Implemented (fee removal confirmed at `core/config.py:344`; prescribed file split landed) |
| WEBSITE_TRADING_DESIGN.md | 06-09 | ARCHIVE | Shipped and live; still labeled "Draft for review" |
| Social_Tabs_Plan.md | 05-02 | ARCHIVE | Done 2026-06-10, superseded by HATMASBOT.md v2.7.1 |
| SonyVegasTODO.md | 04-24 | ARCHIVE | Pipeline abandoned for Resolve; keep for the hard-won Vegas SDK findings recorded nowhere else |
| TODO.md | 06-10 | ARCHIVE | Phase 5 done, Phase 4 abandoned with Vegas; superseded by 7-6-2026-TODO.md |
| cleanup_plan.md | 05-03 | ARCHIVE | The May audit — executed. Two survivors worth lifting: songrequest.py split never happened (still 1,307 lines); bare `except Exception` cleanup (now 321 sites) |
| MORNING_REPORT.md | 07-10 | ARCHIVE as dated file | Rolling file overwritten 9×; its merge action is done. **Lift the unshipped brainstorm first** — kill-feed cross-confirmation, detector health in /health, OBS replay-buffer auto-clip live nowhere else (absorbed into §7) |
| GOD_ROSTER_REPORT.md | 07-04 | ARCHIVE | Merged in `56302a8`; lift the roster-self-refresh paragraph into HATMASBOT.md first |
| Command to run K D A extractor.txt | 04-30 | **DELETE** | Two-line sticky note, fully duplicated elsewhere |
| factorio_mod/.../README.md | 06-11 | KEEP | Fix `C:\Users\james\HatmasBot` path; retitle "Next steps" (they're built) |

Also: at least 8 files still reference the old repo path `C:\Users\james\HatmasBot` (thumbnail_commands.md, cleanup_p2.bat, all three vegas_scripts .cs files, SonyVegasTODO.md, factorio README, .claude/settings.local.json). Worth one find-and-replace pass on the survivors.

---

## 5. Cleanup quick wins (no behavior change)

**Disk:**
- `.claude/worktrees/` — 109 MB, 4 dirs: 3 marked prunable by git, 1 fully orphaned. `git worktree prune` + delete. Also review the stale `overnight-*`/`godreq-*` local branches.
- `inbox/2026-04-23 17-42-29.mp4` — **6.5 GB**, the input to the Vegas run that never happened.
- `recordings/` — **112 GB**, 1,452 files. Needs a retention policy (§7, idea 9).
- `data/` root: ~19 stale `economy_backup_*.db` (~15 MB, superseded by `data/backups/` rotation), ~40 April detector-tuning PNGs (`slice_*.png` etc.), `data/redblue*` (dead tool).

**Dead files/dirs (verified unreferenced or superseded):**
- `overlays/_prototypes/` (11 files) — static mockups, no WebSocket/overlay_client.js; the theme they prototyped shipped
- `.claude/preview_gallery/` (7 files) — one-shot before/after viewer for the July restyle; stale *and* wrongly git-tracked
- `unpacked_doc/` — an exploded .docx from April, referenced by nothing
- `highlight/` — empty since April, only the dead Vegas config points at it
- `cleanup_p2.bat`, `.claude/scheduled_tasks.lock` (stale, pid from Jul 6), `Custom God Cards/VoukoderPro-*.msi` (an installer in an art folder), `assets/ship_orange.png.png` (double extension)
- `core/webserver.py:1087` registers `/overlay/snap` → `overlays/snap.html`, which has never existed; `plugins/snap.py` is commented out in main.py. Delete route or ship the file.
- `.claude/launch.json`: `events-harness` entry points at a dead Temp path; `antrts` entry points at a different repo.

**Gitignore fixes:**
- `data/digit_templates/` (195 curated OCR templates) is swallowed by `data/*` — **a fresh clone gets zero templates and the detector silently degrades**. Add `!data/digit_templates/**` now; relocate to `assets/` later.
- `vegas_presets/_tuneframe_last_run.txt` is a committed run log.
- `.claude/preview_gallery/` is committed but shouldn't be.

---

## 6. Code improvement areas (ranked)

The codebase is well above hobby average: zero bare excepts, zero SQL injection, parameterized queries throughout, no committed secrets ever (git history verified), atomic writes in 24 places, excellent explanatory comments. The issues below are the growth scars of four fast months.

1. **The §2 restart bug + the 67 unwatched background tasks.** Highest value-to-effort in the repo.
2. **Secrets consolidation.** `core/config_local.py` holds 13 live credentials (Stripe secret + webhook key, Discord token, Anthropic key, Twitch tokens, session-signing secret...) in one plaintext file guarded by one `.gitignore` line. `core/config.py` already reads `os.environ` for nearly all of them — the plumbing exists, only the values need to move to env vars (or at minimum, add a pre-commit secrets scan). One `git add -f` away from a very bad day.
3. **Atomic-write gaps.** `core/bot.py:248` and `:419` write `command_platforms.json` and `feature_overrides.json` — the files deciding which subsystems are on after restart — with raw `write_text`, bypassing the `atomic_io` module built for exactly this. A corrupt `feature_overrides.json` silently resets every dashboard toggle to defaults. Same fix needed in `snap.py:33`, `nsfw_check.py:53`, `detector_regions.py:119`; and `auth.py`/`token_manager.py`/`findit/items_store.py` hand-roll the pattern instead of importing it.
4. **Dashboard (port 8069) has zero auth.** Localhost-bound, but no Origin/Host validation on 53 routes including the 36-action mutation endpoint — any page in your browser could hit it (DNS rebinding). The public server already does this right (`_is_local_admin`); copy the pattern. Also `webserver.py` indexes `bot.plugins["snap"]` etc. without `.get()` — commenting a plugin out of main.py turns dashboard actions into 500s (snap is *currently* in that state), and it reaches into other plugins' private state (`._suggestions`, `._session_wins`) in 7 places.
5. **Testing: 812 test lines vs 32,900 code lines, no CI, no pytest config, and the suite is split** — 13 `test_*.py` in `tools/`, 3 in `tests/`, two of which pytest can't even collect (script-style). Zero coverage on: OAuth flows, Stripe webhook, trading endpoints, token refresh, command routing. Start with `core/web_session.py` (written to be unit-testable; its test is stranded in tools/) and the Stripe path (real money). Add a GitHub Actions workflow with pytest + ruff.
6. **God objects.** `core/public_webserver.py` (3,470 lines, 66 routes, one class) → aiohttp sub-apps along existing method-name seams (auth/market/mod/social/payments/ws). `core/webserver.py:656` `handle_action` = 341-line if/elif with 36 branches → dispatch dict. `plugins/killdetector.py:924` `_detection_loop` = 906 lines. `main()` = 445 lines doing DI + wiring + lifecycle. `plugins/songrequest.py` (1,307) is the one split from cleanup_plan.md that never happened. The package plugins (economy/, smite/, factorio/, findit/) show the target pattern — nothing in them exceeds 664 lines.
7. **Duplication.** MixItUp HTTP client copied 3× (`gamble.py`, `godrequest.py`, `economy/mixitup.py` — the last one's docstring literally asks the others to use it). aiohttp session lifecycle copied 10×; 26 `ClientSession(` sites, 12 of them throwaway per-request (defeats pooling). "401 → refresh → retry" copied 3× despite `TokenManager.handle_401()` existing. ~34 hand-rolled JSON load/save blocks.
8. **Logging: 678 prints vs 29 logger calls.** No levels, rotation, or timestamps; `main.py` has to fish out the one real logger by name at shutdown. Mechanical fix, big payoff. Pair with a `BasePlugin` ABC — 5 plugins are missing `cleanup()` entirely and nothing enforces the contract.
9. **Resilience gaps.** OBS: `reconnect()` exists, nothing calls it — start the bot before OBS and scene control + kill detection are dead for the session; `log_quiet.py` exists to mute the resulting errors (symptom treatment). Contrast with `streamloots.py`'s exemplary SSE backoff loop — copy that pattern. Also: `economy.db-wal` is 4.1 MB vs a 2.8 MB DB — checkpoints aren't keeping up; consider `wal_autocheckpoint` tuning. Requirements: all 13 deps are `>=` with no lock file while `core/bot.py:784-816` pokes TwitchIO 3.2.1 internals — pin `twitchio==3.2.1` at minimum.
10. **FindIt security triage** (review_findings.md, §4 above): mark each of F1-F22 fixed/accepted/open before FindIt ever defaults ON.

---

## 7. Feature ideas

Grounded in what exists; roughly ordered by payoff-for-effort.

1. **Auto-generated command reference.** Commands are registered in code; generate Commands.md (and a `!commands`-linked page on hatmaster.tv) from the registry at startup. Permanently kills the three-drifting-copies problem and documents Discord slash-command parity for free.
2. **Health dashboard / watchdog.** One `/health` panel on the mod page: per-background-task liveness (fixes the "YouTube scanner died 3 days ago" blind spot from §2), OBS connection state, detector frame-rate and last-KDA-read age, tunnel status, WAL size, token expiry. The MORNING_REPORT brainstorm already wanted detector health surfaced; this generalizes it. Natural follow-on: auto-reconnect OBS from the same watchdog.
3. **End-of-stream recap.** You have the data already: session KDA (smite plugin), deaths, market movers and top traders (economy), gamble jackpot, new followers/subs (EventSub). Auto-post a recap card to Discord at `go_offline` time. Viewers love it, and it drives Discord.
4. **Kill-feed cross-confirmation** (from MORNING_REPORT, otherwise lost): confirm KDA-OCR kill events against the on-screen kill feed before overlays/economy react — cuts false positives in the money path.
5. **OBS replay-buffer auto-clips** (same source): on confirmed kill, save the replay buffer; end-of-stream, feed the clip list into the Resolve pipeline → auto-drafted TikTok/Shorts. This connects two systems you've already built (detector + resolve_tiktok.py) into a content flywheel.
6. **SpaceGame Phases 3-6.** The only in-flight plan doc. Phase 3 (currency) is where it becomes a retention feature — and its §11 open decisions (currency name, boss rewards, lose condition) are decisions only you can make; worth 20 minutes on stream with chat.
7. **Discord account linking** (Plan Phase 5, deferred). The v2.9 YouTube↔Twitch merge built the account-linking machinery; extending it to Discord unlocks cross-platform economy balances and slash-command trading.
8. **Economy seasons.** Quarterly leaderboard resets with a hall-of-fame page on hatmaster.tv and a small Streamloots/priority-token prize. Cheap to build on existing tables; recurring engagement spike.
9. **Recordings retention tool.** `tools/prune_recordings.py`: keep events.json + clips, age out raw mp4s (112 GB and growing). Could run from `process_recordings.bat`.
10. **FindIt model refresh** when you next touch it: research_findings.md's YOLOE/DINOv3 recommendations are still open, and the bench scaffolding (`embed_bench.py`, `findit_bench.py`) already exists to validate the swap. Gate on the F1-F22 triage.

---

## 8. Suggested order of operations

1. `main.py` restart bug (§2) — do this before the next stream
2. Atomic-write fixes for `feature_overrides.json` / `command_platforms.json` (§6.3) — 10 minutes
3. §5 deletions + worktree prune + digit_templates gitignore exception — one sitting, no code changes
4. Docs move to `docs/` + archive per §4 table — no code changes
5. Asset-folder moves with path updates (§3) — one careful commit, verified by `check_stream.bat` + one thumbnail build
6. Secrets → env vars (§6.2)
7. Then the refactors (§6.5-6.8) and features (§7) as streams allow
