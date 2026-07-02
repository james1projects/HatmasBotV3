# HatmasBot

Complete reference for HatmasBot. This file is the single source of truth for Claude when working on this codebase across sessions.

**v2.8.1 | July 2026 | Built by Hatmaster & Claude**

v2.5 adds the public-facing Hatmas Market website (`hatmaster.tv`),
YouTube comment-based share rewards, periodic match backfill from
tracker.gg, the fair-value pricing formula, the replay tool, and
several CLI tools for ops. See the **v2.5 Update — Hatmas Market
Public Website** section near the bottom of this document for full
details.

v2.6 adds paid priority god requests: viewers pay $5 via Stripe
Checkout on `hatmaster.tv/community` to jump the god request queue,
with a crash-safe webhook lifecycle, refund/dispute handling, a
reconciliation CLI, and a 16-test regression suite. See the **v2.6
Update — Priority God Requests (Stripe)** section at the bottom.

v2.7 adds Twitch login + trading on hatmaster.tv: viewers log in with
Twitch (zero scopes, identity only) and buy/sell god shares from the
portfolio and god pages — same execute_buy/execute_sell path as chat.
See the **v2.7 Update — Website Login + Trading** section at the
bottom and `WEBSITE_TRADING_DESIGN.md`.

v2.8 adds the Streamloots event hub (SSE listener on the alert
overlay stream with listener-list dispatch of card redemptions,
chest purchases, and gifts) and the game half of the Factorio
integration: the `hatmas-events` Factorio mod with viewer pets and
boss biters, a `hatmas` remote interface for future RCON control,
and a JSONL event outbox. See the **v2.8 Update — Streamloots Hub +
Factorio Mod** section at the bottom.

July 2026: the website split in two. `hatmaster.tv/` is now a simple
home page (social tabs: Twitch live embed, YouTube, TikTok, Bluesky,
plus a market teaser card), and all Hatmas Market content (ticker,
god grid, search, top traders, recent activity) lives at
`hatmaster.tv/market` (`public/market.html`; `/Market` redirects).

v2.8.1 (July 2, overnight hardening pass) fixes a whisper bypass of
mod-only commands, makes every trade/dividend/settlement path
compensate cleanly on partial failure, moves gTTS and the Claude chat
API call off the event loop, converts every plugin JSON state write to
atomic tmp+replace (new `core/atomic_io.py`), and recovers a
truncated-on-disk `plugins/economy/plugin.py` whose `cleanup()` had
silently been a no-op. See the **v2.8.1 Update — Overnight Hardening
Pass** section at the bottom.

---

## Working in this repo (read first if you are Claude)

This repo has an observed file-tool desync: `Edit` and `Write` on files
inside `C:\Users\james\HatmasBot` can silently leave the on-disk file
truncated mid-statement or with NUL bytes injected, even when the tool
reports success and a post-edit `Read` shows clean content. Verify every
non-trivial edit from bash, not from `Read`:

```bash
file <path>                       # expect "Python script, Unicode text"; "data" means NUL bytes
wc -l <path>                      # should match expected line count
tail -5 <path>                    # should end on a complete statement
python3 -m py_compile <path>      # for .py files
```

For edits longer than a few lines, multi-line strings, or anything touching
docstrings, skip `Edit`/`Write` and apply the change via a small Python
script in bash (read, assert exact `old` block matches once, write
replacement, verify on disk). The `hatmasbot-safe-edits` skill has the full
playbook including recovery (`tr -d '\0'` for NUL bytes, bash heredoc
rewrite for truncation). Install it if it isn't already loaded.

This is not hypothetical: `plugins/economy/plugin.py` shipped truncated
mid-comment for several releases (discovered 2026-07-02). It still
compiled because a docstring alone is a valid function body, so
`EconomyPlugin.cleanup()` silently did nothing on every shutdown. A
reliable tripwire: a Python file whose last byte is not a newline is a
truncation suspect —

```bash
for f in $(git ls-files '*.py'); do
  [ -n "$(tail -c 1 "$f" | tr -d '\0\r\n')" ] && echo "SUSPECT: $f"
done
```

---

## Tone and Style

All chat messages, overlay text, and any text that appears on screen must read like a straightforward bot — no emojis, no flair, no AI-sounding language. Keep it plain and informational. For example: "Player Kill! KDA: 18/27/0" not "⚔️ PLAYER KILL! KDA: 18/27/0". Same applies to announcements, notifications, and any user-facing strings throughout the codebase.

---

## Tech Stack

- Python 3.14, TwitchIO v3.2.1 (EventSub WebSocket)
- aiohttp web server on port 8069 (also used as the HTTP client for Spotify Web API — no spotipy dependency)
- obsws-python for OBS WebSocket control
- yt-dlp for YouTube audio
- curl_cffi for Cloudflare bypass on tracker.gg
- aiosqlite for async SQLite (economy database, WAL mode)
- Pillow + opencv-python + numpy for KDA digit matching and god portrait identification
- anthropic SDK for !@HatmasBot Claude API responses
- gTTS for server-side text-to-speech
- MixItUp Developer API v2 on localhost:8911 for currency (Hats) and inventory (God Tokens)

## Architecture

Plugin system. Each plugin has `setup(bot)`, `on_ready()`, `cleanup()`. Bot core (core/bot.py) handles EventSub subscriptions, command routing, and plugin management. `on_ready()` is called in plugin registration order, so plugins registered later (e.g. OBS) are not yet connected when earlier plugins (e.g. Smite) run their `on_ready()`. Any cross-plugin startup work that depends on OBS must use a background task that polls for OBS connection readiness. Web server (core/webserver.py) serves overlays as OBS browser sources and a control panel dashboard. State is shared via `/api/state` JSON endpoint, polled by overlays every 1s. Config in core/config.py with local overrides in core/config_local.py (gitignored).

## Plugin Registration Order (main.py)

Order matters: plugins later in the list can rely on earlier ones being
registered (their `setup()` has run, even if `on_ready()` hasn't fired
yet). DeathCounterPlugin in particular is registered **before**
KillDeathDetector so the kill detector's `on_death` callback can call
`death_counter.increment()`.

1. BasicPlugin - chat commands, suggestion box
2. SmitePlugin - match tracking, god detection, predictions, title updates (receives token_manager)
3. SongRequestPlugin - Spotify/YouTube song requests with queue and likes
4. OBSPlugin - scene switching, source control, fade effects
5. GodRequestPlugin - god request queue with MixItUp token economy
6. ClaudeChatPlugin - AI chat responses via Claude API (per-user history, safety)
7. GamblePlugin - wager Hats on dice rolls with jackpot pool
8. DeathCounterPlugin - daily death tally with auto-reset at midnight, powers the /overlay/deaths browser source
9. KillDeathDetector - real-time kill/death/assist detection via OBS screenshot analysis + template matching
10. VoiceLinePlugin - channel point redemptions for god-specific jokes, taunts, and laughs with optional MP4 animations (receives token_manager)
11. EconomyPlugin - stock-market-style god economy with live price ticking, dividends, trading, and 7 websocket overlays (receives token_manager)
12. YouTubeRewardsPlugin - polls YouTube Data API v3 for new commenters on "Full Gameplay" videos and grants free shares of the featured god
13. StreamStatusPlugin - polls Twitch /helix/streams every 60s and emits stream_live / stream_offline events to the overlay manager (receives token_manager + web_server)
14. YouTubeLiveBadgePlugin - listens for stream_live / stream_offline and shells out to tools/youtube_live_badge.py to apply/revert LIVE badges on the last 8 YouTube thumbnails
15. BackupManagerPlugin - daily gzipped snapshots of economy.db to data/backups/ (configurable via BACKUP_INTERVAL_HOURS / BACKUP_RETENTION_DAYS)
16. GodPoolPlugin - viewer-driven god voting (!nominate / !pool / !spin / !poolclear) with the current pool exposed via /api/community for the website's /community page
17. PriorityRequestPlugin - Stripe-paid priority god requests ($5 queue jump bought on hatmaster.tv/community). Registered after GodRequestPlugin so it can resolve the godrequest plugin. No chat commands — the entire surface is HTTP on the public webserver. See the v2.6 section at the bottom.

18. StreamlootsPlugin - SSE listener on the Streamloots alert overlay stream. Event hub: other plugins subscribe via add_redemption_listener / add_purchase_listener / add_gift_listener in main.py. No-op until STREAMLOOTS_ALERT_ID is set in config_local.py.
19. FactorioPlugin - bot half of the hatmas-events Factorio mod. RCON commands into the game, outbox tailer out of it (chat announcements). Registered after StreamlootsPlugin; main.py wires streamloots.add_redemption_listener(factorio.on_streamloots_redemption) and factorio.register_api_routes(web.app). No chat commands - the control surface is the card manager page at /factorio/cards (mappings persist to data/factorio_cards.json; FACTORIO_CARD_MAP only seeds it once). RCON disabled until FACTORIO_RCON_PASSWORD is set in config_local.py; the card manager works regardless.

SnapPlugin exists but is commented out in main.py.

Bot also receives `token_manager` directly for raid shoutout and TTS API calls.

**Cross-plugin event wiring (listener-list pattern).** After registration, main.py wires inter-plugin event flow without monkey-patching. Two hubs publish events; multiple subscribers attach listeners to each:

- `KillDeathDetector` exposes `add_kill_listener` / `add_multikill_listener` / `add_death_listener` / `add_assist_listener` / `add_god_identified_listener` / `add_gameplay_ended_listener`. Both the webserver (overlay events) and `EconomyPlugin` (live price ticks on K/D/A) subscribe. `DeathCounterPlugin.increment()` is also called from a kill-detector death listener.
- `SmitePlugin` exposes `on_match_start` / `on_match_end` / `on_match_result` / `on_god_detected`. `EconomyPlugin` subscribes to all four (dividends, tick-stop, settlement). `VoiceLinePlugin` subscribes to god detection and match end to track / clear the active god for redemptions.

Because listeners are append-only lists, adding a new plugin that needs to react to a kill or a match result is purely additive — register the plugin, then `kd.add_kill_listener(plugin.on_kill)` in main.py. No existing wiring needs to be touched.

KillDeathDetector starts automatically on bot launch and runs continuously (does not depend on SmitePlugin match state). It uses OBS WebSocket to grab screenshots of the "Smite 2" source every 0.8s, detects gameplay via HUD variance checks, identifies the current god from the in-game portrait via HSV histogram matching (2-5 min faster than the tracker.gg API), and then reads K/D/A numbers from the HUD bar using a per-component template-based digit matcher (with per-component Tesseract OCR fallback). KDA reading is gated behind god portrait identification — the detector won't attempt KDA reads until a god is matched, which eliminates noise during lobby, god select, and menus. When K, D, or A increases, it fires callbacks that push events to the webserver queue for the overlay. KDA state persists to disk (JSON with 30-min staleness threshold) so mid-game bot restarts resume correctly. On startup, 3 consecutive identical reads are required before accepting a baseline. Optional chat announcement toggle sends KDA updates to Twitch chat.

---

## File Structure

```
main.py                     Entry point. Creates TokenManager, WebServer, Bot, registers plugins. Graceful shutdown via console ("quit"/"exit"/"stop"/"close") or Ctrl+C.
core/
  bot.py                    HatmasBot class. EventSub subs, command routing, raid handler, TTS handler.
  config.py                 All settings. Loads config_local.py at bottom.
  token_manager.py          Async OAuth token manager with auto-refresh for bot + broadcaster tokens.
  webserver.py              aiohttp server. Overlays, API, control panel, TTS/gamble queues.
  auth.py                   OAuth browser flow for bot and broadcaster tokens.
  atomic_io.py              atomic_write_json/_text (tmp + os.replace). Used by every plugin that persists data/*.json — a crash mid-write can no longer truncate state files.
  cache.py                  Simple TTL cache.
  nsfw_check.py             Album art NSFW classification.
  god_matcher.py            Portrait-based god identification via HSV histogram matching.
  digit_matcher.py          Template-based digit recognition for KDA numbers. XOR distance + hole-count pre-filter.
  web_session.py            Stateless HMAC-signed session tokens for hatmaster.tv login (stdlib only). Rotating WEB_SESSION_SECRET logs everyone out.
plugins/
  basic.py                  !hello, !uptime, !socials, !suggest, !suggestions, !clearsuggestions
  smite.py                  Match tracking, god detection, predictions, title, record, commands.
  songrequest.py            Spotify/YouTube queue, likes, now playing overlay, blacklist.
  obs.py                    OBS WebSocket control, scene switching, fade effects.
  godrequest.py             God request queue, token economy, MixItUp integration, auto-complete. Exposes add_history_listener for resolved entries (played/skipped/removed).
  priority_request.py       Stripe-paid priority god requests. Two-phase webhook lifecycle (paid -> fulfilled), refund/dispute handling, played_at stamping via godrequest history listener.
  claude_chat.py            Claude API responses for @mentions. Per-user history, safety prompt.
  gamble.py                 Dice roll gambling with Hats currency, jackpot pool, sound/visual alerts.
  killdetector.py           Real-time kill/death detection via OBS screenshot analysis + template matching (Tesseract OCR fallback).
  voicelines.py             Channel point redemptions for god voice lines (jokes, taunts, laughs) with MP4 animation support.
  deathcounter.py           Daily death tally with auto-reset at midnight. Hooks killdetector on_death, powers /overlay/deaths.
  economy.py                Stock market economy: god share prices, live ticks, dividends, trading, 7 overlay events.
  snap.py                   Thanos snap. Times out random half of chat.
  streamloots.py            Streamloots event hub. SSE listener + listener-list dispatch of card redemptions, chest purchases, gifts.
  factorio/                 Factorio integration package. plugin.py (lifecycle, card handling, manager API), rcon.py (asyncio Source-RCON client, no deps), catalog.py (action defs, Lua escaping, outbox->chat formatting), cards.py (CardStore: persisted card->action mappings), events.py (JSONL outbox tailer).
overlays/
  control_panel.html        Dashboard for stream management (includes economy sim/test controls).
  nowplaying.html           Now Playing overlay (450x120).
  youtube_player.html       Hidden YouTube audio player.
  god_overlay.html          God match data overlay.
  sound_alerts.html         Gamble dice roll animation + sound effects.
  tts.html                  TTS overlay for highlighted messages. Plays gTTS audio, shows message.
  kills.html                Kill/death event overlay. Shows kill type popups + K/D counter.
  voicelines.html           Voice line playback overlay. Shows MP4 animation + .ogg audio from channel point redemptions.
  deaths.html               Simple text overlay showing today's total death count. Polls /api/death_count every 1s.
  economy_ticker.html       1920x52 scrolling price bar for all gods. Shows on bot_ready.
  economy_match_live.html   320x280 live match panel with price chart, KDA, trade buttons. Shows on economy_god_detected.
  economy_match_end.html    420x400 post-match settlement report. Shows on match_end_economy.
  economy_dividend.html     420x170 dividend payout popup. Shows on dividend_paid.
  economy_leaderboard.html  260x360 top investors board. Shows on leaderboard_update.
  economy_tradefeed.html    320x340 live trade/dividend feed. Shows on trade_executed/dividend_paid.
  economy_portfolio.html    420x670 viewer portfolio card. Shows on portfolio_requested.
  factorio_cards.html       Card manager page (/factorio/cards). Map Streamloots card names to Factorio actions, test buttons, recently-played card list.
  overlay_client.js         Shared JS client library. WebSocket auto-reconnect, show/hide/update callbacks, hatPrice() helper, getColor() canvas helper.
  hatmas_theme.css          Shared CSS theme. Warm neutral palette, hat-icon utility classes, component styles.
  hat.png                   Hat currency icon used by hatPrice() helper across all overlays.
data/
  suggestions.json          Viewer suggestions from !suggest.
  song_queue.json           Current song request queue.
  song_history.json         All songs ever played.
  song_likes.json           Like counts and per-user tracking.
  song_blacklist.json       Blacklisted songs.
  song_state.json           Current playing song (overlay recovery after restart).
  smite_state.json          Last match result, daily W/L record (auto-resets each day).
  god_icons/                Reference god portrait icons from tracker.gg CDN (for portrait matcher).
  god_cards/                400x600 god card / splash art used by tools/build_thumbnail.py for YouTube thumbnails. Populated by tools/download_god_cards.py.
  godreq_queue.json         God request queue.
  godreq_history.json       Completed god requests with status.
  snap_stats.json           Snap statistics.
  spotify_token.json        Spotify access/refresh tokens.
  gamble_jackpot.json       Current jackpot pool.
  claude_history.json       Per-user Claude conversation history.
  kda_state.json            Persisted KDA state for mid-game restart recovery (K/D/A, match stats, timestamp).
  death_count.json          Daily death counter state (count + date). Auto-resets when the date rolls over.
  voiceline_rewards.json    Twitch reward ID → internal key mapping for voice line channel point redemptions.
  smite_voicelines/         Downloaded god voice lines (per-god folders with category subfolders). Generated by tools/download_voicelines.py.
  smite_animations/         God animation MP4s for voice line overlay (per-god folders, e.g. achilles/laugh.mp4).
  nsfw_cache.json           NSFW album art classification cache.
  twitch_token.json         Bot OAuth token (auto-refreshed).
  twitch_broadcaster_token.json  Broadcaster OAuth token (auto-refreshed).
  tts_audio/                Generated TTS MP3 files (auto-cleaned, keeps last 30).
  killdetect_debug/         Debug frame archives (timestamped subfolders, preserved for regression testing).
  test_fixtures/kda/        Canned KDA reader fixtures: <name>.png is a full 1920x1080 frame, sibling <name>.json declares expected (kda, per-group digits, verdicts, distance/margin bounds). Consumed by tools/test_kda_fixture.py — see "KDA fixture regression check" below for the format. Seeded with atlas_4_0_0_live_1080p (clean early-game Atlas frame, KDA=4/0/0).
  economy.db                SQLite database for god economy (WAL mode, async via aiosqlite).
  factorio_cards.json       Persisted card->action mappings (source of truth; seeded once from FACTORIO_CARD_MAP, edited via /factorio/cards).
  streamloots_events.jsonl  Raw Streamloots event log (appended by the hub for debugging/card mapping).
tools/
  obs_screenshot.py         Captures OBS "Smite 2" source screenshot for calibration. Saves full frame + KDA crop.
  download_voicelines.py    Downloads all god voice lines from the Smite fandom wiki. Uses curl_cffi for Cloudflare bypass.
  seed_economy.py           Seeds economy DB with realistic price history from tracker.gg stats. Usage: python tools/seed_economy.py [gods...] [--force]
  vod_detector.py           Offline VOD scan engine. Wraps core/kda_reader.py + core/god_matcher.py with a coarse-scan + binary-search refinement loop, streaming ffmpeg frame extraction, optional NVIDIA/CUDA HEVC hwaccel, lobby fast-skip, overlap event merging, and per-frame god portrait identification. Imported by extract_events.py and process_recordings.py — not run directly.
  extract_events.py         CLI that scans a folder of Smite 2 OBS recordings (1920x1080 60fps HEVC .mp4) and writes sibling <name>.events.json files consumed by HighlightBuilder.cs (Sony Vegas script). Supports --include, --no-refine, --hwaccel, --no-merge-overlaps, --enroll-templates, --workers N, --overwrite, --dry-run.
  process_recordings.py     End-of-stream orchestrator. Scans HatmasBot\recordings\ for unprocessed .mp4s, runs the detector, writes each <name>.events.json, and sorts the .mp4 + JSON pair into recordings\<God Name>\, recordings\mixed\, or recordings\unknown\ depending on which god(s) appeared. Renames to <stem>-N.<ext> using lowest-unused-integer per folder. Defaults are tuned for the daily flow (--hwaccel cuda, --include deaths) so a Stream Deck button can run it with no arguments.
  check_kda_region.py       One-off helper for visually verifying KDA crop coordinates against a reference frame.
  test_kda_fixture.py       Regression runner for the KDA reader. Walks data/test_fixtures/kda/, runs read_kda_with_details on each .png, diffs against the sidecar .json (kda tuple must match exactly; distance/margin numbers must stay within configurable slack). Prints PASS/FAIL per fixture, exits non-zero on any mismatch. Use --verbose for per-digit margins, --save-binary to dump the binarised 8x crop next to each fixture, or pass a substring to filter. New fixtures: drop a 1920x1080 frame + sidecar JSON (copy atlas_4_0_0_live_1080p.json as a template). Wire into a Stream Deck button to sanity-check the pipeline whenever a binarisation/threshold knob changes.
  capture_god_reference.py  Pulls a clean god-portrait crop out of a recording and saves it as a custom-overlay reference icon under Portrait_Source/. Used to permanently fix borderline matcher cases — if a recording lands in recordings/unknown/ because NVDEC subtly shifts pixel values vs software decode, capture one reference frame from the same decode pipeline and the matcher correlates near-1.0 on future scans.
  diagnose_god_detection.py Sample N frames from a recording and print, per frame, whether the gameplay/overlay checks pass and what the matcher's top 3 god candidates are with confidence scores. Saves each frame's portrait crop alongside so you can eyeball whether the matcher is even looking at the right pixels. Use this when a recording lands in recordings/unknown/ and you want to know why.
  sort_unknowns.py          Interactive walkthrough of recordings/unknown/. Suggests the most likely god per clip, prompts to confirm, then captures a portrait reference (via capture_god_reference) AND moves the .mp4 + .events.json into the right per-god subfolder with the standard {God}-N naming + source_video rewrite. Each captured reference accumulates as another fingerprint for that god, so the library learns from every clip you sort.
  process_vods.py           Sony Vegas pipeline orchestrator (Step 11 of SonyVegasTODO.md). Watches inbox/, runs extract_events.py if needed, drives Vegas via -SCRIPT:ProcessVideo.cs to build full-gameplay (1920x1080) + highlight (1080x1920) timelines per video, polls jobs/go.flag for keypress-to-render, moves processed clips to inbox/processed/. Refuses to start if vegas210.exe is already running (prevents orphan-window confusion).
  download_god_cards.py     Downloads 400x600 god card art (splash / loading-screen art) from wiki.smite2.com to data/god_cards/<slug>.png. Scrapes each god's wiki page for the og:image URL because the wiki uses three different filename conventions for the same asset. Supports --force/--check/--add/--only flags. Used by build_thumbnail.py.
  build_thumbnail.py        Preset-driven Pillow compositor that builds a 1280x720 YouTube thumbnail from a JSON preset + CLI inputs. Outputs flat composite PNG, per-layer transparent PNGs (drag-import into Paint.NET for layered editing), and optional layered PSD (if psd-tools is installed). Auto-launches Paint.NET on the result. Presets: thumbnail_presets/{1v1,1v2,2matches,2gods,single}.json.
  import_god_icons.py       Auto-imports candidate images from Custom_Icons_Inbox/ into Custom God Icons/. Smart-crops to 1:1 (top-biased), resizes to 512x512 PNG, and names per the build_thumbnail.py convention (<God>.png primary, <God>-1.png variants). Fuzzy-matches god names from filenames. Use --list-missing to see which gods lack a primary icon.
  youtube_live_badge.py     Apply/revert "LIVE NOW" badge on the last N YouTube thumbnails. Subcommands: apply (stream start), revert (stream end), status, auth. Caches originals locally at data/youtube_thumbnails/<video_id>.png. Requires one-time OAuth setup (downloads google-auth-oauthlib + google-api-python-client). Stream Deck pair: go_live.bat / go_offline.bat.
  test_web_session.py       11-test suite for core/web_session.py (tamper, expiry, forgery).
  discord_test.py           Standalone Discord send tester. Connects with DISCORD_BOT_TOKEN WITHOUT starting the bot, lists visible channels (--list), and sends a test message to a given channel id (defaults to DISCORD_DEFAULT_CHANNEL_ID). Exit 0 = sent. Use it to confirm the bot can post to a specific (e.g. private) channel. Wrapper: discord_test.bat.
  test_web_trade.py         19-test suite for website login + /api/trade (every guard, lock serialization, OAuth redirect).
  check_factorio_rcon.py    Standalone probe for the Factorio bridge: connects RCON, pings the hatmas-events mod, verifies remote calls round-trip. Run after hosting the save; exit 0 = bridge working.
  reconcile_stripe.py       Audit/repair CLI for priority-request payments. Subcommands: audit (diff Stripe vs priority_payments), unplayed (refund candidates), refund <session_id>.
  test_priority_request.py  16-test regression suite for the Stripe webhook money path. Run before touching priority_request.py; exits non-zero on any failure.
  dev_stub_site.py          Stdlib stub of the public webserver on port 8071: serves public/ pages with canned JSON for every API the landing + market pages call, so the front-end can be previewed and its JS exercised without starting the bot. Also wired as the "stub-site" config in .claude/launch.json.
  check_stream_ready.py     Pre-stream readiness check. Runs ~12 concurrent end-to-end probes (bot dashboard, both Twitch tokens, OBS WebSocket + Smite 2 source, MixItUp, tracker.gg, public webserver, hatmaster.tv, cloudflared service, disk space, asset library, Spotify token, SMITE 2 process). ~400ms full / ~150ms with --quick. Exit 0 = ready, 1 = at least one FAIL. Use check_stream.bat for one-press Stream Deck workflow.
go_live.bat                 Stream Deck: apply LIVE NOW badge on last 8 YouTube thumbnails. Pair with go_offline.bat at end of stream.
go_offline.bat              Stream Deck: revert LIVE badges. Idempotent and partial-failure tolerant.
start_factorio.bat          Stream Deck: launch Factorio with RCON bound to 127.0.0.1:27015. Reads FACTORIO_RCON_PASSWORD from core/config_local.py (single source of truth). After launch: Multiplayer -> Host saved game (RCON is NOT active in single player).
check_stream.bat            Stream Deck-friendly wrapper around tools/check_stream_ready.py. Runs the readiness check, prints colored report with hints, pauses 8s on success / 30s on failure so you can read it. Drop onto a Stream Deck "System: Open" button and press ~30s before going live.
discord_test.bat            Stream Deck-friendly wrapper around tools/discord_test.py. Passes its args straight through (e.g. `discord_test.bat --list`, `discord_test.bat <channel_id> "hello"`) and pauses at the end so you can read the OK/FAIL line.
Custom_Icons_Inbox/         Inbox folder for tools/import_god_icons.py. Drop candidate images here (any of .png/.jpg/.jpeg/.gif/.webp/.bmp/.tiff). Auto-created as needed. Successfully-processed files move to _processed/ subfolder.
thumbnail_presets/          JSON presets for build_thumbnail.py. Schema mirrors the .tune.json approach: layers[] rendered bottom-to-top with types solid/gradient/image/icon/text. Placeholders: {my_god}, {my_god2}, {vs_god}, {vs2_god}, {my_god_card}, {my_god2_card}, {vs_god_card}, {vs2_god_card}, {my_god_icon}, {my_god2_icon}, {vs_god_icon}, {vs2_god_icon}, {text}, {subtext}, {result}, {result2}, {kda}.
thumbnail_presets/_assets/  Background images / supporting PNGs referenced by presets. Drop joust_map.png here for the 2matches preset; auto-fallback to the brand gradient if a referenced file is missing.
thumbnails/                 Output landing zone for build_thumbnail.py. Each render produces <stem>.png + <stem>_layers/ + optionally <stem>.psd.
build_thumbnail.bat         Stream Deck-friendly wrapper around tools/build_thumbnail.py. Interactive prompt flow that adapts to the chosen preset: always asks for my god + skin variant + headline + subtext + KDA + flip toggle; conditionally asks for opposing god(s), second player god, second skin variant, and a second result based on which of 1v1 / 1v2 / 2matches / 2gods / single was picked. Pressing Enter on any optional field skips the corresponding --flag entirely (so blank result/kda just don't render). Flip toggles invert whatever the preset already does — type y to flip a god's card when its splash art is facing the wrong way. Drop the path into a Stream Deck "System: Open" button for one-press end-of-stream thumbnail creation.
process_recordings.bat      Stream Deck-friendly wrapper around tools/process_recordings.py. pushd's to repo root, runs the orchestrator, tees stdout+stderr to data/process_recordings.log, brief pause at the end so the summary stays on screen. Point a Stream Deck "System: Open" button at this file for one-click end-of-stream cleanup.
recordings/                 Drop-folder for new OBS recordings. Anything sitting in the root is treated as unprocessed. After process_recordings.py runs, files are sorted into per-god subfolders (recordings\Ymir\Ymir-3.mp4 etc.), recordings\mixed\ for multi-game sessions, and recordings\unknown\ for clips with no god identified. The .events.json travels with each .mp4 with a matching basename.
factorio_mod/
  hatmas-events/            Factorio 2.x mod (Lua). Viewer pets + boss biters, "hatmas" remote interface (future RCON surface for the bot), JSONL event outbox to script-output. Install via folder junction into %APPDATA%\Factorio\mods. See its README.md + the v2.8 section.
vegas_scripts/
  HighlightBuilder.cs       Sony Vegas Pro script that reads a *.events.json and auto-cuts a vertical highlight reel. Recurses from EVENTS_FOLDER (default C:\Users\james\Videos) so the newest .events.json across every god subfolder gets pre-selected in the file picker. Hand-rolled JSON parser (no Newtonsoft dependency) — only requires source_video and events fields, ignores any extras like gods_seen.
```

---

## EventSub Subscriptions (bot.py setup_hook + plugin on_ready)

All subscribed when both owner_id and bot_id are set:

1. ChatMessageSubscription - all chat messages
2. WhisperReceivedSubscription - incoming whispers
3. ChannelSubscribeSubscription - new subs (god token awards)
4. ChannelSubscribeMessageSubscription - resub messages (god token awards)
5. ChannelSubscriptionGiftSubscription - gift subs (god token awards to gifter)
6. ChannelRaidSubscription(to_broadcaster_user_id=OWNER_ID) - incoming raids (auto-shoutout)
7. ChannelPointsRedeemAddSubscription - channel point redemptions (voice lines, subscribed manually in `bot.py setup_hook` via `_manual_channel_points_subscribe()` — see TwitchIO bug section below)

### TwitchIO v3.2.1 Token Bug (Channel Point Subscriptions)

TwitchIO v3.2.1 has a bug where `subscribe_websocket()` picks the wrong token (bot token instead of broadcaster token) for `ChannelPointsRedeemAddSubscription`. This causes 403 "subscription missing proper authorization" even though the broadcaster token has the correct scopes (`channel:read:redemptions`, `channel:manage:redemptions`) and matching user_id. This was confirmed by: (1) validating the broadcaster token's scopes via the Twitch API right before the subscription attempt — scopes correct, user_id matches; (2) making the exact same subscription manually via direct Helix API call with the broadcaster token — succeeds immediately.

**Workaround:** `bot.py` uses `_manual_channel_points_subscribe()` which bypasses TwitchIO's token selection entirely. It grabs the WebSocket session_id from TwitchIO's internals (`self._websockets[owner_id][session_key].session_id`) and POSTs directly to the Helix EventSub API with the broadcaster token. The `event_custom_redemption_add` handler in bot.py still fires normally because TwitchIO routes incoming WebSocket events by type regardless of how the subscription was created.

Subscriptions 1-6 (chat, whisper, subs, resubs, gifts, raids) work fine via `subscribe_websocket()` in `setup_hook()` — the bug is specific to channel point redemption subscriptions.

---

## Commands Reference

### Basic (basic.py)
| Command | Access | Description |
|---------|--------|-------------|
| !hello | Everyone | Greets user |
| !uptime | Everyone | Bot uptime |
| !socials | Everyone | YouTube, Bluesky, Twitch links |
| !suggest <text> | Everyone | Submit a suggestion (60s cooldown, 500 char max) |
| !suggestions | Mods | Show count + last 3 suggestions |
| !clearsuggestions | Mods | Clear all suggestions |

### Smite (smite.py)
| Command | Access | Description |
|---------|--------|-------------|
| !god [name] | Everyone | Current god stats or look up any god |
| !stats | Everyone | Ranked Conquest K/D/A, win rate, KDA |
| !rank | Everyone | Current SR and rank tier |
| !match | Everyone | Check if in match with duration and live KDA |
| !winrate | Everyone | Ranked win percentage |
| !kda | Everyone | KDA ratio and KA/D |
| !damage | Everyone | Total/per-match/per-min damage |
| !team | Everyone | All players on team with gods and KDA |
| !lastmatch | Everyone | Last completed match results |
| !record | Everyone | Today's W-L and win rate percentage |

### Song Request (songrequest.py)
| Command | Access | Description |
|---------|--------|-------------|
| !sr <song/URL> | Everyone | Request song (2 per user, 4 for subs, 10min max) |
| !skip | Mods | Skip current song |
| !wrongsong | Everyone | Remove your most recent queued song |
| !songlist | Everyone | Top 5 queued songs |
| !song | Everyone | Current song with requester and likes |
| !like | Everyone | Like current song (one per user per song) |
| !mysongs | Everyone | Your total likes and most-liked song |
| !toprequester | Everyone | Top 3 by total likes received |
| !topsongs | Everyone | Top 5 most-liked songs |
| !voteskip | Everyone | Vote to skip (5 votes needed) |
| !songstatus | Everyone | Where your queued songs are + wait times |
| !blacklistsong | Mods | Blacklist current or specific song |

### God Request (godrequest.py)
| Command | Access | Description |
|---------|--------|-------------|
| !godrequest <god> | Everyone | Spend 1 God Token to request a god |
| !godreq <god> | Mods | Add god to queue free |
| !godqueue | Everyone | Next 5 gods in queue |
| !godlist | Everyone | Entire queue |
| !godtokens | Everyone | Check token balance |
| !godskip | Mods | Remove next god |
| !remove <pos> | Mods | Remove god at position |
| !godclear | Mods | Clear entire queue |

### Gamble (gamble.py)
| Command | Access | Description |
|---------|--------|-------------|
| !gamble <amount/all/half/quarter> | Everyone | Wager Hats (min 10, 10s cooldown) |
| !jackpot | Everyone | Show current jackpot pool |

### Economy (economy.py)
| Command | Access | Description |
|---------|--------|-------------|
| !buy [god] [amount\|all] | Everyone | Buy shares of a god with hats. Defaults to current god if omitted. |
| !sell [god] [amount\|all] | Everyone | Sell shares for hats. Defaults to current god if omitted. |
| !portfolio | Everyone | View your holdings with current value, P&L, and total net worth |
| !price [god] | Everyone | Current price, recent trend, volatility tier |
| !market / !stocks | Everyone | Top movers — gainers and losers |
| !dividend | Everyone | Most recent dividend payout info |

### OBS (obs.py)
| Command | Access | Description |
|---------|--------|-------------|
| !scene [name] | Mods | Switch scene or show current |
| !overlay <on/off/auto> | Mods | Control Now Playing visibility |

---

## Automated Features

### Smite Match Tracking
Polls tracker.gg with adaptive intervals (IDLE: 45s, SEARCHING: 30s, FOUND: 45s). Auto-announces match start, detects god, fires callbacks to other plugins. Updates OBS god portrait with fade. Creates Twitch predictions on match start. Auto-switches OBS scenes. Tracks daily W-L record (resets at midnight).

### Early God Detection (Portrait Matcher)
The tracker.gg API has a 2-5 minute delay before returning god data after a match starts. To show the god portrait immediately, the KillDeathDetector runs a portrait matcher on each game screenshot. It crops the in-game god portrait (bottom-center HUD, below K/D/A bar, region 635,942 to 715,1022 at 1920x1080) and compares it against a library of reference icons using HSV histogram correlation (cv2.compareHist with HISTCMP_CORREL). Acceptance uses dual criteria: above 0.80 absolute confidence, OR above 0.60 with a 0.20+ gap to the runner-up (margin-based acceptance for gods with low histogram correlation like Ymir's ice-blue palette which scores ~0.73). When a match is found, it fires immediately — setting the OBS portrait and updating the stream title. **No `is_in_match` guard** — the kill detector's gameplay screen check already ensures we're in actual gameplay, so portrait detection works in jungle practice, custom games, and early in real matches before tracker.gg responds. When tracker.gg eventually responds, it **reconciles** the detection: if tracker.gg returns the same god, only the background is updated; if tracker.gg returns a DIFFERENT god (misidentification), the portrait, title, and voiceline god are corrected automatically. Reference icons are downloaded from the tracker.gg CDN via `download_god_icons.py` and stored in `data/god_icons/`. The matcher is initialized in KillDeathDetector.on_ready and runs on each frame in the detection loop until a god is identified for the current match.

### Stream Title Placeholders
Templates in TITLE_TEMPLATE_GOD and TITLE_TEMPLATE_LOBBY support:
- `{god}` - current god name
- `{command}` - rotates through TITLE_COMMAND_ROTATION every 5min (configurable)
- `{record}` - daily W-L (e.g. "3-1")
- `{song}` - current song ("Title - Artist" or "No song playing")

### Song Request Automation
Polls Spotify every 3s. Pushes next song to queue 30s before current ends (gapless playback). Coordinates Spotify pause/resume with YouTube playback. Auto-skips blacklisted songs. Detects Spotify disconnection, slows polling, notifies chat on reconnect. Now Playing overlay auto-hide relies on the `is_playlist` field in every `update_now_playing()` call: playlist songs auto-hide after SR_PLAYLIST_AUTO_HIDE_SECONDS (8s), requested songs stay visible. All update paths (startup restore, YouTube playback, Spotify monitor) include this field. All overlay HTML responses include no-cache headers (`Cache-Control: no-cache, no-store, must-revalidate`) so OBS browser sources always serve the latest code without manual cache refreshes.

### Auto-Shoutout on Raid (bot.py)
Fires on ChannelRaidSubscription. Fetches raider's last game via GET /helix/channels. Sends chat message with raider name, viewer count, and last game. Calls POST /helix/chat/shoutouts for official shoutout card. 120s per-raider cooldown. Configurable min viewers (SHOUTOUT_MIN_VIEWERS). Controllable via `auto_shoutout` feature toggle.

### TTS for Highlighted Messages (bot.py + webserver + tts.html)
Detected in event_message by checking `payload.type == "channel_points_highlighted"`. Message text is sent to webserver's trigger_tts(), which spawns a background task that generates the MP3 via gTTS in a worker thread (asyncio.to_thread — gTTS makes a blocking HTTPS call to Google, which used to freeze the whole event loop per message) and then queues it. Overlay at /overlay/tts polls /api/tts_queue, plays audio via Web Audio API (AudioContext + decodeAudioData for OBS compatibility), shows purple message box with username + text + sound wave animation. Auto-hides after speech ends. Queue processes one at a time. Audio files stored in data/tts_audio/, auto-cleaned to last 30 files. Controllable via `tts_highlights` feature toggle. Max message length: 300 chars (TTS_MAX_LENGTH).

### God Request Auto-Complete
When smite plugin fires god_detected, godrequest plugin checks if detected god matches next in queue. If so, removes request, logs to history, announces in chat, advances OBS display.

### Kill/Death/Assist Detection (killdetector.py + digit_matcher.py + webserver + kills.html)
Starts automatically on bot launch and runs continuously — does not depend on SmitePlugin match state. Detects gameplay vs menus via HUD variance checks, so it works in jungle practice and real matches alike without waiting for the tracker.gg API. Grabs OBS screenshots of the "Smite 2" source via GetSourceScreenshot every 0.8s.

**Detection loop order:** gameplay check → god portrait identification → (gate: skip if god not identified) → overlay check → KDA reading → sanity checks → event firing.

**God portrait gating:** KDA reading only begins after the god portrait matcher has identified a god for the current match. Before that (lobby, god select, menus), only the gameplay check and portrait scanning run. When the match ends and `reset_match_stats()` fires, `_god_identified` resets to False and KDA reading stops until the next god is found. This eliminates noisy failed-read log spam during non-gameplay states.

**God identification validation:** To prevent lobby false positives (lobby screens can occasionally pass the gameplay check and produce a spurious portrait match), the same god must be identified on `_GOD_CONFIRM_REQUIRED` (3) consecutive frames before it is accepted. If a different god appears or no match is found, the confirmation counter resets. This ensures only real gameplay triggers portrait detection — a lobby screen won't consistently match the same god across multiple frames.

**Early match-end detection:** When the kill detector sees `NON_GAMEPLAY_FRAMES` (3) consecutive non-gameplay screens, it fires `on_gameplay_ended()` (before `reset_match_stats()` so stats are still available in callbacks) which calls `smite.force_end_match()`. This immediately clears the OBS god portrait/background, resets match state, and reverts the stream title — without waiting for tracker.gg's live API to drop the match (which can lag by several minutes). Works for both real matches (`is_in_match=True`) and portrait-only sessions like jungle practice (`_god_from_portrait=True`). A bounce-loop guard (`_force_ended_match_id`) prevents tracker.gg from re-entering the same match after force-end. The normal tracker.gg poll path no-ops when `is_in_match` is already False.

**Startup god portrait cleanup:** On bot startup, the smite plugin spawns a background task (`_startup_hide_god_image`) that polls every 0.5s (up to 15s) waiting for the OBS plugin's `client` attribute to become non-None (indicating a live OBS WebSocket connection). Once connected, it instantly hides the god portrait and background sources (opacity→0, visibility→False) without a fade animation. This clears any leftover portrait from a previous bot session. The background task approach is required because OBS is registered after the smite plugin, so OBS is not yet connected when smite's `on_ready()` runs.

**KDA region:** Reads K/D/A from the bottom-left HUD bar at pixel region `(35, 1033, 160, 1055)` on a 1920x1080 source — a 125x22px rectangle positioned below the item bar where it is always visible (never occluded by store or scoreboard overlays). The bar shows sword icon + kills, skull icon + deaths, hand icon + assists. The bar is semi-transparent — the background shifts as the camera moves, which is the root cause of recognition difficulty.

**Recognition pipeline:** Crops the KDA region, scales 8x (INTER_CUBIC), Otsu binarization (adaptive Gaussian fallback), connected component analysis (8-connectivity) to separate icons (h>88px) from digit blobs, groups digits into K/D/A by the two largest x-gaps. Each digit component is then recognized individually (per-component, not whole-group).

**Primary method — template matching (core/digit_matcher.py):** Each digit component is extracted, resized to 60x80, pre-filtered by hole count (0=must be 1/2/3/5/7, 1=must be 0/4/6/9, 2=must be 8), then XOR distance matched against reference templates in data/digit_templates/ (197 templates as of April 2026). Rejects if distance > 0.30 or margin < 0.02. Typically completes in 4-7ms per frame.

**Fallback method — per-component Tesseract OCR:** If template matching fails for any individual component, that component falls back to Tesseract PSM 10 (single character) with dilated and original image variants. Other components that matched templates keep their results. This handles double-digit numbers where the "1" in the tens place may not match any template. pytesseract is fully optional: all `import pytesseract` statements in core/kda_reader.py are guarded by `self._ocr_available`, so template-only matching works when it isn't installed. (June 2026 fix — an unconditional import inside `read_kda()`'s try block made every live read raise ImportError and silently return None whenever pytesseract was missing, killing the whole session's KDA tracking with nothing in the log.)

**Auto-collection:** After sanity checks pass, confirmed digit crops are saved as new templates (max 20 per digit). The template library grows automatically over time.

**State persistence:** KDA state saves to `data/kda_state.json` on every successful read (K/D/A values, match stats, timestamp). On startup, if the saved state is less than 30 minutes old (`STATE_STALE_SECONDS`), it's restored as the baseline. If older, the detector starts fresh. This allows mid-game bot restarts without losing KDA tracking.

**Startup validation:** The first `STARTUP_REQUIRED_READS` (3) consecutive reads must all agree before the baseline is accepted. If restored state exists, the validated read is compared against it — if live reads >= saved values, the delta is applied; if live reads < saved values (new match or bad data), it starts fresh.

**Sanity checks:** Rejects any frame where KDA decreases (misread). Rejects any frame where a single value jumps by more than `MAX_KDA_JUMP_BASE` (5) + seconds_elapsed * `MAX_KDA_JUMP_PER_SEC` (1/3) — scales with time since last successful read. Non-gameplay screen detection checks HUD area for low std/mean — if missing for 3 consecutive frames, assumes game ended and resets stats.

**Multi-kill classification:** Tracks kill timestamps within MULTIKILL_WINDOW (10s) to classify double, triple, quadra, and penta kills. Batched kills (+2 or more from missed frames) clear the window and fire as player_kill.

**Chat announcements:** Optional toggle sends Twitch chat message on each kill, death, or assist with updated KDA line.

**Callbacks:** on_kill, on_multikill, on_death, on_assist — set in main.py to push events to the webserver overlay queue.

**Sustained-failure alerting (June 2026):** when the god is identified and gameplay is active but KDA reads keep failing, every `READ_FAILURE_ALERT_EVERY` (25) consecutive failures the detector logs a non-debug WARNING with the reader's `failure_reason` (from `read_kda_with_details`) and saves a full-frame snapshot to `data/detector_snapshots/readfail_<ts>/fullframe.png` + `reason.txt` (rate-limited to one per `READ_FAILURE_SNAPSHOT_MIN_INTERVAL` = 300s). Snapshots use the same layout fixtures are built from, so a failing frame can be promoted straight into `data/test_fixtures/kda/`. Previously a fully-broken reader looked identical to lobby idling in the logs.

**Debug:** set_debug(True) saves crops to data/killdetect_debug/ (archived into timestamped subfolders on each session start). File logging writes all [KillDetector] output to data/killdetect.log (overwritten per session, flushed on every write).

**Overlay:** /overlay/kills polls /api/kill_events, shows popup (red for kills, gold for multi-kills, gray for deaths) with K/D counter. Events and stats available via /api/kill_stats (includes running, debug, announce_chat, ocr_available, last_kda, last_read_ago). Test via actions: test_kill (with optional kill_type param), test_death. Debug panel actions: start_kill_detect, stop_kill_detect, kd_toggle_debug, kd_toggle_announce.

**Dependencies:** Pillow, opencv-python, numpy. Optional: pytesseract + Tesseract binary (fallback OCR, not needed if template library has full digit coverage).

**Calibration tool:** `tools/obs_screenshot.py` captures an OBS screenshot from the exact "Smite 2" source at 1920x1080 and saves a KDA crop for coordinate verification.

**KDA fixture regression check (`tools/test_kda_fixture.py` + `data/test_fixtures/kda/`):** Canned frame + expected-output pairs that exercise the whole pipeline end-to-end (region crop, binarisation, icon filter, digit grouping, template matcher) on real game stills. Each fixture is two files in `data/test_fixtures/kda/`: `<name>.png` (a full 1920x1080 frame, usually copied out of `data/detector_snapshots/<ts>/fullframe.png` from a session where the live reader behaved correctly) and `<name>.json` (the sidecar declaring `expected.kda` plus optional per-group digit assertions, per-digit `max_distance` / `min_margin` floors, and `tolerance.*_slack` to absorb harmless drift). The KDA tuple must match exactly; distance/margin numbers only have to stay within the slack values, so binarisation tweaks won't break fixtures unless they meaningfully degrade confidence. Run `python tools/test_kda_fixture.py` for the whole suite (exits 0 if every fixture passes, 1 otherwise), pass a substring to filter (`python tools/test_kda_fixture.py atlas`), add `--verbose` for per-digit margins, or `--save-binary` to dump the binarised 8x crop next to each fixture for eyeballing. Adding a new case: drop the PNG, copy `atlas_4_0_0_live_1080p.json` as a template, edit `expected.kda` + `expected.groups[*].concatenated` to the truth, then run the tool once with `--verbose` to fill in `max_distance` / `min_margin` from observed values. This is the canary for binarisation/threshold changes — if you tighten the saturation mask or move `KDA_REGION`, run the suite first.

### Voice Line Redemptions (voicelines.py + webserver + voicelines.html)
Three channel point rewards (God Joke 500pts, God Taunt 500pts, God Laugh 200pts) auto-created on Twitch on first startup (requires `channel:manage:redemptions` broadcaster scope, re-auth with `python -m core.auth --broadcaster` if needed). Reward IDs are persisted to `data/voiceline_rewards.json` so they're not recreated each launch. The EventSub subscription is created manually in `bot.py setup_hook()` via `_manual_channel_points_subscribe()` because TwitchIO v3.2.1 picks the wrong token for this subscription type (see EventSub section above). When redeemed, picks a random .ogg from the current god's voice line folder (e.g. `data/smite_voicelines/achilles/jokes/`). If an MP4 animation exists in `data/smite_animations/<god>/joke.mp4` (or `laugh.mp4`, `taunt.mp4`), it plays alongside the audio in the OBS overlay. The overlay at /overlay/voicelines polls /api/voiceline_events, plays video+audio together, fades out when both finish (15s safety timeout). Events queue so rapid redemptions play in sequence. Current god is tracked via the kill detector's portrait matcher and the SmitePlugin god detection — the last known god is used as a fallback between matches (defaults to Sylvanus on a fresh install). Voice lines are pre-downloaded via `python tools/download_voicelines.py` (uses curl_cffi for Cloudflare bypass).

**Dynamic reward prompts:** The reward prompts (descriptions viewers see when clicking the reward) auto-update on Twitch whenever the current god changes. Templates in `REWARD_DEFS["prompt_template"]` use `{god}` as a placeholder — "Play a joke for {god}", "Play a taunt for {god}", "{god} laughs". `set_current_god()` schedules `_update_reward_prompts()` as a background task whenever a new god is detected, which PATCHes each reward via `PATCH /helix/channel_points/custom_rewards`. On startup, `_ensure_rewards()` also calls the prompt updater so restored state from disk is reflected on Twitch. Note: Twitch's API does not allow updating the reward image/icon, only the prompt, title, cost, color, and other text fields.

### Gamble Sound + Visual Alerts
Triggers on wagers >= 100 Hats (GAMBLE_ALERT_MIN_WAGER). Jackpot always triggers. OBS overlay at /overlay/sound_alerts shows dice roll animation (1.5s cycling), then result with color-coded popup. Sounds synthesized via Web Audio API. Multiple rapid gambles queue server-side (/api/gamble_queue drain pattern). Auto-hides after 5s.

### God Economy / Hatmas Market (plugins/economy/ + 7 overlays)
Stock-market-style system where viewers invest Hats in Smite 2 gods as shares. God prices move based on Hatmaster's tracker.gg-verified match performance. Full details in `HATMAS_MARKET_AIRTIGHT_DESIGN.md` (the authoritative source for the current design — replaces the older notes that referenced a single monolithic `economy.py`).

**Package layout:** `plugins/economy/` is a package, not a single file. Concern-specific mixins: `plugin.py` (lifecycle), `db.py` (schema + cache), `match.py` (match lifecycle + backfill + settle_match), `dividends.py` (start dividend + catch-up dedup), `ticking.py` (cosmetic K/D/A ticks), `trading.py` (buy/sell), `helpers.py` (chatters, broadcaster-live gate), `mixitup.py` (currency API), `fair_value.py` (formula), `overlays.py`, `god_names.py`, `commands.py`, `api.py`, `testing.py`. `from plugins.economy import EconomyPlugin` still works because the package re-exports it.

**Database:** SQLite via `aiosqlite` (async, WAL mode). Tables: `god_prices` (current prices, games played), `price_history` (timestamped for sparklines), `portfolios` (per-viewer holdings with avg cost), `transactions` (buy/sell/dividend/free_share), `dividends` (payout records, with `match_id` for backfill catch-up dedup), `processed_matches` (settlement dedup keyed on tracker.gg `match_id`, includes `was_live_at_settle` audit column). Path: `data/economy.db`.

**Match lifecycle (airtight design — May 2026):** tracker.gg confirms match (SEARCHING → FOUND with a real `match_id`) → smite plugin fires `on_match_confirmed` → economy pays 5% dividend to holders (gated on broadcaster being live on Twitch) → COSMETIC live ticks (+1.5% per kill, -2% per death, +0.5% per assist) animate the overlays from a locally-computed price but NEVER mutate `god_prices.price` or `price_history` → match ends → tracker.gg's canonical W/L + KDA settles the price via the fair-value formula in `match.py:settle_match()`, dedup'd via `processed_matches.match_id` → 1 free share to current chatters + match-end overlay popup + leaderboard refresh (all gated on broadcaster being live AT SETTLEMENT TIME, regardless of whether settlement came via the immediate kick from `on_match_result` or the scheduled 5-min backfill loop). Portrait-only signals (jungle practice, custom games, lobby false-positives) animate the overlay god portrait but never trigger dividends, free shares, or persisted price changes.

**Visual vs authoritative events:** `smite_plugin.on_god_detected` fires from BOTH the OBS portrait matcher AND the tracker.gg poll. The economy deliberately does NOT subscribe — visual-only consumers (voicelines, godrequest, OBS title, overlays) do. `smite_plugin.on_match_confirmed` (added in the airtight pass) fires only from the tracker.gg path, only when there's a real `match_id`. The economy subscribes here. Payload: `{match_id, god, team}`.

**Twitch viewer list:** Free share distribution uses the Helix `GET /helix/chat/chatters` API with pagination (up to 1000 per page) to get all users connected to chat, including lurkers. Requires `moderator:read:chatters` broadcaster scope. Falls back to TwitchIO's IRC-based `channel.chatters` if the API call fails. The `token_manager` is passed to EconomyPlugin from main.py for authenticated Helix calls.

**Broadcaster-live gate:** `_HelpersMixin._is_broadcaster_live()` reads `StreamStatusPlugin.get_status()["is_live"]` and is consulted at: (1) `on_match_confirmed` for the start-of-match dividend; (2) `settle_match` for free shares, leaderboard refresh, match-end overlay, and the catch-up dividend. The simulator in `testing.py` bypasses the gate via `self._sim_force_live = True`.

**Backfill catch-up dividend:** if the live path missed the start dividend (bot was offline at match-start, or broadcaster wasn't live then), `settle_match` checks `_dividend_already_paid(match_id)`. If False AND broadcaster is currently live, it pays the dividend at settlement time. Normal case (live path already paid at match start) skips this — the SELECT finds the existing row keyed on `match_id`.

**Price formula:** Fair-value formula in `plugins/economy/fair_value.py:calculate_fair_value(wins, losses, K, D, A)`. Asymmetric: above 50% winrate scales aggressively with games (so a 100-game 80% comfort pick is worth meaningfully more than a 2-game 100% sample); below 50% stays bounded. See `HATMAS_MARKET_AIRTIGHT_DESIGN.md` and the constants block at the top of `fair_value.py` for tuning.

**No transaction fees.** `ECONOMY_TRANSACTION_FEE` was removed from config in the airtight-economy pass. The `fee` column in `transactions` is preserved at 0 for schema compatibility.

**Overlay events emitted:** `economy_god_detected`, `god_stock_update`, `match_end_economy`, `dividend_paid`, `trade_executed`, `portfolio_requested`, `leaderboard_update`. All driven by the centralized overlay manager (`core/overlay_manager.py`) with rules in `core/overlay_rules.json`.

**Shared overlay infrastructure:**
- `overlays/overlay_client.js` — WebSocket client with `HatmasOverlay.connect()`, `onShow`/`onUpdate`/`onHide` callbacks. `HatmasOverlay.hatPrice(value, opts)` renders hat values with inline hat icon (e.g., `🎩118 hats`). `HatmasOverlay.getColor('--hm-var')` resolves CSS variables for canvas drawing. All overlays start hidden (`display: none` on container at connect time) and only appear when the overlay manager sends a `show` event. On show, CSS animations are replayed via the `_replayAnimations()` method (removes animation, forces reflow, restores — standard trick to restart CSS animations). On hide, container returns to `display: none`.
- `overlays/hatmas_theme.css` — Shared CSS variables, hat-icon utility classes (`.hat-icon`, `.hat-icon-sm`, `.hat-icon-lg`), component styles.
- `overlays/hat.png` — Hat currency icon used by `hatPrice()`.
- Icon paths: `/icons/custom/{GodName}.png` (512x512) → `/icons/gods/{godname}.png` (96x96) → graceful fallback.
- **Always-on overlay reconnect:** When an overlay connects via websocket, the webserver checks if that overlay is already marked visible in the overlay manager (e.g., ticker and deaths counter after `bot_ready`). If so, it immediately re-sends the `show` event. This ensures always-on overlays survive OBS browser source refreshes without needing a bot restart.

**7 production overlays:** economy_ticker (scrolling bar), economy_match_live (live panel), economy_match_end (settlement report with god:mode subtitle, free share line, session movers), economy_dividend (payout popup), economy_leaderboard (top investors), economy_tradefeed (trade feed showing shares bought/sold, not hat amounts), economy_portfolio (viewer portfolio with Twitch profile picture via Helix API, hat icon rendering, sparklines). All use shared theme, font stack (BigNoodleTitling + Poppins + JetBrains Mono), and websocket client. All display whole number hats only (no decimals).

**Voiceline triggers:** Currently disabled (early `return` in `_trigger_voiceline()`) due to inconsistent god-to-voiceline file naming across 127 gods. When re-enabled: dividend → "You Rock!", win → "Awesome!", loss → "That's too bad!", big spike → "Woohoo!", big crash → "Help!".

**Seeder tool:** `python tools/seed_economy.py [gods...] [--force]` fetches real stats from tracker.gg and simulates match-by-match price history. Uses mean reversion (2% toward 200 target), ±8% match swing cap, and 1000 hat ceiling to prevent exponential blowup. Falls back to hardcoded stats for Ymir, Geb, Sylvanus.

**Control panel integration:** Economy simulator with force-override checkbox. 7 individual test buttons (TICKER, DIVIDEND, LEADERBOARD, PORTFOLIO, TRADE FEED, MATCH END) that emit realistic sample data through the overlay system. RELOAD PRICES button reloads in-memory price cache from the database without a bot restart (use after running the seeder).

**API endpoints:** `GET /api/economy/market`, `GET /api/economy/portfolio?user=`, `GET /api/economy/leaderboard`, `GET /api/economy/price/{god}`.

**_db guard pattern:** All 6 chat commands and key event hooks check `if self._db is None` before database operations. This prevents crashes if `aiosqlite` is not installed. The dependency must be installed manually: `pip install aiosqlite`.

### Daily Death Counter (deathcounter.py + deaths.html)
Tracks total deaths per day across every gameplay session the kill detector sees (practice, custom, ranked, all count). State lives in `data/death_count.json` as `{count, date}`. The `_check_day_reset()` helper compares the stored date against today's date on every increment and getter, so the counter auto-resets at midnight without needing a background task. `main.py` wires the existing `kd.on_death` callback to also call `death_counter.increment()`, meaning the counter stays in lockstep with the kill detector. The overlay at `/overlay/deaths` is a transparent text widget (red "Deaths Today" label + large white number) that polls `/api/death_count` every 1 second. Drop the URL into OBS as a browser source and position it anywhere — no dashboard configuration needed.

---

### Streamloots Card Events (plugins/streamloots.py)
Event hub for Streamloots card redemptions, chest purchases, and gifts. Streamloots has no official developer API; the plugin listens to the alert overlay's Server-Sent Events stream at `https://widgets.streamloots.com/alerts/<STREAMLOOTS_ALERT_ID>/media-stream` - the same unofficial-but-stable surface MixItUp and Firebot use. One JSON event arrives per SSE `data:` block. Card fields are read BY NAME ("username", "message", "longMessage", "rarity", "quantity", "giftee"), never positionally - field order is not guaranteed. A gift is a purchase event whose fields include "giftee". The listen loop auto-reconnects with exponential backoff (5s doubling to 120s cap, reset on successful connect). Every raw event is appended to `data/streamloots_events.jsonl` for debugging and for building card maps from real payloads.

Consumers subscribe in main.py via the listener-list pattern (`streamloots.add_redemption_listener(cb)` etc.) and receive normalized dicts - shapes documented in the plugin docstring. The `streamloots` feature toggle gates dispatch only (the connection stays up; events are dropped while toggled off). Empty STREAMLOOTS_ALERT_ID disables the plugin at on_ready (fail closed). Dashboard testing without spending real cards: POST /api/action with `action=test_streamloots` (optional card/user/message/rarity params) pushes a fake redemption through the real dispatch path via `simulate_redemption()`. The control panel has a TEST STREAMLOOTS button plus a status line fed by `/api/state` (the state payload includes `streamloots: get_status()` — configured / connected / config_error / events_received / last_event_at / listener counts). Robustness: a 15-minute `sock_read` deadline acts as a dead-connection watchdog (a half-open socket otherwise looks connected forever while cards go missing; idle reconnects are routine and skip backoff growth), and 4xx responses other than 429 set `config_error` and stop retrying (wrong/revoked alert ID), while 5xx/429 keep exponential backoff.

First consumer: the Factorio integration. The game-side mod lives at `factorio_mod/hatmas-events/` (viewer pets, boss biters - see its README); the bot side is `plugins/factorio/` (registered as plugin 19), which subscribes to redemptions in main.py and maps card names to mod remote calls via FACTORIO_CARD_MAP.

## Offline VOD Highlight Pipeline (tools/extract_events.py + tools/vod_detector.py)

A standalone CLI, completely separate from the live bot, that scans a folder of OBS recordings and emits sibling `<name>.events.json` files describing every kill/death/assist timestamp. The companion Sony Vegas script `HighlightBuilder.cs` consumes these JSONs to auto-cut highlight reels. Runs offline, reads nothing from the bot's runtime, and only *reads* from `data/digit_templates/` unless `--enroll-templates` is explicitly passed.

**Goal:** turn "I played a 24-minute ranked game" into a Vegas timeline with one clip per notable moment, fully unattended, in well under realtime.

**Usage:**
```
python tools/extract_events.py "C:\Users\james\Videos" [flags]
```

Common flags:
- `--include deaths,assists` — by default kills only; `all` picks up every event type
- `--overwrite` — re-scan even if `.events.json` already exists
- `--dry-run` — list what would be scanned without writing anything
- `--hwaccel cuda` — NVIDIA GPU HEVC decode (≈2.6x faster; `d3d11va` / `dxva2` / `auto` also accepted). Software decode is the default so batch runs behave identically to the live plugin.
- `--no-refine` — skip the binary-search refinement step; emits events at the coarse-scan timestamp with widened pre/post windows so the clip still covers the real moment. Much faster for bulk scans.
- `--no-merge-overlaps` — disable the event-merging pass (see below) and keep every kill/death/assist as its own clip
- `--enroll-templates` — save confirmed digit crops back into `data/digit_templates/`, just like the live detector. Gated behind KDA-level sanity guards (see below) so bad reads never poison the library.
- `--workers N` — run N videos concurrently in subprocesses. 3-4 is the sweet spot on a typical gaming rig. Not compatible with `--enroll-templates` (the template folder would race).
- `--coarse 5.0` / `--precision 0.2` — tune coarse scan interval and refinement precision
- `--no-lobby-skip` — disable the pre-match lobby sparse-sampling optimization
- `--no-ffmpeg-crop` — disable the ffmpeg-side HUD-strip crop (debugging only)

### Architecture

**`core/kda_reader.py`** is the shared KDA-reading brain used by both the live `plugins/killdetector.py` and the offline `tools/vod_detector.py`. It owns: gameplay/overlay detection, KDA cropping, 8x upscale + Otsu binarization, per-digit template matching (with hole-count pre-filter + XOR distance), Tesseract PSM 10 fallback, and the enroll/discard API (`enroll_last_read()` / `discard_last_read()`). Both the live bot and the VOD tool see identical recognition behavior because they call the same reader.

**`tools/vod_detector.py`** is the offline scan engine:
- `VodDetectorOptions` dataclass holds every knob: `hwaccel`, `merge_overlaps`, `enroll_templates`, `enable_god_detection`, `god_icons_dir`, `include_deaths/assists`, `coarse_sec`, `precision_sec`, `no_refine`, `no_lobby_skip`, `no_ffmpeg_crop`, `progress_callback`, and paths to ffmpeg/ffprobe/tesseract.
- `_stream_raw_frames()` spawns a single ffmpeg process per video that pipes raw `rgb24` frames at `1/coarse_sec` FPS (with optional ffmpeg-side HUD crop and `-hwaccel <value>` inserted before `-i`). Python reads the pipe in-process — no per-frame ffmpeg spawn.
- `detect()` walks the coarse stream, runs sanity guards at every step, and binary-searches each KDA change down to `precision_sec` before emitting events. After the scan finishes, `detector.gods_seen` holds the de-duplicated list of gods identified in the recording (ordered by first-confirmation time).
- Multi-kill classification reuses the live detector's 10s window rule (double/triple/quadra/penta).

**God identification during the scan:** every gameplay frame the detector reads is also fed to `core/god_matcher.GodMatcher.identify()`. The matcher takes a new `crop_origin=(0,0)` kwarg (default preserves live-plugin behavior) so PORTRAIT_REGION resolves correctly inside the ffmpeg-side HUD-cropped strip. The same 3-consecutive-frame confirmation rule the live detector uses gates acceptance — a candidate has to come back from `identify()` on three consecutive gameplay frames before it lands in `gods_seen`. After confirmation the counter resets, so a multi-match recording (Ymir → match end → Loki) confirms each god in turn. The matcher is lazy-initialized on the first `detect()` call, so importing the module costs nothing if you never scan; if `data/god_icons/` is missing or fails to load, scanning continues without god identification and `gods_seen` stays empty.

**Dual-library matching (in-game CDN + custom OBS overlay):** the bot's OBS plugin overlays a custom god image (`Custom God Icons/{GodName}.png`, 512x512) on top of the in-game HUD portrait region during a match. The live detector reads from the **"Smite 2" OBS source directly** (game capture, pre-composite) so it never sees this overlay — `data/god_icons/` is enough for live use. The recording on disk is the **final composited** scene, however, so the same portrait region is covered by the custom overlay for most of the runtime. To make recordings identifiable, `GodMatcher` now supports a second icon source via the `overlay_icons_dir` constructor param. `vod_detector.py` passes `<repo>/Custom God Icons` by default (configurable via `VodDetectorOptions.god_overlay_icons_dir`; pass `Path("")` to disable). Internal storage changed to `dict[god_name, list[hist]]` so each god can carry both an in-game-CDN fingerprint and a custom-overlay fingerprint; `identify()` takes the max correlation per god before ranking distinct gods, so margin-based acceptance still works correctly. Files in `Custom God Icons/` whose stem doesn't map to a god in the base library (skin variants like `Ymir Frostbringer.png`, unrelated assets) are silently skipped — no skin-to-base-god mapping required. Live `KillDetector` is unaffected (no `overlay_icons_dir` passed → matcher loads only the base library, same as before).

**`tools/extract_events.py`** is the CLI driver:
- Discovers `.mp4` files, skips any that already have a `.events.json` (unless `--overwrite`), and prints `[idx/total]` progress lines with live `\r`-updated percentages.
- Serializes output in the same human-friendly JSON style HighlightBuilder.cs expects — top-level pretty, each event on its own single line. Now also emits a top-level `"gods_seen": [...]` field next to `source_video` and `events`. HighlightBuilder's regex parser ignores unknown fields, so the addition is fully backwards-compatible with the existing Vegas script.
- Per-video summary line includes the identified god(s) when any were confirmed.
- Summary print at the end: total kills / deaths / assists, elapsed wall time, realtime multiplier.

### Event merging (`--no-merge-overlaps` to disable)

In-game, a kill and the death that follows it are frequently within a few seconds of each other, so their pre/post clip windows overlap. Importing that into Vegas as two separate events produces two overlapping clips of the same footage — wasteful, and awkward to edit. The detector now collapses any events whose pre/post windows overlap into a single wider event:
- Groups are built greedily by sweeping events in chronological order.
- The merged event anchors on the highest-priority type in the group — kill > death > assist — so a "kill → death" trade still surfaces as a kill in HighlightBuilder.
- `pre_sec` and `post_sec` are widened to cover the earliest start and latest end in the group.
- The `note` is updated to show the chronological sequence, e.g. `"kill + death"` or `"first blood (kill + death)"`.
- Runs **before** the include filter so a merged kill+death survives even a kills-only run.

### Clip window tuning

Post-Vegas review indicated the previous clip windows clipped the build-up slightly. Constants in `vod_detector.py` were bumped +2s on both sides:
- `DEFAULT_PRE_SEC = 7.0` / `DEFAULT_POST_SEC = 6.0` (was 5.0 / 4.0)
- `MULTIKILL_POST_SEC = 8.0` (was 6.0)
- `NO_REFINE_PRE_SEC = 9.5` / `NO_REFINE_POST_SEC = 8.5` (was 7.5 / 6.5; these compensate for the ±half-coarse-interval timestamp uncertainty when refinement is skipped)

### Template enrollment (gated behind KDA-level sanity guards)

`--enroll-templates` enables saving confirmed digit crops back to `data/digit_templates/`, mirroring the live detector. The gotcha is that the per-digit template matcher can successfully label an obviously-wrong KDA (e.g. `64/0/4` when the ones column glued into the next field, or `3/4/97` when an overlay glyph slipped into the crop). Those get rejected at the KDA level by `detect()`'s sanity guards, but if the digits were auto-enrolled during matching the template library would silently drift.

Fix: `_read_from_image()` never commits samples on its own anymore. `detect()` calls `_commit_sample(trustworthy=True|False)` at every branch of its loop — baseline, clean 0/0/0 reset, no-change, and real forward changes are `True`; partial decreases, max-jump outliers, and anything that fails the KDA-level guards are `False`. Refinement reads in `_read_at()` / `_read_with_offsets()` always discard via `reader.discard_last_read()` since they're mid-transition samples by design. Net effect: enrollment is safe to leave on for any recording, and misreads that the per-digit matcher would have blessed can no longer poison the template library.

Practical note: `enroll_last_read()` already silently dedupes near-identical crops, so running `--enroll-templates` on a recording where the library has strong coverage typically leaves the template count unchanged (e.g. a ~24-min scan kept the library at 197 entries). That's expected — remaining Tesseract fallbacks at current coverage are matcher-threshold-bound, not template-coverage-bound.

### Performance

Reference recording is a 1434s (≈24min) 1080p60 HEVC ranked conquest. Progression:
- Original per-frame ffmpeg spawns: ~6-8 minutes per video.
- Task #13 (ffmpeg-side crop), #14 (lobby fast-skip), #15 (`--no-refine`), #8 (streaming frame pipe): down to ~293s (≈5x realtime). Python was no longer the bottleneck — ffmpeg decode was.
- Task #16 (`--hwaccel cuda`): ~110s (**≈13x realtime**, ≈2.6x faster than software decode on the same machine). This is the final production configuration.

Typical final run for that recording: 7 kills, 5 deaths detected; one kill+death trade merged into a single event; max-jump guard rejected a `3/4/97` OCR blowup at t≈935s without polluting the template library.

### Concurrency and working while scans run

`--workers N` runs videos concurrently by handing each one to its own subprocess with its own `KdaReader` instance. Because each worker owns its own template cache, `--enroll-templates` is deliberately blocked in multi-worker mode — the shared `data/digit_templates/` folder would race. For bulk scanning (post-stream, overnight), leave enrollment off and push `--workers 3` or `--workers 4`.

Scanning a video is a pure read of the underlying .mp4. It's safe to render, scrub, or re-import the same file in Sony Vegas while `extract_events.py` is working through the folder — ffmpeg opens its own read-only handle.

---

## End-of-Stream Recording Sorter (tools/process_recordings.py + process_recordings.bat)

A one-button workflow that takes the VOD pipeline above and wraps it for daily end-of-stream cleanup. James drops new OBS recordings into `recordings\`, presses a Stream Deck button, and the script auto-scans, names, and files everything.

**Goal:** turn "I have five .mp4s from tonight's stream sitting in a folder" into "five neatly-sorted clips in `recordings\Ymir\`, `recordings\Loki\`, and `recordings\mixed\`, each with its `.events.json` ready to import into Vegas."

### Folder layout

```
HatmasBot\recordings\                  Drop folder. New recordings go here.
HatmasBot\recordings\<God Name>\       Single-god recordings, e.g. Ymir\Ymir-1.mp4 + Ymir-1.events.json
HatmasBot\recordings\mixed\            Multi-game sessions covering 2+ gods.
HatmasBot\recordings\unknown\          Recordings where no god was confirmed (short clips, demos, menus).
```

Anything sitting in the `recordings\` root is treated as unprocessed. Anything in a subfolder is considered already sorted and ignored — no marker files needed, the directory structure IS the marker.

### Routing rules

After `vod_detector` finishes scanning a recording:

| `gods_seen`                | Target folder              | Filename stem |
|----------------------------|----------------------------|---------------|
| `["Ymir"]`                 | `recordings\Ymir\`         | `Ymir`        |
| `["Hou Yi"]`               | `recordings\Hou Yi\`       | `Hou Yi`      |
| `["Ymir", "Loki"]`         | `recordings\mixed\`        | `mixed`       |
| `["Ymir", "Loki", "Geb"]`  | `recordings\mixed\`        | `mixed`       |
| `[]` (no god confirmed)    | `recordings\unknown\`      | `unknown`     |

The `.mp4` and its `.events.json` move together, both renamed to `<stem>-N.<ext>` where `N` is the **lowest positive integer not already used** in the target folder. So if `Ymir-1.mp4` and `Ymir-3.mp4` exist, the next Ymir recording lands as `Ymir-2.mp4` (gaps fill before the sequence extends). The numbering is case-insensitive on Windows so `ymir-2` and `Ymir-2` can't both claim slot 2.

The `.events.json` is rewritten on emit so `source_video` points at the new absolute path — HighlightBuilder reads that field directly and calls `File.Exists()` on it, so the rename has to be reflected there. The `gods_seen` list and the events array carry through unchanged.

### Order of operations per recording

1. Scan with `VodDetector`, capturing `events` and `detector.gods_seen` in memory.
2. Decide the target subfolder + filename stem from `categorize(gods_seen)`.
3. Compute `next_index(target_dir, stem)` to pick the new number.
4. `mkdir` the target subfolder if it doesn't exist.
5. `shutil.move()` the .mp4 to the target with the new name.
6. Render the .events.json with the new absolute `source_video` path and write it next to the moved .mp4.

The move happens before the JSON is written, so the JSON we emit can never reference a path that doesn't exist on disk. If the move fails for any reason, the JSON is never written and the original `.mp4` stays put for retry.

### CLI defaults

Tuned for the daily flow so the Stream Deck button can run with no arguments:

- `--source recordings\` (relative to repo root)
- `--include deaths` (kills + deaths in the output, matching the typical Vegas highlight workflow)
- `--hwaccel cuda` (NVIDIA GPU HEVC decode; pass `--hwaccel none` to force software decode)
- god detection on, lobby skip on, ffmpeg-side crop on, overlap merging on

Override flags are kept identical in spelling to `extract_events.py` so the muscle memory is shared: `--no-refine`, `--no-merge-overlaps`, `--no-lobby-skip`, `--no-ffmpeg-crop`, `--enroll-templates`, `--coarse`, `--precision`, `--ffmpeg`, `--ffprobe`, `--tesseract`, `--data-dir`, `--dry-run`, `-v/--verbose`.

### `process_recordings.bat` (Stream Deck wrapper)

Lives at the repo root. Single-purpose batch file that:

1. `pushd "%~dp0"` so it always operates from the HatmasBot repo root regardless of where Stream Deck launches it from.
2. Appends a timestamp banner to `data\process_recordings.log`.
3. Runs `python tools\process_recordings.py` and tees both stdout and stderr to the log via PowerShell's `Tee-Object`. Live progress still shows in the console; the log gets a permanent record.
4. Logs the exit code and a closing timestamp.
5. `timeout /t 5` so the summary stays on screen briefly before closing.

To wire it up: drag the `.bat` path into a Stream Deck "System: Open" or "Multimedia: Run" button. One press at end of stream, hands-free sort + JSON-emit for every recording sitting in the queue.

For a fully silent variant (no console window), point the Stream Deck button directly at `pythonw.exe tools\process_recordings.py` and skip the .bat — you lose the log capture but gain a hidden run.

### HighlightBuilder.cs recursion

`vegas_scripts/HighlightBuilder.cs` was patched in lockstep with this reorg. Both `FindNewestEventsJson()` and `PickEventsJsonViaDialog()` now pass `SearchOption.AllDirectories` to `DirectoryInfo.GetFiles()`. After sorting, the `recordings\` root will be empty by design, so a non-recursive scan would always throw "no events files." With the patch, the newest `.events.json` across every god subfolder gets pre-selected in the file picker — open Vegas, run the script, hit Enter and the latest recording loads. If you need a different one, the dialog opens already inside the right subfolder so the navigation is one click.

### Module structure

```
tools/process_recordings.py
  categorize(gods_seen)           → (subfolder, stem) routing decision
  next_index(folder, stem)        → lowest unused integer for {stem}-N.mp4
  move_and_emit(...)              → shutil.move + render JSON with new source_video
  process_one(video, idx, ...)    → scan + sort one recording, never raises
  build_parser() / main()         → CLI surface, defaults
  _resolve_hwaccel(value)         → maps "none"/"off"/"" to None for VodDetectorOptions
```

The orchestrator imports `find_mp4s`, `_parse_include`, and `render_events_json` from `tools/extract_events.py` to share the discovery and serialization logic. No code is duplicated between the two CLIs.

### Testing

Unit-tested with mock files at build time:
- `categorize()` — empty, single-god (with spaces in name), multi-god.
- `next_index()` — empty folder, gap-filling, case-insensitive collision, non-numbered files ignored, different stems independent.
- `move_and_emit()` — single-god routing, multi-god → mixed, empty → unknown, sequential numbering across separate runs, `source_video` field rewritten correctly.

End-to-end on a real recording is the next test (waiting on James's first post-stream batch).

---

## Sony Vegas Automation Pipeline (in progress — see SonyVegasTODO.md)

End-to-end pipeline from OBS recording to finished YouTube + TikTok renders. Works alongside the offline VOD pipeline above: drop `.mp4`s into `inbox\`, `extract_events.py` produces `.events.json`, the Python orchestrator (`tools/process_vods.py`) drives Sony Vegas via `-SCRIPT:` to build two timelines per video (full-gameplay horizontal, highlight vertical) using presets captured by TuneFrame.cs. James tweaks in Vegas, presses Enter in the CMD window, Vegas auto-renders.

### Folder layout

```
vegas_scripts\
  HighlightBuilder.cs      Standalone: loads .tune.json + .events.json, builds vertical TikTok timeline.
  TuneFrame.cs             Runs in a manually-tuned Vegas project; dumps pan/crop + audio + OFX effects to <name>.tune.json via SaveFileDialog.
  ProcessVideo.cs          (pending, Step 9) Orchestrator-driven mega-script — builds Phase A (full gameplay) + Phase B (highlight) in one Vegas instance, polls jobs/go.flag for keypress-to-render.
  HelloScript.cs           Temp, delete after batch test — verifies -SCRIPT: command line.
vegas_presets\
  vertical_tiktok.tune.json    1080x1920 vertical highlight preset.
  horizontal_full.tune.json    1920x1080 full-gameplay preset (pending, Step 8).
  _tuneframe_last_run.txt      Full summary + JSON from last TuneFrame run (overwritten each run). Copy-paste friendly when MessageBox text isn't selectable.
config\vegas_pipeline.json  Paths, render template names ("Youtube HD", "TikTok YouTube Short HD"), preset names, auto_render flag.
jobs\current.json           Python writes per-video; ProcessVideo.cs reads on startup. Holds source path, events.json path, both preset names, both render templates, both output dirs.
jobs\go.flag                Python writes on Enter keypress; ProcessVideo.cs polls + consumes to trigger each render.
jobs\phase_done.flag        ProcessVideo.cs writes on completion.
jobs\error.flag             Written on any exception during build/render — orchestrator surfaces it to the user.
inbox\                      Drop .mp4s here.
inbox\processed\            Videos moved here after successful pipeline completion (skip with --keep).
rendered\                   Full-gameplay YouTube outputs (1920x1080).
highlight\                  Vertical TikTok outputs (1080x1920).
```

### `.tune.json` preset schema (v1)

Produced by `TuneFrame.cs`, consumed by `HighlightBuilder.cs` and (soon) `ProcessVideo.cs`:

- `schema_version`, `name`, `kind` ("highlight" | "full_gameplay" | "unknown" — inferred from project dims), `captured_at`.
- `project`: `{width, height, framerate}`.
- `video_tracks[]`: per video track in Vegas UI order — composite mode (CompositeMode enum as string), Pan/Crop four-corner vertices + center (source-pixel coordinates), rotation radians, smoothness, keyframe_type, **scale_to_fill** (mirrors `VideoMotion.ScaleToFill` — critical for overlay tracks that shouldn't stretch to the full output), **effects[]** — each entry has `plugin_id` (OFX PlugInNode.UniqueID), `plugin_name`, `bypass`, `parameters[]` with typed values (Boolean / Double / Integer / Choice stored as choice name / Double2D stored as `{x, y}` / String / Custom).
- `audio_tracks[]`: name, `volume_db` (converted from linear `AudioTrack.Volume` via `20*log10`), `muted` (read via `AudioTrack.Mute` — verified Boolean read/write on Vegas 21).

HighlightBuilder skips preset video tracks named "Titles" — it synthesizes its own Titles track for intro/outro cards to avoid duplication.

### Vegas 21 SDK findings (important)

- **CLI `-SCRIPT:` always spawns a new Vegas window**, even if one is already open. Forces a one-script-per-video architecture (the mega-script `ProcessVideo.cs`) instead of separate build+render scripts — otherwise the second script would render an empty project.
- **Pan/Crop Mask is NOT exposed in `ScriptPortal.Vegas.dll`.** Reflected over every type and property in the assembly; zero hits for "mask". The Mask checkbox in the Pan/Crop dialog is a UI-only feature — the shape data lives in the `.veg` XML but there's no scripting API for it. **Workaround:** use **Bézier Masking OFX** instead. It's a real OFX Effect accessed via `VideoEvent.Effects`, so `Effect.OFXEffect.Parameters` exposes everything (151 parameters across up to 8 mask slots for Bézier Masking). TuneFrame captures; HighlightBuilder re-adds via `vegas.VideoFX` plugin lookup + `vEvent.Effects.AddEffect(plugin)` + per-parameter `Value` set.
- **`OFXDouble2D` uses public fields `X` and `Y`, not properties.** Generic `GetProperty("X")` returns null. Our `TryGetXY`/`TrySetXY` helpers check `FieldInfo` first, then fall back to `PropertyInfo` variants for other SDKs. Apply side uses `Activator.CreateInstance` + boxed-object reflection so it works whether `OFXDouble2D` is a class or struct.
- **`AudioTrack.Mute`** exists as `Boolean` read/write on Vegas 21 (confirmed via reflection diagnostic).
- **Vegas's C# compiler folds `const bool` in ternaries and flags the other branch as "unreachable code"** (error 0x80131600). Use `static readonly bool` instead — same "set once" semantics, no fold.
- **Title generator / Effect API:** title cards via `vegas.Generators` tree walk + `Media(PlugInNode)` + `AddVideoEvent(...).AddTake(media.Streams[0], true)`. OFX effects via `vegas.VideoFX` tree walk + `Effects.AddEffect(pluginNode)`.

### Status

**Done (as of 2026-04-24):** Steps 1–7 per SonyVegasTODO.md. TuneFrame.cs + HighlightBuilder.cs fully rewired around the `.tune.json` schema with OFX effect capture+apply, including Bézier Masking location preservation. Single vertical preset captured and tested end-to-end.

**Next (Steps 8–12):** capture horizontal preset, write `ProcessVideo.cs` mega-script, write `tools/process_vods.py` Python orchestrator, end-to-end dry run, 3-video batch test.

---

## YouTube Live-Badge Thumbnail Swap (tools/youtube_live_badge.py + go_live.bat / go_offline.bat)

When you go live, slap a "🔴 LIVE NOW" badge onto the last 8 video thumbnails on your YouTube channel. When you go offline, restore the originals. Every old-video visitor who lands on your channel during a stream sees red badges and has a one-click jump to live. Free passive viewer-funnel.

**Architecture: download-on-first-encounter, cache, reuse forever.** The first time we badge a video, we download its current YouTube thumbnail and cache it locally at `data/youtube_thumbnails/<video_id>.png` — the canonical original we revert to later. Subsequent stream cycles never re-download. The cache survives manual edits in YouTube Studio (we always pull the current thumbnail when caching for the first time).

**Why this beats generating both versions upfront:** works on every video on the channel including ones uploaded before this tool existed; survives manual thumbnail edits; decoupled from `build_thumbnail.py` (the badge is a Pillow operation that takes ANY input PNG); badge design changes anytime without needing to regenerate a parallel library.

**OAuth requirement.** `thumbnails.set` requires write access (the `youtube` scope), not just an API key. One-time browser flow saves a refresh token to `data/youtube_oauth.json`. The existing `YOUTUBE_API_KEY` (read-only, used by `youtube_rewards.py`) stays in place — the live-badge tool uses a separate OAuth credential.

**Quota cost per stream:** ~900 quota units (`search.list` 100 + 8 × 50 apply + 8 × 50 revert). About 9% of the 10,000-unit daily default budget, leaving plenty of headroom for everything else.

**Anti-spam considerations.** YouTube's heuristics flag rapid thumbnail churn (>5/hr per video). Our pattern is 2/day per video (apply on stream-start, revert on stream-end) — well under any documented or reported thresholds. We also stagger uploads with a small delay between videos so we don't burst-set 8 thumbnails in a single second.

**State tracking** at `data/live_badge_state.json` records the video IDs that got badged in the current stream so `revert` knows exactly what to touch. Idempotent: running `revert` when nothing's badged is a no-op. Failure-tolerant: if any upload fails during revert, the state file isn't cleared, so re-running `revert` retries only the failed ones.

**Stream Deck integration.** `go_live.bat` (apply) and `go_offline.bat` (revert) live at the repo root for one-press triggering. Pair them with the same buttons that start/end your stream session.

**Badge glyph asset (`assets/twitch_logo.png`).** The "LIVE" badge composited onto each thumbnail includes a small Twitch chat-bubble glyph next to the text. If `assets/twitch_logo.png` exists, the tool uses it as the glyph (transparent background expected, resized to fit the badge). If the file is missing, the tool falls back to drawing the glyph programmatically with Pillow — same shape, slightly less polish. The file is optional but recommended for the cleaner look. Path is hardcoded in `tools/youtube_live_badge.py` as `LOGO_PATH = REPO_ROOT / "assets" / "twitch_logo.png"`.

---

## Pre-Stream Readiness Check (tools/check_stream_ready.py + check_stream.bat)

One-press readiness check for everything HatmasBot needs to stream cleanly. Runs ~12 concurrent end-to-end probes — not process-existence checks — so green is real-world meaningful.

**Design philosophy: end-to-end, not presence checks.** The reliable way to verify "Cloudflare tunnel works" isn't `sc query cloudflared`, it's `GET https://hatmaster.tv/healthz` from your machine. That single request transitively validates internet → DNS → Cloudflare edge → `cloudflared` daemon → bot's `public_webserver.py`. Each check in the battery is designed to exercise the real interface the bot uses, so a fail catches actual problems rather than rubber-stamping running processes.

**The 12 checks:**

| Check | Transitively validates |
|---|---|
| Bot dashboard | Bot process + dashboard webserver + plugin manager loaded |
| Twitch bot token | Token fresh + reachable Twitch + scopes intact |
| Twitch broadcaster token | Same + verifies all 7 required broadcaster scopes |
| OBS WebSocket + Smite 2 source | OBS running + plugin enabled + correct port + source named per `KILL_DETECT_OBS_SOURCE` |
| MixItUp API | MixItUp open + Developer API enabled on 8911 |
| tracker.gg | Cloudflare bypass working + broadcaster profile reachable |
| Public webserver (local) | `core/public_webserver.py` listening on 8070 |
| hatmaster.tv | DNS + Cloudflare + cloudflared + public webserver, all in one HTTP probe |
| cloudflared service | Windows service installed + RUNNING |
| Disk space | At least 30 GB free in `recordings/` (warns under 30, fails under 10) |
| God asset library | All wiki gods have icons + cards on disk |
| Spotify token | Token still valid + Spotify reachable |
| SMITE 2 process | Game launched (warning only — not blocking) |

**Concurrent execution.** All checks run in parallel via `asyncio.gather` with hard 5-second per-check timeouts. Total wall-clock is ~400ms full / ~150ms with `--quick`.

**Output modes:**

- Default: colored, terminal-friendly report with hints inline.
- `--json`: machine-readable, no formatting — pipes cleanly into automation or Stream Deck companion buttons.
- `--quick`: skips slower external HTTP (tracker.gg, hatmaster.tv, Spotify) for fast pre-pre-flight.

**Exit codes:** 0 on all-pass-or-WARN, 1 on any FAIL, 2 if the checker itself errors.

**Stream Deck integration.** `check_stream.bat` at the repo root wraps the python tool and pauses 8s on success / 30s on failure so you can read the hints. Drag onto a "System: Open" button. Press ~30s before going live; green verdict means clear, anything red shows you exactly what to fix.

**Future: bot-internal `/api/readiness`.** A natural follow-on is to expose the same check battery from inside the running bot, so the bot can report state it uniquely knows (e.g. last successful KDA read, plugin-level health). Not needed for v1; the external checker covers the most painful real-world failures.

---

## YouTube Thumbnail Builder (tools/build_thumbnail.py + tools/download_god_cards.py)

End-to-end "type two god names, get a 1280x720 YouTube thumbnail" pipeline. Sits alongside the offline VOD pipeline and Sony Vegas automation: once a recording is rendered, run `build_thumbnail.py` to slap together a thumbnail with god icons, card art, headline, KDA, and result, then optionally hand-tweak in Paint.NET before uploading.

**Design choice — Pillow, not Vegas.** A YouTube thumbnail is a single static image. Vegas's strengths (timeline, transitions, render queue) don't apply to a 1-frame deliverable, and Vegas's CLI `-SCRIPT:` always spawns a new window which is too slow for thumbnail iteration. Pillow composites layers in well under a second, and the preset-driven `.json` format mirrors the `.tune.json` approach the rest of the Vegas pipeline already uses, so the muscle memory transfers.

### Asset library (`data/god_cards/`)

Companion to `data/god_icons/` (the 256x256 portrait icons used by the live god matcher). `data/god_cards/<slug>.png` holds the 400x600 "god card" / splash art for every Smite 2 god — the same image SMITE 2's wiki uses as the page's `og:image` meta tag. Filenames use the existing kebab-case slug convention (`ymir.png`, `hou-yi.png`, `morgan-le-fay.png`).

### `tools/download_god_cards.py`

Sibling to `download_god_icons.py`. Pulls god card art for every god listed in `Gods - SMITE 2 Wiki.html`. The wiki uses several different filename conventions for the same asset, varying across **three independent dimensions**:

1. **Filename pattern** — five observed templates: `T_<Name>(S2)_Default`, `T_<Name>S2_Default`, `SkinArt_<Name>S2_Default`, `SkinArt_<Name>(S2)_Default`, and `GodCard_<Name>` (Guan Yu uses this last form, with no `S2` and no parens).
2. **Extension** — most are `.png`, but some are `.jpg` (e.g. `SkinArt_AnubisS2_Default.jpg`). JPEG responses are transcoded to PNG on save so on-disk filenames are always `data/god_cards/<slug>.png`.
3. **Name form for multi-word gods** — sometimes underscored (`Hou_Yi`, `Baron_Samedi`), sometimes concatenated (`DaJi`, `JingWei`). The icon path and the card path can use *different* forms for the same god (Baron Samedi's icon is `T_Baron_Samedi(S2)_Default_Icon.png` but his card is `T_BaronSamediS2_Default.png`).

The downloader probes every combination directly (5 stems × 2 extensions × 1-2 name forms = 10-20 URLs per god) and the first 200 wins. An `og:image` HTML-scrape fallback is available behind `--use-og-scrape`.

**Cloudflare bypass:** `wiki.smite2.com` is fronted by Cloudflare and 403s plain `urllib.request` (TLS fingerprint check). The downloader uses `curl_cffi` (Chrome TLS fingerprint impersonation, same trick `tools/download_voicelines.py` uses for tracker.gg) to get through. `pip install curl_cffi` is required.

```
python tools/download_god_cards.py                  # Download all
python tools/download_god_cards.py -v               # Verbose: show every URL tried + status code
python tools/download_god_cards.py --force          # Re-download everything
python tools/download_god_cards.py --check          # List which cards are missing
python tools/download_god_cards.py --add "God Name" # Add a single god (supports new gods not in saved wiki HTML)
python tools/download_god_cards.py --only "Ymir,Loki,Hou Yi" --throttle 0.2
python tools/download_god_cards.py --use-og-scrape  # Enable HTML og:image fallback
```

Same `--force` / `--check` / `--add` flag spelling as `download_god_icons.py` so the muscle memory transfers. `--throttle` (default 0.4s) gates the per-request sleep for the wiki. `-v` is invaluable when a new god is missing — prints exactly which URLs were tried and what the wiki returned.

### Preset schema (`thumbnail_presets/*.json`)

Mirrors the `.tune.json` approach used by `TuneFrame.cs` / `HighlightBuilder.cs`. Top-level fields: `schema_version`, `name`, `description`, `size: [w, h]`, and `layers: [...]`. Layers render bottom-to-top.

**Layer types:**

| Type | Notes |
|------|-------|
| `solid` | Filled rectangle. `pos`, `size`, `color`, `opacity`. Defaults to full canvas. |
| `gradient` | Linear gradient. `direction: horizontal\|vertical\|diagonal`, `stops: [{at, color, opacity?}]`. |
| `image` | File path or placeholder. `src`, `pos`, `size`, `fit: cover\|contain\|stretch`, `fit_anchor`, `anchor`, `flip_h`, `opacity`, `fallback_text`, `feather_edges`. |
| `icon` | God icon by name. `god`, `pos`, `size` (square px), `border: {color, width}`, `shadow: {color, offset, blur, opacity}`. |
| `text` | Rasterized text. `value`, `pos`, `anchor`, `font`, `size`, `fill`, `stroke: {color, width}`, `shadow: {...}`, `max_width`, `skip_if_empty`. |

**Anchors (`anchor`):** `topleft` (default), `topright`, `bottomleft`, `bottomright`, `center`, `center_top`, `center_bottom`, `left_center`, `right_center`. Anchor names define which point of the layer is placed at `pos`.

**Image-only properties:**
- `fit_anchor` — controls which part of the source image is preserved when `fit: cover` has to drop pixels. Values: `center` (default), `top`, `bottom`, `left`, `right`, `top left`, `top right`, `bottom left`, `bottom right`. The 1v1 preset uses `top` on both god cards so the heads are always visible (the bottoms get cropped instead).
- `feather_edges: {left, right, top, bottom}` — soften image edges with linear alpha ramps in pixels. Used in the 1v1 preset to blend the two god cards across their seam: left card has `feather_edges: {right: 80}`, right card has `feather_edges: {left: 80}`, and the cards overlap by 160px so they fade into each other instead of butting up with a hard line.

**Placeholders resolved at render time:** `{my_god}`, `{my_god2}`, `{vs_god}`, `{vs2_god}`, `{my_god_card}`, `{my_god2_card}`, `{vs_god_card}`, `{vs2_god_card}`, `{my_god_icon}`, `{my_god2_icon}`, `{vs_god_icon}`, `{vs2_god_icon}`, `{text}`, `{subtext}`, `{result}` (WIN/LOSS), `{result2}` (WIN/LOSS for the second matchup in 1v2 / 2matches presets), `{kda}`. Any string field in the preset (including `src`, `value`, `fallback_text`) supports them.

**Shipped presets:**
- `thumbnail_presets/1v1.json` — Two gods facing each other across a feathered seam, big gold VS centered, my god name asymmetrically placed upper-left, opposing god name lower-right (mirrored offsets around canvas center). My god icon top-left with cyan outer glow, opposing god icon top-right with red outer glow. Headline darkening band across the top for legibility on busy splash art. Optional WIN/LOSS badge bottom-center. All text in Big Noodle Titling with stroke + colored glow.
- `thumbnail_presets/1v2.json` — One video, two 1v1 matchups with the same god of yours. My god dominates the left half full-height (the through-line); the two opponents share the right half with the first opponent on top and the second on the bottom. Two gold "VS" markers on the seam, one per matchup. Independent WIN/LOSS badges per opponent via `--result` (top) and `--result2` (bottom). Same color language as 1v1 — cyan glow on my god, red glow on each opponent — so the brand carries over. Use it when you played, e.g., Awilix vs Eset and Awilix vs Chiron in a single video.
- `thumbnail_presets/2matches.json` — One video, two separate 1v1 matches stacked top-to-bottom (each row is its own matchup). Top row is Match 1 (`{my_god}` vs `{vs_god}`), bottom row is Match 2 (`{my_god2}` vs `{vs2_god}`). Each row gets its own gold "VS" marker and four corner icon badges (cyan glow on Hatmaster's gods, red on opponents). Optional joust map background — drop a 1280×720 PNG at `thumbnail_presets/_assets/joust_map.png` and the preset will use it as the base layer with a darkening overlay; falls back to the brand gradient if the file isn't present yet. Independent WIN/LOSS badges per match via `--result` (Match 1) and `--result2` (Match 2). Use it when you played different gods across two matches in one video — e.g. Thanatos vs Baron Samedi, then Baron Samedi vs Awilix.
- `thumbnail_presets/2gods.json` — One video covering two gods Hatmaster played, no opponents shown. Two god cards side by side filling the canvas, full height, feathered at the center seam. Both gods get cyan-glow icon badges in the upper corners (no red opponent glow because there are no opponents). No "VS" marker — these are gods you played, not a matchup. A small gold ampersand sits at the center seam to signal "and" rather than "against". Use it when you played, e.g., 2 Sylvanus games + an Atlas game in one video and want both gods on the thumbnail. CLI: `--god` and `--god2`.
- `thumbnail_presets/single.json` — One hero god dominant on the right, gradient ramp into headline + KDA on the left, result badge top-right.

### `tools/build_thumbnail.py`

```
# Defaults: headline = my god's name, subtext = opposing god's name. Zero typing for a clean matchup thumbnail.
python tools/build_thumbnail.py --god Ymir --vs Loki

# Custom headline + auto subtext
python tools/build_thumbnail.py --god Ymir --vs Loki --text "Pentakill Special"

# Both labels custom
python tools/build_thumbnail.py --god Ganesha --vs Bacchus --text "Fear the Drunk Man" --subtext "Comeback of the Year"

# No labels at all (just god cards + icons + VS)
python tools/build_thumbnail.py --god Ymir --vs Loki --no-text --no-subtext

# Single hero preset
python tools/build_thumbnail.py --god "Hou Yi" --preset single --text "Solo Lane Domination" --kda 18/2/4 --result win

# 1v2 — one video covering two matchups with the same god of yours.
# --vs is the top opponent, --vs2 is the bottom opponent. Independent
# result badges per matchup via --result / --result2.
python tools/build_thumbnail.py --god Awilix --preset 1v2 --vs Eset --vs2 Chiron
python tools/build_thumbnail.py --god Awilix --preset 1v2 --vs Eset --vs2 Chiron --result win --result2 loss

# 2matches — one video covering TWO separate 1v1 matches where you
# played different gods between matches. Top row = Match 1, bottom
# row = Match 2. Optional joust map background — drop the 1280x720
# PNG at thumbnail_presets/_assets/joust_map.png to enable.
python tools/build_thumbnail.py --god Thanatos --vs "Baron Samedi" --god2 "Baron Samedi" --vs2 Awilix --preset 2matches
python tools/build_thumbnail.py --god Thanatos --vs "Baron Samedi" --god2 "Baron Samedi" --vs2 Awilix --preset 2matches --result win --result2 loss

# 2gods — one video covering two of YOUR gods, no opponents shown.
# Two cards side-by-side with cyan icon badges. Use --text for an
# overall headline; --result for an overall WIN/LOSS badge.
python tools/build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods
python tools/build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods --text "Stream Highlights" --result win
# Custom skin art for Sylvanus (drop Custom God Cards/Sylvanus-Forest Lord.png first):
python tools/build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods --skin "Forest Lord"

# Misc
python tools/build_thumbnail.py --list                # show available presets
python tools/build_thumbnail.py --god Ymir --no-open  # skip Paint.NET launch
```

**Auto-fill defaults for labels.** If you don't pass `--text`, the headline auto-fills with `{my_god}` (your god's display name). If you don't pass `--subtext`, the subtext auto-fills with `{vs_god}`. Pass `--no-text` / `--no-subtext` to disable a label entirely; pass `--text "Custom"` / `--subtext "Custom"` to override with explicit text.

**CLI flags:**

| Flag | Default | Description |
|---|---|---|
| `--god <name>` | (required) | My god display name. Drives `{my_god}`, card lookup, icon lookup. |
| `--god2 <name>` | "" | My second god display name (2matches / 2gods preset — for videos covering two matches where you switched gods, or two gods you played without showing opponents). |
| `--skin <name>` | "" | Optional skin variant for `--god`. Looks up `Custom God Cards/<God>-<Skin>.png` first, falling back to `Custom God Cards/<God>.png` and then the auto-downloaded base art. Use this when you want a specific skin's card art instead of the default. |
| `--skin2 <name>` | "" | Optional skin variant for `--god2` (same lookup rules as `--skin`). Used by 2matches and 2gods presets. |
| `--vs <name>` | "" | Opposing god display name (1v1 / 1v2 / 2matches preset; top opponent in 1v2, Match 1 opponent in 2matches). |
| `--vs2 <name>` | "" | Second opposing god display name (1v2 / 2matches preset; bottom opponent in 1v2, Match 2 opponent in 2matches). |
| `--preset <name>` | `1v1` | Preset name from `thumbnail_presets/`. |
| `--text <string>` | (auto: my god name) | Headline above VS. |
| `--no-text` | off | Disable the headline. |
| `--subtext <string>` | (auto: vs god name) | Sub-headline below VS. |
| `--no-subtext` | off | Disable the subtext. |
| `--result <win\|loss>` | "" | Fills `{result}` as WIN/LOSS, else blank. In 1v2: result for the first (top) matchup. |
| `--result2 <win\|loss>` | "" | Fills `{result2}` for the second (bottom) matchup in the 1v2 preset. |
| `--kda <K/D/A>` | "" | KDA string for `{kda}` placeholder (e.g. `12/3/8`). |
| `--size <WxH>` | preset size | Override canvas size. |
| `--out <path>` | `thumbnails/<auto>.png` | Output PNG path. |
| `--no-open` | off | Skip auto-launch into Paint.NET. |
| `--list` | off | List available presets and exit. |

**Three outputs per render:**

1. `thumbnails/<auto_stem>.png` — flat 1280x720 composite, ready to upload to YouTube.
2. `thumbnails/<auto_stem>_layers/` — sidecar folder with one transparent PNG per layer, named in render order (`00_Background.png`, `01_Left_god_card.png`, …). Multi-select all of them and drag into a Paint.NET window to get a layered editing session — they import as separate layers in a single operation.
3. `thumbnails/<auto_stem>.psd` (optional) — single-file layered PSD if `psd-tools` is installed (`pip install psd-tools`). Each layer carries its preset name. Opens cleanly in Paint.NET when the [PSD plugin by 0xC0000054](https://github.com/0xC0000054/psd-plugin) is installed (free, the de facto Paint.NET PSD add-on).

**Auto-open in Paint.NET:** after writing, the script tries to launch Paint.NET on the .psd if it exists, falling back to the .png. Detects Paint.NET at `C:\Program Files\paint.net\paintdotnet.exe` and the (x86) variant; falls back to `os.startfile` (Windows file association) if not found. Pass `--no-open` to skip.

### Card source priority (with skin support)

The god card resolver checks three locations in order before falling back to the auto-downloaded base art:

1. `Custom God Cards/<God>-<Skin>.png` — used when the CLI receives `--skin "<Skin>"` (or `--skin2 "<Skin>"` for the second god). Spaces in skin names are tolerated, so `--skin "Forest Lord"` matches `Sylvanus-Forest Lord.png`, `Sylvanus-ForestLord.png`, or `Sylvanus-Forest_Lord.png`.
2. `Custom God Cards/<God>.png` — drop a file here to permanently override the default card for that god, no flag needed.
3. `data/god_cards/<slug>.png` — the auto-downloaded base art from the Smite 2 wiki.

Use this for skin art the wiki doesn't host yet — download the skin's promo image manually, drop it into `Custom God Cards/` named after the god (and optional skin suffix), and the renderer will pick it up. Example: `python tools/build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods --skin "Forest Lord"`.

### Icon source priority

Icons come from `Custom God Icons/<Display Name>.png` first (your custom OBS overlay icons — high-res, on-brand), then fall back to `data/god_icons/<slug>.png` (the canonical wiki portrait). Drop a `Custom God Icons/<God>.png` file to override the icon used in thumbnails for any god.

**Random icon variants.** When a god has multiple **numbered** icons in `Custom God Icons/` — primary `<God>.png` plus any combination of `<God>-1.png`, `<God>-2.png`, etc. — the resolver randomly picks one per render. Each thumbnail you build for the same matchup looks different, which is fun for streams where you generate multiple thumbnails per session. Only files whose suffix after the final `-` is purely digits count as variants; legacy skin-named files like `<God>-Battleworn.png` are intentionally not pooled (they remain on disk untouched). The console output shows how many variants are in the pool and which one was used. Pass `--no-random-icons` for deterministic primary-only behavior, or `--seed <int>` to lock in a reproducible random choice (useful for batch render scripts that need consistency).

### `tools/import_god_icons.py` — bulk-fill missing icons

Drop candidate images into `Custom_Icons_Inbox/` (any combination of `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.tiff`); the tool detects the god from the filename via fuzzy match (`Hou Yi.png`, `Hou_Yi.jpg`, `HouYi.gif`, `Hou Yi-2.png`, etc.), smart-crops to 1:1 with a top-biased default (heads-up), resizes to 512×512, and saves as PNG into `Custom God Icons/`. Naming follows the convention `build_thumbnail.py` already uses: first import becomes `<God>.png` (primary), additional images for the same god become `<God>-1.png`, `<God>-2.png`, … via `--variants`.

By default existing `<God>.png` files are left alone — the tool only fills gaps. Use `--list-missing` to see which gods don't have a primary icon yet, `--variants` to add numbered alternates to gods that do, or `--force` to replace primaries. Successfully-processed source files move to `Custom_Icons_Inbox/_processed/` so the inbox shows at a glance what's left to do.

```
python tools/import_god_icons.py --list-missing
python tools/import_god_icons.py
python tools/import_god_icons.py --variants
python tools/import_god_icons.py --crop-bias center
```

### Fallback rendering

If `data/god_cards/<slug>.png` is missing for a god (e.g. you haven't run the downloader yet), the image-layer's `fallback_text` fires instead — a colored rectangle with the god's display name in big white letters. This means thumbnails always render successfully; the warning printed by the CLI tells you which god needs `download_god_cards.py --add`.

### Files added

```
tools/
  download_god_cards.py        NEW — downloads 400x600 god card art via og:image scraping
  build_thumbnail.py           NEW — preset-driven Pillow compositor with layered output + Paint.NET launch
thumbnail_presets/             NEW DIRECTORY
  1v1.json                     Two-god matchup preset (left vs right + VS)
  single.json                  Single hero god preset
data/god_cards/                NEW DIRECTORY — populated by download_god_cards.py (auto-downloaded base art)
Custom God Cards/              NEW DIRECTORY — manual overrides; <God>.png replaces the default card,
                               <God>-<Skin>.png is selected when --skin/--skin2 is passed
thumbnails/                    NEW DIRECTORY — output landing zone (auto-created)
```

### Dependencies

| Package | Required for | Notes |
|---|---|---|
| `Pillow` | All compositing | Already in `requirements.txt`. |
| `curl_cffi` | `download_god_cards.py` | **Required** — `wiki.smite2.com` 403s plain `urllib.request` due to Cloudflare's TLS fingerprint check. Same trick `tools/download_voicelines.py` already uses. `pip install curl_cffi`. |
| `psd-tools` | Single-file layered PSD output | **Optional** — without it the tool still produces the flat PNG and the per-layer PNG sidecar folder, so the layered Paint.NET workflow is still available, it just costs one drag-and-drop instead of opening a single .psd. `pip install psd-tools`. |

The PSD plugin for Paint.NET (https://github.com/0xC0000054/psd-plugin) is recommended for opening layered .psd files but is not required if you're happy multi-selecting per-layer PNGs from the sidecar folder.

### Font

The presets use **Big Noodle Titling**, the same condensed display face used by the Hatmas Market overlays. The font loader scans both `C:\Windows\Fonts\` (system-wide) and `%LOCALAPPDATA%\Microsoft\Windows\Fonts\` (per-user, where fonts installed without admin rights end up — Paint.NET reads both, but Pillow doesn't by default). Falls back to Impact / Arial Black / DejaVu Sans Bold if the font isn't installed, so renders never error out.

---

## Web Server Endpoints

### Overlays
| Route | Description |
|-------|-------------|
| / | Control panel dashboard |
| /overlay/nowplaying | Now Playing (450x120, OBS browser source) |
| /overlay/youtube_player | Hidden YouTube audio player |
| /overlay/snap | Snap animation |
| /overlay/god | God match data and team composition |
| /overlay/sound_alerts | Gamble dice roll + sound effects |
| /overlay/tts | TTS highlighted message overlay |
| /overlay/kills | Kill/death event popup + K/D counter |
| /overlay/voicelines | God voice line playback (audio + optional MP4 animation) |
| /overlay/deaths | Daily death counter (simple text, resets at midnight) |
| /overlay/economy_ticker | Economy scrolling price bar (1920x52, OBS browser source) |
| /overlay/economy_match_live | Economy live match panel (320x280) |
| /overlay/economy_match_end | Economy post-match settlement (420x400) |
| /overlay/economy_dividend | Economy dividend popup (420x170) |
| /overlay/economy_leaderboard | Economy top investors (260x360) |
| /overlay/economy_tradefeed | Economy live trade feed (320x340) |
| /overlay/economy_portfolio | Economy viewer portfolio (420x670) |
| /factorio/cards | Factorio card manager (map Streamloots cards to in-game actions, test buttons, recent cards) |

### API
| Endpoint | Method | Description |
|----------|--------|-------------|
| /api/state | GET | Full JSON state |
| /api/state | POST | Partial state update |
| /api/action | POST | Trigger actions (see below) |
| /api/gamble_queue | GET | Drain gamble result queue |
| /api/tts_queue | GET | Drain TTS message queue |
| /api/tts_audio/{filename} | GET | Serve generated TTS MP3 |
| /api/kill_events | GET | Drain kill/death event queue |
| /api/kill_stats | GET | Current match K/D/A stats + detector status |
| /api/voiceline_events | GET | Drain voice line event queue |
| /api/voiceline_audio/{god}/{folder}/{file} | GET | Serve voice line .ogg file |
| /api/voiceline_video/{god}/{file} | GET | Serve animation .mp4 file |
| /api/death_count | GET | Today's death count (`{count, date}`) |
| /api/suggestions | GET | All viewer suggestions |
| /api/economy/market | GET | All god prices, games played, sparkline data |
| /api/economy/portfolio?user= | GET | User portfolio with holdings, P&L, net worth |
| /api/economy/leaderboard | GET | Top 20 investors by portfolio value |
| /api/economy/price/{god} | GET | Single god price data with history |
| /api/factorio/cards | GET | Card mappings + action catalog + recently played cards + status |
| /api/factorio/cards | POST | {op: set\|remove\|test, card, action?, cooldown?, user?, message?} |

### Actions (POST /api/action)
toggle_feature, skip_song, youtube_ended, youtube_started, youtube_progress, snap, go_live, stop_stream, resolve_prediction, set_title_templates, update_title_now, record_result, test_sound, test_kill, test_death, test_tts, test_streamloots (card/user/message/rarity params), add_rotation_command, remove_rotation_command, send_chat, god_donation, god_skip, god_clear, clear_suggestions, start_kill_detect, stop_kill_detect, kd_toggle_debug, kd_toggle_announce, sim_economy (with optional force + god/outcome/kills/deaths/assists params), test_overlay (overlay param: ticker/dividend/leaderboard/portfolio/tradefeed/match_end), reload_prices

---

## Feature Toggles

All controllable via dashboard or API. Default: all enabled.

song_requests, predictions, snap, claude_chat, smite_tracking, gamble, now_playing_overlay, auto_scene_switch, auto_title, god_requests, auto_shoutout, tts_highlights, kill_detection, voicelines, economy, streamloots, factorio

---

## Overlay Manager (core/overlay_manager.py)

Centralized WebSocket-based overlay system. All overlays connect via websocket and receive events through a rules engine.

**overlay_rules.json** defines show_on, hide_on, hide_after (seconds), and keep_alive_on triggers for each overlay. When an event is emitted, the manager checks all rules, shows/hides matching overlays, and manages auto-hide timers. The `keep_alive_on` rule resets the hide timer (used by tradefeed to stay visible during rapid trades).

**WebSocket lifecycle:** Overlays connect to `/ws/overlay/{name}`. On connect, they receive the current state if visible. The manager tracks all connected clients per overlay name, broadcasts events to matching clients, and handles disconnection cleanup. All sends use a 2-second timeout (`asyncio.wait_for`) to prevent hang on unresponsive clients.

**Shutdown:** The `shutdown()` method cancels all active timers, cancels all pending show delays, force-closes all websocket connections (2s timeout each), and clears all state. Called by `webserver.stop()` after emitting `bot_shutdown`.

**Key methods:** `emit(event, data)` — broadcast event + run rules engine. `show(overlay, data)` / `hide(overlay)` — manual show/hide. `_send(overlay, msg)` — broadcast to all WS clients of an overlay.

---

## Token Manager (core/token_manager.py)

Manages bot + broadcaster OAuth tokens. On startup: loads persisted tokens from data/, validates both, refreshes expired ones. Background task validates every ~50min. On 401: immediate refresh + retry. Persists refreshed tokens to data/twitch_token.json and data/twitch_broadcaster_token.json. Updates config module values in memory. Refresh throttled to 30s minimum per token.

Used by: SmitePlugin (title updates, predictions), bot.py (raid shoutout API calls).

## Authentication

- Bot token: `python -m core.auth` (log in as HatmasBot)
- Broadcaster token: `python -m core.auth --broadcaster` (log in as Hatmaster)
- Spotify: auto-generated on first run via browser OAuth

### Broadcaster Scopes
channel:manage:broadcast, channel:manage:predictions, channel:read:subscriptions, moderator:manage:shoutouts, channel:manage:redemptions, channel:read:redemptions, moderator:read:chatters

---

## Key Config Values

| Setting | Description |
|---------|-------------|
| TWITCH_BOT_ID / TWITCH_OWNER_ID | Bot and broadcaster user IDs |
| TITLE_COMMAND_ROTATION | List of commands to cycle in {command} placeholder |
| TITLE_COMMAND_ROTATION_INTERVAL | Seconds between rotations (default: 300) |
| GAMBLE_ALERT_MIN_WAGER | Min wager for sound/visual alerts (default: 100) |
| GAMBLE_CURRENCY_NAME | Must match MixItUp currency name ("Hats") |
| SHOUTOUT_MIN_VIEWERS | Min raid viewers for auto-shoutout (default: 1) |
| SHOUTOUT_COOLDOWN | Seconds between shoutouts to same user (default: 120) |
| TTS_MAX_LENGTH | Max chars for TTS messages (default: 300) |
| SR_PLAYLIST_AUTO_HIDE_SECONDS | Seconds before playlist overlay hides (default: 8) |
| MIXITUP_API_BASE | MixItUp API URL (default: localhost:8911) |
| MIXITUP_INVENTORY_NAME / ITEM_NAME | Must match MixItUp inventory and item names exactly ("God Tokens" / "God Token") |
| ECONOMY_DB_PATH | SQLite database path (default: data/economy.db) |
| ECONOMY_STARTING_PRICE | Initial price for new gods (default: 100) |
| ECONOMY_PRICE_FLOOR | Minimum share price (default: 10) |
| ~~ECONOMY_TRANSACTION_FEE~~ | Removed in the airtight-economy pass. Trading is fee-free. The `transactions.fee` column stays at 0 for schema compat. |
| ECONOMY_DIVIDEND_RATE | Dividend % paid on god pick (default: 0.05) |
| ECONOMY_KILL_TICK / DEATH_TICK / ASSIST_TICK | Live price change per event (+1.5%, -2%, +0.5%) |
| ECONOMY_FREE_SHARE_COUNT | Free shares to chatters on win (default: 1) |
| ECONOMY_EXCLUDED_USERNAMES | Bot accounts to filter out of the economy entirely (default: streamelements, nightbot, moobot, fossabot, pretzelrocks, soundalerts, wizebot). TWITCH_BOT_USERNAME is auto-added. |
| FAIR_VALUE_GAMES_LOG_BONUS | Upside winrate scaling factor (default: 5.5). Higher = more reward for grinding a god. |
| FAIR_VALUE_VOLUME_LOG_BONUS | Flat upside premium for total games played (default: 0.30). |
| FAIR_VALUE_DOWNSIDE_CONFIDENCE_FLOOR | Minimum confidence applied to losing records (default: 0.5). Stops 1-game 0% losses from looking the same as 100-game 43% losses. |
| FAIR_VALUE_KDA_TARGET / PER_UNIT / CAP | KDA modifier shape (default neutral=2.0, ±5%/unit, ±20% cap). |
| YOUTUBE_API_KEY / YOUTUBE_CHANNEL_ID | Required to enable YouTube share rewards. |
| YOUTUBE_POLL_INTERVAL | Seconds between regular comment scans (default: 900 = 15 min). |
| YOUTUBE_DEEP_SCAN_INTERVAL / YOUTUBE_DEEP_SCAN_VIDEOS | Daily catch-up scan (default: 86400s, 250 videos). |
| YOUTUBE_VIDEOS_PER_SCAN | Recent uploads to check on each regular scan (default: 25). |
| YOUTUBE_FREE_SHARE_COUNT | Shares awarded per new commenter per video (default: 1). |
| YOUTUBE_TITLE_PATTERN | Regex matching "Full Gameplay: X vs Y" titles. |
| SMITE2_GAMEMODES_TO_TRACK | List of gamemode keys queried by replay tool's per-mode profile fetcher. |
| SMITE2_BACKFILL_INTERVAL | Seconds between periodic match backfills (default: 300 = 5 min). |
| SMITE2_BACKFILL_BOOT_DELAY | Seconds before the first backfill runs after on_ready (default: 15). |
| STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET | From env or config_local.py. BOTH required or priority requests disable themselves (accepting payments we can't verify would strand paid viewers). |
| PRIORITY_REQUEST_* | Price (500 cents), currency, product name, success/cancel URLs, max message length (200). See v2.6 section. |
| WEB_SESSION_SECRET | 64+ random chars in config_local.py. Empty = website login + trading disabled (fail closed). Rotating it logs every viewer out. |
| WEB_TRADING_ENABLED | Master switch for website trading (default False). Also gated by the `web_trading` feature toggle for mid-stream cutoff. |
| WEB_TRADE_COOLDOWN / WEB_TRADE_MAX_PER_MIN | Per-user trade cooldown (3s, mirrors chat) / per-IP request cap on /api/trade + /auth/* (30/min). |
| WEB_OAUTH_REDIRECT_URI | Twitch OAuth callback. Prod default https://hatmaster.tv/auth/twitch/callback; localhost override for dev. |
| TIKTOK_USERNAME / TIKTOK_LATEST_VIDEO_URL | Social tab: profile handle + manually-pasted featured video URL (TikTok has no usable public API). |
| BLUESKY_HANDLE / SOCIAL_FEED_CACHE_TTL | Social tab: Bluesky account + feed cache TTL (900s). |
| STREAMLOOTS_ALERT_ID | Streamloots alert overlay GUID (config_local.py only - treat as a secret). Empty = StreamlootsPlugin disabled (fail closed). |
| FACTORIO_RCON_HOST / PORT / PASSWORD | RCON target for the hatmas-events mod (default 127.0.0.1:27015). Empty password = FactorioPlugin disabled (fail closed). |
| FACTORIO_SCRIPT_OUTPUT | Factorio script-output folder. Empty = auto-detect %APPDATA%\Factorio\script-output. |
| FACTORIO_CARD_MAP | SEED ONLY for data/factorio_cards.json (first run). Live mappings are managed at /factorio/cards. Actions: adopt_pet, grow_pet, pet_say, boss_attack. |
| FACTORIO_ANNOUNCE_EVENTS | Relay mod outbox events (pet deaths, boss kills) to Twitch chat (default True). |

---

## v2.5 Update — Hatmas Market Public Website

The biggest change in 2.5 is that HatmasBot now drives a public-facing
website at `hatmaster.tv`. Viewers earn shares of Smite gods (priced
based on Hatmaster's actual Smite 2 performance) just by participating
— Twitch chatters via free-share distribution and `!buy`/`!sell`
commands, YouTube commenters via comment scanning. They can then look
at their portfolio, top holders for any god, the live god grid with a
scrolling stock-ticker tape, and an embedded Twitch player + chat that
shows up automatically when Hatmaster is streaming. The whole thing is
exposed via Cloudflare Tunnel — no port forward, no exposed IP.

### Public Webserver (`core/public_webserver.py`, port 8070)

A separate, intentionally **read-only** aiohttp app that runs on port
8070 alongside the existing dashboard webserver on 8069. The split is
a security boundary: 8069 hosts the control panel + OBS overlays + the
POST `/api/action` endpoints that mutate state; 8070 only does GETs and
WebSocket subscriptions. The Cloudflare Tunnel only points at port
8070, so even if the public-facing code has a bug, the dashboard
literally cannot be reached from the internet.

Bound to `127.0.0.1`, never `0.0.0.0`. The only path to it from outside
is through the cloudflared process running locally (which makes an
outbound connection to Cloudflare's edge — Cloudflare has no inbound
access).

**Routes:**

| Route | Description |
|-------|-------------|
| `GET /` | Landing: god grid + ticker + Twitch embed + search |
| `GET /yt/{channel_id}` | YouTube viewer's portfolio page |
| `GET /twitch/{username}` | Twitch viewer's portfolio page (same template) |
| `GET /god/{name}` | God detail page: chart, lifetime stats, holders (with All/Twitch/YouTube tabs), recent matches, formula breakdown |
| `GET /api/gods` | All gods with sparklines + W/L + KDA totals |
| `GET /api/god/{name}` | Single god detail (history, holders by platform, breakdown) |
| `GET /api/yt/{channel_id}` | Per-YouTube-viewer portfolio JSON |
| `GET /api/twitch/{username}` | Per-Twitch-user portfolio JSON |
| `GET /api/search?q=` | Cross-platform display-name search (returns YT + Twitch results, each tagged) |
| `GET /api/prices` | Snapshot of every god's current price |
| `GET /api/stream-status` | `{is_live, channel, title, viewer_count, current_god, ...}` driven by StreamStatusPlugin |
| `GET /god-icon/{slug}` | Serves `data/god_icons/{slug}.png` (kebab-case lowercase set) |
| `GET /custom-god-icon/{slug}` | Serves `Custom God Icons/{Title}.png` (matches OBS overlay assets); 302-falls-through to `/god-icon/` if no exact match |
| `GET /theme.css` | Shared CSS theme (B612 + B612 Mono fonts, terminal palette) |
| `GET /ws/yt/{channel_id}` | WebSocket: live price ticks for a YouTube portfolio |
| `GET /ws/twitch/{username}` | WebSocket: live price ticks for a Twitch portfolio |
| `GET /ws/god/{name}` | WebSocket: live price ticks for a single god page |
| `GET /healthz` | Health check (Cloudflare uses this) |
| `POST /api/priority-request/create` | Create a Stripe Checkout Session for a $5 priority god request (form on /community) |
| `POST /api/stripe-webhook` | Stripe signed webhook: checkout completed, refunds, disputes (see v2.6 section) |
| `GET /priority-success` | Post-payment thank-you page Stripe redirects to |
| `GET /auth/login` | Redirect to Twitch OAuth authorize (zero scopes, state nonce cookie) |
| `GET /auth/twitch/callback` | Code exchange + identity fetch + session cookie issue (v2.7 section) |
| `POST /auth/logout` | Clear the session cookie |
| `GET /api/me` | Session identity for JS hydration (401 + capabilities when logged out) |
| `GET /api/me/balance` | Logged-in viewer's hat balance from MixItUp |
| `POST /api/trade` | Authenticated buy/sell — delegates to economy execute_buy/execute_sell behind nine guards (v2.7 section) |
| `GET /auth.js` | Shared login/trading JS client injected into every page's brand-band |
| `GET /api/me/settings` | Logged-in viewer's account flags (leaderboard_hidden) |
| `POST /api/me/visibility` | Self-serve leaderboard hide/show (replaces the never-implemented !hideme) |
| `GET /api/social/youtube` | Latest 12 uploads via playlistItems (1 quota unit, 15-min cache) |
| `GET /api/social/bluesky` | Latest 10 posts via Bluesky public API (reposts/replies skipped, 15-min cache) |
| `GET /api/social/tiktok` | Manual config passthrough (profile + featured video URL) |

**Live updates:** the public webserver subscribes to the existing
`OverlayManager` via a new `add_event_listener` API. When `economy.py`
fires `god_stock_update` / `god_stock_update_kd` / `dividend_paid`,
the public webserver receives the event, looks up which `youtube_holdings`
and `portfolios` rows reference the affected god, then forwards the
event to any open WebSocket on `/ws/yt/{channel_id}`, `/ws/twitch/{username}`,
or `/ws/god/{name}` whose key matches. So holders see their portfolio
value tick during a kill, viewers on a god page see the price flash
amber and the chart append a point.

### Cloudflare Tunnel (live at hatmaster.tv)

Cloudflare Tunnel is what makes the website reachable from the internet
*without* exposing your home IP, opening any inbound ports on your
router, or making your bot machine network-addressable. James was new
to this when we set it up, so the rationale and operational notes are
here for posterity.

**What it actually is:** the `cloudflared` daemon makes an *outbound*
connection from your PC to Cloudflare's edge network. It says "I'm
ready to receive traffic for hatmaster.tv." Cloudflare receives public
traffic for that hostname at their edge servers worldwide, then routes
it down through that outbound connection to your machine. Inbound HTTP
never touches your home network — your PC initiated the link, like a
permanent SSH tunnel.

**Why this beats port forwarding:**
- No router config (no opened ports, no DDNS).
- Your home IP is never exposed to website visitors. They see Cloudflare's IPs.
- Cloudflare absorbs DDoS attempts at the edge. None hit your PC.
- Free TLS certificate provisioned automatically. `https://hatmaster.tv` just works.
- Free DDoS protection, bot mitigation, basic rate limiting.
- Survives ISP IP changes silently — the outbound connection re-establishes.

**Setup steps we ran (one-time):**

> **Tunnel name:** the live tunnel is now **`HatBot`** (current PC). An
> older `HatmasBot` tunnel still exists on the previous PC and in the
> Cloudflare dashboard, pending removal — don't confuse the two. The
> commands below use the current name. `CLOUDFLARED_TUNNEL_NAME` in
> `core/config.py` must match whatever `cloudflared tunnel list` shows.

1. Bought `hatmaster.tv` via **Cloudflare Registrar** ($30/yr, no markup over wholesale, auto-managed DNS). Could also work with any registrar (Porkbun, Namecheap) by pointing nameservers at Cloudflare.
2. Created a Cloudflare Zero Trust account (free tier; required to use Tunnels).
3. Installed `cloudflared` via the Windows MSI (https://github.com/cloudflare/cloudflared/releases).
4. Ran `cloudflared tunnel login` — opened a browser, picked the `hatmaster.tv` zone, downloaded `cert.pem` to `C:\Users\james\.cloudflared\`.
5. `cloudflared tunnel create HatBot` — created a new tunnel, generated `<TUNNEL_ID>.json` credentials.
6. Wrote `C:\Users\james\.cloudflared\config.yml`:
   ```yaml
   tunnel: <TUNNEL_ID>
   credentials-file: C:\Users\james\.cloudflared\<TUNNEL_ID>.json

   ingress:
     - hostname: hatmaster.tv
       service: http://localhost:8070
     - service: http_status:404
   ```
7. `cloudflared tunnel route dns HatBot hatmaster.tv` — automatically created the CNAME pointing the bare domain at the tunnel.
8. `cloudflared.exe service install` (run from the user home directory so the config.yml is found) — registered cloudflared as a Windows service.
9. `sc config cloudflared start= auto` + `sc start cloudflared` — sets startup type to auto so it survives reboots, and starts it now.

Day-to-day there's nothing to do — the Windows service auto-starts on
boot. If you ever see Cloudflare error 1033 ("Tunnel error") on
hatmaster.tv, the cloudflared service has stopped. Fix with `sc start
cloudflared`. If `sc query cloudflared` says it's running but the
dashboard shows zero connections, check `cloudflared tunnel info
HatBot` — usually means the wrong tunnel ID is configured, redo
the local install.

**Pitfalls we hit:**
- Token-installed tunnels (the dashboard "paste this command" install)
  use *remote-managed config* — you can't edit `config.yml` and have
  it take effect. We deleted that and recreated via CLI for full local
  control.
- `cloudflared service uninstall` puts the service into "marked for
  deletion" state if Services.msc / Task Manager has a handle open.
  Sign out + back in (or reboot) to clear.
- Foreground `cloudflared tunnel run HatBot` works for testing but
  only persists while the terminal stays open. Always use the Windows
  service for production.
- The `parent=` query param in Twitch player iframe URLs has to match
  the actual hostname. Our pages use `location.hostname` so it works
  on both `localhost:8070` and `hatmaster.tv` automatically.

### YouTube Rewards (`plugins/youtube_rewards.py`)

A passive scanner that polls YouTube's Data API v3 looking for new
comments on Hatmaster's "Full Gameplay" videos. Each new commenter
earns 1 free share of the god featured in that video, applied to their
`youtube_holdings` row. They never need to authenticate or "sign up"
— just commenting publicly registers them.

**Title parser:** every video the bot scans gets a god mapping in
`youtube_video_gods`. The default regex captures James's god from the
"Full Gameplay: X vs Y" title format (the X side, validated against
the known god list from `data/god_icons/`). Manual override available
via `python tools/mark_youtube_video.py set <video_id> <god>`. Auto
tags (set_by='auto') are protected from being overwritten by manual
tags (set_by='manual') even with `--overwrite`.

**Two scan tiers:**
- *Regular scan* every `YOUTUBE_POLL_INTERVAL` (default 15 min). Walks
  the most recent `YOUTUBE_VIDEOS_PER_SCAN` (25) uploads.
- *Deep scan* on bot launch (always) and once per `YOUTUBE_DEEP_SCAN_INTERVAL`
  (default 24h). Walks `YOUTUBE_DEEP_SCAN_VIDEOS` (250) uploads with
  pagination so comments on older videos still get picked up.
- Both share dedup via `youtube_processed_comments` (keyed on
  `(video_id, channel_id)` so re-comments on the same video don't pay
  out twice).

**Schema:** `youtube_portfolios`, `youtube_holdings`, `youtube_video_gods`,
`youtube_processed_comments`, `youtube_transactions`. All in `economy.db`,
parallel to the Twitch-side `portfolios` / `transactions` tables.
Schema definitions live in `core/youtube_schema.py` as the single
source of truth — `plugins/economy/db.py:_init_schema()` imports
`YOUTUBE_SCHEMA_SQL` from there, and standalone CLI tools (e.g.
`tools/mark_youtube_video.py`) call `ensure_youtube_schema()` from the
same module. `IF NOT EXISTS` everywhere so first launch on an existing
DB just adds the new tables.

**Dividends compound for YouTube holders as fractional bonus shares.**
A YouTube viewer with 3 shares of Sylvanus gets 0.15 extra shares
when a 5% Sylvanus dividend fires (instead of hats — they have no
MixItUp account). Their `shares` REAL column carries the decimals.

### Stream Status Plugin (`plugins/stream_status.py`)

Polls Twitch's `/helix/streams?user_login=<channel>` endpoint every
60 seconds. On state change (offline → live or live → offline),
emits `stream_live` / `stream_offline` via the OverlayManager so the
public webserver caches the status and exposes it via `/api/stream-status`.
Drives the Twitch player + chat embed on the website — the iframe is
mounted only when the API reports `is_live: true`.

Also surfaces `current_god` from the smite plugin's in-memory state,
so the website's "Currently playing: <god>" line updates within 30
seconds of a god switch mid-stream.

### Match Settlement & Periodic Backfill (`plugins/economy.py`)

Replaces the original momentum-based "compound a delta per match"
model with a **fair-value formula** derived from running stats. Single
`settle_match(match_id, god, outcome, k, d, a, *, source)` function
owns all match-end price math; both the live path (broadcaster resolves
prediction) and the periodic backfill go through it. Identical
arithmetic in either path.

**Dedup table:** `processed_matches` (PK on `match_id`). Every settled
match writes a row. Re-runs are idempotent — same `match_id` is silently
skipped. Source column distinguishes 'live' / 'backfill' / 'replay'
for debugging.

**Periodic backfill:** the economy plugin schedules a background task
that runs every `SMITE2_BACKFILL_INTERVAL` (default 5 min). Each cycle
calls `smite_plugin.get_match_history()` (uses the tracker.gg listing
endpoint, which is more inclusive than the per-mode profile aggregates
endpoint and includes Duel/Arena/etc.), diffs against `processed_matches`,
and calls `settle_match(source='backfill')` for anything new. **Means
matches settle without requiring you to manually resolve the Twitch
prediction or restart the bot.**

Live-only side effects (free shares to current chatters, "Awesome!"
voiceline, leaderboard overlay update) are scoped to `source='live'`.
Backfilled matches do the price math but skip the social effects.

**Match-listing parser:** `smite_plugin.parse_listing_entry()` parses
the JSON from `/api/v2/smite2/standard/matches/steam/{id}` (no auth
needed). Outcome is derived by comparing `segment.metadata.teamId`
against the match-level `metadata.winningTeamId`. Replaces the older
detail-fetch path which required `?authlevel=user` (and 401'd in
practice).

### Fair-Value Pricing Formula

Located at the top of `plugins/economy.py` as `calculate_fair_value()`.
Takes (wins, losses, total_kills, total_deaths, total_assists) and
returns a price.

Asymmetric: above 50% winrate the formula scales aggressively with
games played (so a 100-game 80% comfort pick is worth meaningfully
more than a 2-game 100% sample). Below 50% it stays bounded (max -50%
from winrate alone, ±20% from KDA). Volume premium of `1 + log10(games+1) * 0.30`
fires on the upside only — rewards grinding a god you genuinely win on.
Downside confidence is floored at 0.5 so a 1-game 0% loss penalizes
visibly even at low sample size.

Reference points (with shipped constants):

| Scenario | Price |
|---|---|
| 50% WR, 1 game | ~100 |
| 100% WR, 1 game | ~125 |
| 30% WR, 50 games | ~81 |
| 60% WR, 50 games | ~287 |
| 80% WR, 100 games | ~505 |
| 70% WR, 200 games | ~437 |
| 50% WR, 300 games | ~175 |
| 0% WR, 1 game | ~75 |
| 100% WR, 1000 games | ~1490 |

Tunable via `FAIR_VALUE_*` constants at the top of `economy.py`.
Edit + re-run replay to see new prices on existing data.

### Multi-Gamemode Profile Aggregates (`smite_plugin.get_god_aggregates()`)

Tracker.gg's profile aggregates are gamemode-scoped — the default call
returns ranked-conquest data only. To get a complete picture across
your Smite 2 history, the replay tool fetches per-gamemode and SUMs
the per-god stats across every mode in `SMITE2_GAMEMODES_TO_TRACK`.

URL format: `/profile/steam/{id}/segments/god?gamemode={mode}&season=`.
Gamemode keys: `conquest-ranked`, `conquest`, `arena`, `assault`,
`joust`, `duel`. Conquest (Bots) is intentionally excluded.

**Dedupe by response fingerprint:** if tracker.gg ever ignores the
gamemode param and returns the same response for multiple modes, a
sorted-tuple fingerprint detects the duplicate and skips it. Prevents
the "all stats 6× too high" bug we hit during development.

### Bot Account Exclusion (`ECONOMY_EXCLUDED_USERNAMES`)

Bot accounts that show up in Twitch chat (StreamElements, Nightbot,
Moobot, Fossabot, Pretzelrocks, Soundalerts, Wizebot, plus the bot's
own `TWITCH_BOT_USERNAME`) are excluded from the economy at every
query point:

- `_get_chatters` filter: don't grant free shares to bot accounts.
- `_pay_dividend` SQL: `LOWER(username) NOT IN (...)` filters them
  out of dividend payouts.
- `cmd_buy` / `cmd_sell`: silent no-op for excluded users (avoids
  bot-talking-to-bot loops).
- Public webserver top-holders SQL on `/api/god/{name}`: same filter.
- Public webserver `/api/search`: bot usernames don't appear in
  cross-platform search results.

**No data deleted at query time.** Existing rows for these accounts
stay in the DB; if you remove an account from the exclusion list,
their old shares immediately become visible and earn dividends again.

If you also want to permanently delete those accounts' rows from
`portfolios` + `transactions`, run `python tools/purge_excluded.py`.
It backs up the DB first and asks for explicit confirmation.

### New CLI Tools

| Tool | Purpose |
|------|---------|
| `tools/mark_youtube_video.py` | YouTube video → god mapping (manual `set <video_id> <god>`, `--auto-scan`, `--list-untagged`, `--list-tagged`, `--scan-comments`, `--stats`). |
| `tools/replay_economy.py` | Wipe `god_prices` + `price_history` + `processed_matches`, re-fetch tracker.gg profile aggregates across all gamemodes, recompute every god's price via the fair-value formula. Backs up `economy.db` first. Portfolios are preserved. |
| `tools/purge_excluded.py` | Delete bot-account rows from `portfolios` + `transactions`. Backs up first; supports `--dry-run`. |
| `tools/catchup_backfill.py` | Settle tracker.gg matches the bot's periodic backfill missed (July 2026, added after the match.py truncation left 5 weeks unsettled). Deep listing fetch (`--max 500 --pages 20`, pagination best-effort — tracker.gg currently serves one ~25-match page), diffs against `processed_matches`, settles oldest→newest through the same `settle_match()` path as the bot. Additive only (no wipe); backs up `economy.db` first; supports `--dry-run`; idempotent. |

### Theme & UI Architecture

- `public/theme.css` — shared design system: B612 + B612 Mono fonts (Airbus cockpit family), near-black palette with amber accent + vivid green/red ticks, sharp 4px corners, mono numbers with tabular figures throughout.
- `public/landing.html` — simple home page (July 2026 split): social tabs (Twitch live embed / YouTube / TikTok / Bluesky), platform pills, and a market teaser card. All market content moved to market.html.
- `public/market.html` — the Hatmas Market at `hatmaster.tv/market`: god grid + ticker tape + portfolio search + top traders + recent activity + icon toggle. `/Market` 301-redirects to `/market`.
- `public/portfolio.html` — per-viewer portfolio (handles both `/yt/` and `/twitch/` routes).
- `public/god.html` — god detail with stock-style price chart, holder tabs (All/Twitch/YouTube), recent matches, formula breakdown.
- All three pages support `?preview=1` to force-show the Twitch embed using sample data.
- All three pages have an `[ICONS: CUSTOM]` ↔ `[ICONS: OFFICIAL]` toggle in the brand band.

### Files added or significantly modified in v2.5

```
core/
  public_webserver.py          NEW — read-only aiohttp app on port 8070
  youtube_parser.py            NEW — title regex + god list loader
  youtube_schema.py            NEW — shared CREATE TABLE definitions
  overlay_manager.py           +add_event_listener / remove_event_listener
plugins/
  youtube_rewards.py           NEW — comment scanner + share grants
  stream_status.py             NEW — Twitch live-status poller
  economy.py                   +settle_match, +fair-value formula, +YouTube schema
  smite.py                     +get_match_history, +get_god_aggregates
public/                        NEW DIRECTORY (theme.css, landing.html, portfolio.html, god.html)
tools/
  mark_youtube_video.py        NEW
  replay_economy.py            NEW
  purge_excluded.py            NEW
core/config.py                 +YOUTUBE_*, +SMITE2_BACKFILL_*, +FAIR_VALUE_* constants
main.py                        +YouTubeRewardsPlugin, +StreamStatusPlugin, +PublicWebServer
```
---

## v2.6 Update — Priority God Requests (Stripe)

Viewers pay $5 on `hatmaster.tv/community` to push a god request to
the head of the godrequest queue. Stripe hosts the card form (Checkout
Session) — the bot never sees card data. Real money now flows through
the bot, so this section also documents the audit/refund tooling and
the regression tests that guard the path.

### Plugin (`plugins/priority_request.py`)

No chat commands — the entire surface is HTTP on the public webserver
(port 8070): `POST /api/priority-request/create` builds the Checkout
Session (god + twitch_username + message ride along as Stripe
`metadata`, so the webhook gets them back without a DB round-trip and
the signature proves Stripe vouches for them), `POST
/api/stripe-webhook` receives Stripe's signed events, and `GET
/priority-success` serves the thank-you page. The feature
hard-disables unless BOTH `STRIPE_SECRET_KEY` and
`STRIPE_WEBHOOK_SECRET` are set — accepting payments we can't verify
on the webhook side would mean a viewer paid and nothing got queued.
For local dev: `stripe listen --forward-to
localhost:8070/api/stripe-webhook`.

### Status lifecycle (two-phase, crash-safe)

`priority_payments` table in economy.db, one row per Checkout Session:

| Status | Meaning |
|---|---|
| pending | Session created, viewer never finished paying (abandoned checkouts stay here — expected noise) |
| paid | Payment verified via webhook signature, god NOT yet queued |
| fulfilled | queue_add succeeded — the god is (or was) in the queue |
| refunded | Money returned: manual refund, `reconcile_stripe.py refund`, or chargeback |

Timestamp columns: `paid_at`, `fulfilled_at`, `played_at`,
`refunded_at`, plus `payment_intent` (required to issue refunds and to
match `charge.refunded` events back to a session). Columns are added
via PRAGMA-driven idempotent migrations, so pre-v2.6 DBs upgrade in
place.

**Why two-phase:** the original implementation marked the row
`fulfilled` BEFORE calling `queue_add`, which silently broke webhook
replay as a crash-recovery tool — a crash between the DB write and the
queue insert left a payment that looked handled but never hit the
queue, and Stripe's retry no-op'd on the `fulfilled` status. Now the
webhook claims the row as `paid`, queues, and only then marks it
`fulfilled`. Replaying a `paid` row re-attempts the queue insert with
a queue-membership check on `stripe_session_id` (stored on the queue
entry) so it can never double-queue. Anything stuck in `paid` is fixed
by resending the webhook from the Stripe dashboard.

**played_at vs fulfilled_at:** `fulfilled` only means "queued".
`played_at` is stamped when the god is actually played, via
godrequest's `add_history_listener` (the same append-only
listener-list pattern as the kill detector hooks; `_save_history`
fires each listener with the final history entry, which carries
`stripe_session_id` for paid entries). Skipped/removed paid entries
deliberately stay unplayed — they're the refund candidates
`reconcile_stripe.py unplayed` lists.

### Webhook events handled

- `checkout.session.completed` — verify signature, payment status,
  amount (underpaid sessions rejected), metadata; claim → queue at
  head (source `paid_priority`) → fulfilled → plain-text chat
  announcement (Tone rule: no emojis).
- `charge.refunded` / `charge.dispute.created` — mark the row
  `refunded` and pull the entry from the queue if it hasn't been
  played yet. Matched via `payment_intent`; events for unrelated
  Stripe products on the same account won't match a row and are
  acknowledged. Disputes are treated like refunds so a disputed
  payment can't ride the queue for free.

Configure the Stripe webhook endpoint to send all three event types.
A late `checkout.session.completed` retry arriving after a refund
returns `already_refunded` and does not resurrect the queue entry.

### Ops tooling (`tools/reconcile_stripe.py`)

| Command | Purpose |
|---|---|
| `python tools/reconcile_stripe.py audit [--days 30]` | Diff Stripe's checkout sessions against `priority_payments`. Catches dropped webhooks (paid in Stripe, nothing local), crash-window `paid` rows, and refund mismatches. Exit 0 clean / 1 discrepancies. |
| `python tools/reconcile_stripe.py unplayed` | List fulfilled-but-never-played payments — the refund candidates. Run at end of stream to settle up before viewers have to ask. |
| `python tools/reconcile_stripe.py refund <session_id> [--yes]` | Issue a real Stripe refund and mark the local row. If the bot is running, its `charge.refunded` webhook also removes any still-queued entry. |

Run `audit` after any stream where the bot crashed or restarted. Safe
to run while the bot is up (WAL mode, single-row writes only).

### Regression tests (`tools/test_priority_request.py`)

16 tests covering the whole webhook path against an in-memory DB and
a fake godrequest plugin, with only Stripe's signature verification
stubbed: signature/payload rejection, idempotent replay, both crash
windows (claimed-but-not-queued and queued-but-not-marked), refund +
dispute handling, post-refund replay, played/skipped stamping, and
the no-emoji chat rule. `python tools/test_priority_request.py` exits
0 on full pass (optional name-substring filter argument). Run it
before touching priority_request.py — same canary role as
test_kda_fixture.py for the KDA pipeline.

### Files added or significantly modified in v2.6

```
plugins/priority_request.py     Two-phase webhook lifecycle, refund/dispute handling, played_at stamping, payment_intent capture
plugins/godrequest.py           +add_history_listener (fired from _save_history); PRIORITY OBS badge emoji removed per Tone rule
tools/reconcile_stripe.py       NEW — audit / unplayed / refund CLI
tools/test_priority_request.py  NEW — 16-test money-path regression suite
public/community.html           Priority badge emoji removed per Tone rule
```
---

## v2.7 Update — Website Login + Trading

Twitch viewers can now log in on `hatmaster.tv` and buy/sell god
shares from the website — phone, between streams, no chat needed.
Full design + threat model in `WEBSITE_TRADING_DESIGN.md` (approved
by Hatmaster June 2026); this section is the operational summary.

### How it works

- **Login:** `GET /auth/login` → Twitch OAuth authorize with ZERO
  scopes (identity only) and a `state` nonce bound to a short-lived
  cookie. The callback exchanges the code server-side, calls
  `/helix/users`, then discards the token — viewer tokens are never
  stored. Reuses the bot's Twitch application; the callback URLs
  (prod + localhost) must be registered in the Twitch dev console.
- **Session:** stateless HMAC-signed cookie (`core/web_session.py`,
  stdlib only). 30-day expiry, HttpOnly + Secure + SameSite=Strict.
  No session table; rotating `WEB_SESSION_SECRET` logs everyone out.
- **Trading:** `POST /api/trade` delegates to the economy plugin's
  `execute_buy` / `execute_sell` — the SAME money path as chat
  commands, tagged `channel='web'` in `transactions`. The handler
  never touches balances or prices itself.
- **One front door, nine guards, in order:** master switch (config +
  `web_trading` feature toggle) → session cookie → Origin allowlist
  (CSRF backstop on top of SameSite=Strict) → excluded-bot filter →
  per-user 3s cooldown → per-IP 30/min window → body validation →
  per-user asyncio.Lock (closes the balance-check race two browser
  tabs could hit; chat never races) → economy DB + MixItUp up.
- **Market hours:** hats live in MixItUp, so trades need the bot PC
  up. `/api/stream-status` now carries `market_open`; the site header
  shows MARKET OPEN/CLOSED. Off-stream trading is allowed — prices
  only move at tracker.gg settlement, so it is value-neutral.
- **Web trades hit the stream trade feed** via the same
  `trade_executed` overlay event as chat trades.

### UI (public/)

`public/auth.js` is included by all four pages and self-injects the
login button / avatar chip + market pill into the brand-band, and
exposes `HatmasAuth.trade()` / `HatmasAuth.balance()`. The portfolio
page shows a Trade card when the logged-in viewer is looking at their
OWN /twitch/ portfolio (god datalist, amount or 'all', click a
holding to prefill). The god page has a trade box under the chart —
logged out it renders the login link instead. The /community priority
form locks `twitch_username` to the session login when logged in,
which kills the typo'd-username failure mode in the Stripe metadata
path. The dashboard side is untouched — port 8069 remains
tunnel-invisible.

### Going live checklist (one-time)

1. Twitch dev console → the bot's application → add OAuth Redirect
   URLs: `https://hatmaster.tv/auth/twitch/callback` and
   `http://localhost:8070/auth/twitch/callback`.
2. `config_local.py`: set `WEB_SESSION_SECRET` (generate with
   `python -c "import secrets; print(secrets.token_urlsafe(64))"`).
3. Test the flow on localhost (`WEB_OAUTH_REDIRECT_URI` localhost
   override), then flip `WEB_TRADING_ENABLED = True`.
4. Cloudflare dashboard → WAF rate rule on `/api/trade` + `/auth/*`
   (e.g. 60 req/min/IP) for edge-level defense in depth.
5. `check_stream.bat` now includes a "Website login" probe that
   fails loudly if trading is enabled but secrets are missing.

### Tests

`python tools/test_web_session.py` — 11 tests on the token crypto
(tamper, forgery, expiry, garbage). `python tools/test_web_trade.py`
— 19 tests that boot the real PublicWebServer with a fake economy
and hit every guard, the per-user lock serialization, /api/me,
logout, the OAuth redirect, and `market_open`. Run both before
touching web_session.py, the auth routes, or the trade handler.

### Files added or significantly modified in v2.7

```
core/web_session.py             NEW — HMAC session tokens (stdlib only)
core/public_webserver.py        +OAuth routes, /api/me, /api/me/balance, /api/trade (nine guards), /auth.js, market_open
core/config.py                  +WEB_* keys, +web_trading feature toggle
plugins/economy/trading.py      execute_buy/execute_sell accept channel= ('chat' default)
plugins/economy/db.py           +transactions.channel migration
public/auth.js                  NEW — shared login/trading client
public/theme.css                +auth chip, market pill, trade panel styles
public/landing.html             +auth.js include (brand-band chip)
public/portfolio.html           +Trade card (own portfolio only)
public/god.html                 +trade box under the price chart
public/community.html           +priority form username lock-in
tools/check_stream_ready.py     +web login readiness probe
tools/test_web_session.py       NEW — 11-test token suite
tools/test_web_trade.py         NEW — 19-test endpoint suite
main.py                         PublicWebServer receives economy=
```

---

## v2.7.1 Update — Social Tabs, Dashboard Refunds, Visibility Toggle

Three smaller features shipped together (June 2026):

**Social tabs (Phase 5 of TODO.md, design in `Social_Tabs_Plan.md` —
now built).** The landing page's Twitch-only embed became a four-tab
strip: Twitch (only visible while live; LIVE pill; auto-selected on
go-live unless the visitor picked another tab that session), YouTube
(latest 12 uploads as cards, click plays inline), TikTok (embed of the
manually-configured `TIKTOK_LATEST_VIDEO_URL` + profile link), and
Bluesky (latest 10 posts with images + like/reply counts). Last active
tab persists in localStorage. Backend: three cached read-only
endpoints in `core/public_webserver.py`; stale cache is served on
upstream failure so a dead API never blanks a tab. YouTube uses the
1-unit `playlistItems` call, NOT the 100-unit `search.list` —
~96 quota units/day at the 15-min TTL. After posting a new TikTok,
paste its URL into `config_local.py:TIKTOK_LATEST_VIDEO_URL`.

**Priority-request panel (control panel, port 8069).** Full-width
card listing the last 50 Stripe payments (status / PLAYED tag) with a
REFUND button on paid/fulfilled rows. Refund flow: confirm dialog ->
`POST /api/action {action: refund_priority, session_id}` ->
`PriorityRequestPlugin.refund_session()` creates a REAL Stripe refund
and applies the same local bookkeeping as the `charge.refunded`
webhook (shared `_apply_refund_locally()` helper — the later webhook
delivery no-ops on the already-refunded status). The CLI
(`tools/reconcile_stripe.py refund`) remains for bot-down scenarios.

**Leaderboard visibility toggle.** The `leaderboard_opt_out` column
and its query filters existed since the airtight pass, but nothing
ever flipped the flag (the documented `!hideme` chat command was never
implemented). Now: logged-in viewers see a
`[LEADERBOARD: VISIBLE/HIDDEN]` button on their own portfolio page —
`POST /api/me/visibility` flips all their portfolio rows, and
`_add_shares` inserts new rows inheriting the user's current setting
so hidden users stay hidden when they buy a god they have never held.

Tests: `tools/test_priority_request.py` grew to 18 (manual refund,
list_payments); `tools/test_web_trade.py` to 20 (visibility round-trip
+ guards).

---

## v2.8 Update — Streamloots Hub + Factorio Mod (hatmas-events)

Two halves of one goal (June 2026): Streamloots cards as interactive
gameplay triggers, starting with Factorio. Same concept as the old
Streamloots-cards-into-Minecraft setup, rebuilt natively on HatmasBot.

### Streamloots event hub (`plugins/streamloots.py`)

Full behavior documented in **Streamloots Card Events** under
Automated Features. Summary: SSE listener on
`https://widgets.streamloots.com/alerts/<STREAMLOOTS_ALERT_ID>/media-stream`
(the unofficial-but-stable surface MixItUp/Firebot use), normalized
listener-list dispatch (`add_redemption_listener` /
`add_purchase_listener` / `add_gift_listener`), fields read by name,
raw event log at `data/streamloots_events.jsonl`, 15-min sock_read
dead-connection watchdog, 4xx fail-fast (config_error) vs 5xx/429
backoff, `streamloots` feature toggle gating dispatch only.

Going-live checklist (one-time):

1. Streamloots dashboard -> Alerts -> "Click here to show URL" ->
   copy the GUID at the end.
2. `config_local.py`: `STREAMLOOTS_ALERT_ID = "<guid>"` (treat as a
   secret — anyone with it can read the alert feed).
3. Restart the bot; expect `[Streamloots] Stream connected`.
4. Dashboard TEST STREAMLOOTS button (or play a real card) and watch
   the `[Streamloots] Card played:` log + the jsonl file.

The protocol was verified against three independent open-source
clients (MixItUp dev's sample, streamloots-events, Firebot) but NOT
yet against a live capture — the first real card play confirms it.
If the live format differs, `data/streamloots_events.jsonl` contains
exactly what the parser needs to adapt.

### Factorio mod (`factorio_mod/hatmas-events/`)

Standalone Factorio 2.x / Space Age mod, fully testable solo via
console commands before any bot wiring. Architecture constraint:
Factorio mods are sandboxed Lua (no sockets, no file reads), so the
bridge is RCON in (bot -> `remote.call("hatmas", ...)`) and
`helpers.write_file` out (mod appends JSON lines to
`script-output/hatmas/events.jsonl`; bot tails it). RCON requires
hosting the save as multiplayer (`--rcon-port 27015 --rcon-password
<pw>`). Console commands and RCON permanently disable achievements on
that save.

**Viewer pets (`scripts/pets.lua`).** One pet biter per owner, owner's
name floating above it. Follows the streamer (walks at >6 tiles,
catch-up teleport at >60, respawns on the new surface after a planet
hop — units cannot teleport cross-surface). Never seeks fights
(`distraction.none`) but biters on the player force defend themselves.
Friendly-fire immune via on_entity_damaged heal-back, with oversized
HP pools (750/1500/3000/6000 across small/medium/big/behemoth
`hatmas-pet-*` prototypes) so bursts don't one-shot them. Dies to
enemies: death announced in game chat + outbox event with owner,
lifetime, killer. Sizes upgrade via entity rebuild (grow path).

**Boss biter (`scripts/boss.lua`).** `hatmas-boss-biter`: behemoth
clone, 40k HP, 1.6x sprite scale, red tint, 2x damage modifier, 0.8x
speed, drops raw fish. Spawns N/S/E/W of the streamer at a given
distance (default 150 tiles, chunks force-generated), attack-moves to
the streamer's position. Floating "<viewer>'s Boss" tag + scripted HP
bar (LuaRenderObject rectangles, fill width rewritten on damage, red
below 25%). Enrages at 25% HP (1.5x speed). Death announced with
time-alive and final blow + outbox event.

**Remote interface (`control.lua`):** ping, spawn_pet(owner, name,
size), upgrade_pet(owner), remove_pet(owner), pet_say(owner, msg),
list_pets(), spawn_boss(viewer, direction, distance). All return "ok"
or a plain error string. Console test commands: /hatmas-pet,
/hatmas-pet-grow, /hatmas-pet-say, /hatmas-pet-remove, /hatmas-boss.

**Outbox events:** pet_spawned, pet_upgraded, pet_removed, pet_died,
boss_spawned, boss_enraged, boss_died — one JSON object per line,
every payload includes `event` + `tick`.

The mod was written against the 2.0.x Lua API docs (verified:
`storage` table object storage, `helpers.write_file`/`table_to_json`,
`LuaCommandable.set_command`, `LuaRenderObject` mutability,
ScriptRenderTarget entity+offset targets). One undocumented edge to
playtest: whether the heal-back prevents a true one-shot (point-blank
nuke) on a pet — if not, that stays as an accepted (funny) edge or
gets a guard. Tuning knobs and a first-playtest checklist are in the
mod's README.md.

### Factorio bot plugin (`plugins/factorio/`)

Bot half of the bridge. `rcon.py` is a dependency-free asyncio Source
RCON client (auth handshake incl. the empty-RESPONSE_VALUE quirk,
serialized commands, one transparent reconnect-and-retry per command —
covers the game restarting between commands; tested against a fake
RCON server). `catalog.py` builds `/silent-command
rcon.print(tostring(remote.call("hatmas", ...) or 'ok'))` lines with
proper Lua string escaping of untrusted viewer text (quotes,
backslashes, newlines), and formats outbox events into plain chat
lines. `events.py` tails `<script-output>/hatmas/events.jsonl`
(1s poll, byte-offset tracking, starts at EOF so history never
replays, partial-line safe, truncation-tolerant). `plugin.py` glues it
together: card handling with per-card cooldowns (cooldown is NOT
burned when Factorio is unreachable; viewers get one rate-limited
"not reachable" chat notice), mod errors relayed to chat ("no pet for
x"), successful plays silent in chat because the mod's outbox
announcement covers them, `factorio` feature toggle, status in
/api/state. No chat commands.

**Card manager (`/factorio/cards` + `cards.py`).** One place to wire
card names to actions. Mappings persist to `data/factorio_cards.json`
(CardStore) - FACTORIO_CARD_MAP in config only seeds the file on
first run, after that the JSON wins, so renaming a card in the
Streamloots dashboard is a UI edit. Lookups are case-insensitive and
whitespace-trimmed. The page lists mappings (edit/save/test/delete +
add row), shows the action catalog with descriptions, and - the
setup shortcut - a "recently played cards" panel fed by the
Streamloots hub's recent_cards tracking: play any card on your own
page, it appears with a mapped/unmapped pill, click MAP to prefill
the exact name. TEST buttons fire the mapped action through the real
RCON path (bypassing cooldowns) and show the mod's response.

Going-live checklist (one-time):

1. Install the mod (junction — see factorio_mod/hatmas-events/README.md).
2. `config_local.py`: `FACTORIO_RCON_PASSWORD = "<password>"` (done June 2026).
3. Launch via `start_factorio.bat` (finds factorio.exe, binds RCON to
   127.0.0.1:27015 with the config password) and host the save as
   multiplayer. Verify with `python tools/check_factorio_rcon.py`.
4. Open http://localhost:8069/factorio/cards -> expect RCON
   "connected". Use a TEST button -> pet/boss appears in-game; its
   outbox events appear in Twitch chat.
5. Create your Streamloots cards with whatever names you like, play
   each once, then map them from the "recently played" panel on the
   card manager page.

### Files added or significantly modified in v2.8

```
plugins/streamloots.py               NEW - SSE listener + event hub
core/config.py                       STREAMLOOTS_ALERT_ID + streamloots feature toggle
core/webserver.py                    test_streamloots action; streamloots status in /api/state
overlays/control_panel.html          TEST STREAMLOOTS button + status line
main.py                              StreamlootsPlugin registration (plugin 18)
factorio_mod/hatmas-events/          NEW - the Factorio mod (info.json, data.lua,
                                     control.lua, scripts/{common,outbox,pets,boss}.lua,
                                     locale/en/hatmas.cfg, README.md)
plugins/factorio/                    NEW - bot half (plugin.py, rcon.py, catalog.py,
                                     events.py). Config: FACTORIO_* in core/config.py,
                                     factorio feature toggle, status in /api/state.
```

### Next steps (not yet built)

- More mod events for the card collection: directional raid waves,
  supply drops, sabotage events (see brainstorm in chat history /
  mod README "next steps").
- Per-card cooldowns currently live in FACTORIO_CARD_MAP; if more
  consumers need cooldowns, promote a shared card router into the
  Streamloots hub.
- Streamloots card art/collection (rarity tiers map naturally to
  event sizes: common = scout raid, legendary = boss).

---

## v2.8.1 Update — Overnight Hardening Pass (2026-07-02)

An unattended overnight review of all 106 Python files (local-LLM
first pass on the RTX 5090, every finding verified against source by
Claude before acting), followed by 23 fix commits. No feature changes;
everything below is correctness, integrity, or responsiveness.
StreamingSpaceGame was excluded. Full per-commit detail lived in
MORNING_REPORT.md on the `overnight-2026-07-01` branch.

### Security

- **Whispered commands now enforce `mod_only`.** The whisper path in
  `core/bot.py` checked command enablement and cooldowns but never
  `mod_only` — any viewer could run `!spin`, `!godclear`, `!scene`,
  `!poolclear`, etc. by whispering the bot. Whisper payloads carry no
  badge info, so after the fix only the broadcaster (is_mod name
  fallback) can run mod commands via whisper.

### Money-path integrity (Hats, shares, channel points)

- **`execute_buy` / `execute_sell` compensate on partial failure**
  (plugins/economy/trading.py). Buy: if the portfolio or ledger write
  fails after MixItUp deducted hats, the granted shares are removed
  and the hats refunded. Sell: shares are removed before the MixItUp
  credit (reliable local DB first, flaky HTTP second) and restored at
  their original avg cost if the credit fails. Failed compensations
  log CRITICAL lines.
- **Per-user trade lock.** `_user_trade_lock` in trading.py serializes
  balance-check -> deduct -> write for the same user across every entry
  point (chat, website, tests). Previously only `/api/trade` held a
  lock, so concurrent chat+web trades could double-spend.
- **Sell payouts round exactly** (`int(round(...))`, not `int(...)`) —
  float round-trips shorted sellers a hat. Sell-"all" (chat + web)
  also rounds, so no dust holding survives closing a position.
- **Dividends only ledger credits MixItUp accepted**
  (plugins/economy/dividends.py). A failed credit is skipped loudly;
  if EVERY credit failed, the dividends row is not written, leaving
  the match_id unclaimed so the settle-time catch-up retries later.
- **Match settlement commits atomically** (match.py + db.py).
  `_update_price` used to commit mid-settlement, opening a crash
  window where the W/L/KDA aggregate bump persisted without the
  processed_matches dedup claim — a retry would double-count the match
  into the price formula. `_update_price(commit=False)` defers to the
  single end-of-settlement commit.
- **Gamble cooldown is claimed before the awaited balance fetch**
  (plugins/gamble.py). Two rapid `!gamble all` messages could both
  pass the cooldown check and double-bet a balance that covered one
  wager. Validation failures release the claim so typos don't burn it.
- **Voiceline redemptions refund when the god has no voice-line files**
  (plugins/voicelines.py) — previously only the no-god-selected case
  refunded; missing files just kept the viewer's points.

### Responsiveness (event-loop freezes)

- **@HatmasBot no longer freezes the bot** — claude_chat.py now uses
  `anthropic.AsyncAnthropic`; the sync client blocked the entire loop
  for the full API round-trip on every mention.
- **TTS no longer freezes the bot** — `trigger_tts` runs gTTS (a
  blocking HTTPS call to Google + mp3 write) via `asyncio.to_thread`
  in a background task.
- **tracker.gg calls serialized onto one executor thread**
  (plugins/smite/plugin.py, max_workers 2 -> 1) — all calls share one
  curl_cffi Session and curl handles are not safe across threads.

### Crash-safety and lifecycle

- **`core/atomic_io.py` (NEW)**: `atomic_write_json` / `atomic_write_text`
  (tmp + `os.replace`). Every plugin state file converted: godrequest
  queue + history (holds paid entries), songrequest likes/queue/
  blacklist/state/history + Spotify token, killdetector KDA state,
  voicelines reward map, death counter, gamble jackpot, suggestions,
  Claude history, custom commands, Discord announce state, smite
  session state + title templates, Factorio card store. Twitch token
  files in token_manager.py and auth.py use the same pattern (a
  corrupted token file means manual re-auth, since Twitch rotates
  refresh tokens).
- **`EconomyPlugin.cleanup()` reconstructed** — plugins/economy/plugin.py
  was truncated on disk mid-comment (the file-tool desync documented
  at the top of this file) since at least the v2.5 catch-up commit.
  It compiled anyway (docstring = valid body), so cleanup silently did
  nothing: backfill task never cancelled, MixItUp session never closed.
- **overlay_manager broadcast fix** — `_send` iterated the live WS
  client set across awaits; a client connecting/dropping mid-broadcast
  raised RuntimeError and killed the broadcast for everyone. Now
  iterates a snapshot. token_manager.close() also awaits its cancelled
  validation task.
- **process_recordings.py** releases the dashboard VOD-processor panel
  in a `finally`, so a mid-batch crash can't leave it stuck at
  "processing".

### Tests

- **Three existing suites could never finish unattended** — they hung
  at exit on leaked aiosqlite worker threads (non-daemon), with output
  stuck in the stdio buffer: tests/test_economy.py (missing
  close_db()), tools/test_priority_request.py (per-test in-memory
  connections never closed). All now exit 0 and are safe to wire into
  Stream Deck buttons or pre-deploy checks.
- **tests/test_trading_hardening.py (NEW)** — 4 regression tests:
  buy refund on portfolio-write failure, sell share-restore on credit
  failure, exact sell rounding, per-user lock vs concurrent
  double-spend. Standalone: `python tests/test_trading_hardening.py`.
- Suite status at merge: test_economy, test_trading_hardening,
  test_web_session (11), test_web_trade (20), test_priority_request
  (18) — all exit 0. test_kda_fixture needs digit templates in data/
  and can't run from a fresh worktree.

### Known-and-deferred (decisions, not oversights)

- `ECONOMY_POSITION_LIMIT` still unenforced in execute_buy (also
  listed in HATMAS_MARKET_AIRTIGHT_DESIGN.md).
- Shared-DB implicit transactions: interleaved coroutines can commit
  each other's half-done multi-statement sequences. The atomic-
  settlement fix closed the worst case; a `db_transaction()`
  async-lock helper would close the rest.
- Partial dividend failure (some credits fail, some succeed) records
  the successes and claims the match — failed holders aren't retried.
- nsfw_check fails open on vision-API errors; gamble win/loss
  `_adjust_balance` results are unchecked (announces regardless);
  claude_chat history JSON grows unbounded; obs.py drives the sync
  obsws client inline (localhost, ms-scale).

### Files added or significantly modified in v2.8.1

```
core/atomic_io.py                    NEW - atomic JSON/text writes
core/bot.py                          whisper mod_only enforcement
core/token_manager.py                atomic token writes; close() awaits task
core/auth.py                         atomic token writes
core/overlay_manager.py              WS broadcast snapshot
core/webserver.py                    TTS generation off-loop
core/public_webserver.py             sell-all rounding
plugins/economy/trading.py           per-user lock + compensation + rounding
plugins/economy/dividends.py         ledger-what-was-paid
plugins/economy/match.py + db.py     atomic settlement commit
plugins/economy/plugin.py            cleanup() reconstructed (was truncated)
plugins/economy/commands.py          sell-all rounding
plugins/claude_chat.py               AsyncAnthropic
plugins/gamble.py                    cooldown claimed pre-await
plugins/godrequest.py                spin fulfillment announcement
plugins/voicelines.py                refund on missing voice-line files
plugins/smite/plugin.py              single-thread tracker executor
plugins/ (13 files)                  atomic state writes via core/atomic_io
tests/test_trading_hardening.py      NEW - 4 regression tests
tests/test_economy.py                exits cleanly (close_db)
tools/test_priority_request.py       exits cleanly (per-test close)
tools/process_recordings.py          dashboard stop in finally
main.py                              banner v2.8
```
