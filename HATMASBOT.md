# HatmasBot

Complete reference for HatmasBot. This file is the single source of truth for Claude when working on this codebase across sessions.

**v2.4 | April 2026 | Built by Hatmaster & Claude**

---

## Tone and Style

All chat messages, overlay text, and any text that appears on screen must read like a straightforward bot — no emojis, no flair, no AI-sounding language. Keep it plain and informational. For example: "Player Kill! KDA: 18/27/0" not "⚔️ PLAYER KILL! KDA: 18/27/0". Same applies to announcements, notifications, and any user-facing strings throughout the codebase.

---

## Tech Stack

- Python 3.14, TwitchIO v3.2.1 (EventSub WebSocket)
- aiohttp web server on port 8069
- obsws-python for OBS WebSocket control
- spotipy for Spotify playback
- yt-dlp for YouTube audio
- curl_cffi for Cloudflare bypass on tracker.gg
- aiosqlite for async SQLite (economy database, WAL mode)
- gTTS for server-side text-to-speech
- MixItUp Developer API v2 on localhost:8911 for currency (Hats) and inventory (God Tokens)

## Architecture

Plugin system. Each plugin has `setup(bot)`, `on_ready()`, `cleanup()`. Bot core (core/bot.py) handles EventSub subscriptions, command routing, and plugin management. `on_ready()` is called in plugin registration order, so plugins registered later (e.g. OBS) are not yet connected when earlier plugins (e.g. Smite) run their `on_ready()`. Any cross-plugin startup work that depends on OBS must use a background task that polls for OBS connection readiness. Web server (core/webserver.py) serves overlays as OBS browser sources and a control panel dashboard. State is shared via `/api/state` JSON endpoint, polled by overlays every 1s. Config in core/config.py with local overrides in core/config_local.py (gitignored).

## Plugin Registration Order (main.py)

1. BasicPlugin - chat commands, suggestion box
2. SmitePlugin - match tracking, god detection, predictions, title updates (receives token_manager)
3. SongRequestPlugin - Spotify/YouTube song requests with queue and likes
4. OBSPlugin - scene switching, source control, fade effects
5. GodRequestPlugin - god request queue with MixItUp token economy
6. ClaudeChatPlugin - AI chat responses via Claude API (per-user history, safety)
7. GamblePlugin - wager Hats on dice rolls with jackpot pool
8. KillDeathDetector - real-time kill/death/assist detection via OBS screenshot analysis + template matching
9. VoiceLinePlugin - channel point redemptions for god-specific jokes, taunts, and laughs with optional MP4 animations
10. DeathCounterPlugin - daily death tally with auto-reset at midnight, powers the /overlay/deaths browser source
11. EconomyPlugin - stock-market-style god economy with live price ticking, dividends, trading, and 7 websocket overlays

SnapPlugin exists but is commented out in main.py.

Bot also receives `token_manager` directly for raid shoutout and TTS API calls.

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
  cache.py                  Simple TTL cache.
  nsfw_check.py             Album art NSFW classification.
  god_matcher.py            Portrait-based god identification via HSV histogram matching.
  digit_matcher.py          Template-based digit recognition for KDA numbers. XOR distance + hole-count pre-filter.
plugins/
  basic.py                  !hello, !uptime, !socials, !suggest, !suggestions, !clearsuggestions
  smite.py                  Match tracking, god detection, predictions, title, record, commands.
  songrequest.py            Spotify/YouTube queue, likes, now playing overlay, blacklist.
  obs.py                    OBS WebSocket control, scene switching, fade effects.
  godrequest.py             God request queue, token economy, MixItUp integration, auto-complete.
  claude_chat.py            Claude API responses for @mentions. Per-user history, safety prompt.
  gamble.py                 Dice roll gambling with Hats currency, jackpot pool, sound/visual alerts.
  killdetector.py           Real-time kill/death detection via OBS screenshot analysis + template matching (Tesseract OCR fallback).
  voicelines.py             Channel point redemptions for god voice lines (jokes, taunts, laughs) with MP4 animation support.
  deathcounter.py           Daily death tally with auto-reset at midnight. Hooks killdetector on_death, powers /overlay/deaths.
  economy.py                Stock market economy: god share prices, live ticks, dividends, trading, 7 overlay events.
  snap.py                   Thanos snap. Times out random half of chat.
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
  economy.db                SQLite database for god economy (WAL mode, async via aiosqlite).
tools/
  obs_screenshot.py         Captures OBS "Smite 2" source screenshot for calibration. Saves full frame + KDA crop.
  download_voicelines.py    Downloads all god voice lines from the Smite fandom wiki. Uses curl_cffi for Cloudflare bypass.
  seed_economy.py           Seeds economy DB with realistic price history from tracker.gg stats. Usage: python tools/seed_economy.py [gods...] [--force]
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
Detected in event_message by checking `payload.type == "channel_points_highlighted"`. Message text is sent to webserver's trigger_tts() which generates an MP3 via gTTS and queues it. Overlay at /overlay/tts polls /api/tts_queue, plays audio via Web Audio API (AudioContext + decodeAudioData for OBS compatibility), shows purple message box with username + text + sound wave animation. Auto-hides after speech ends. Queue processes one at a time. Audio files stored in data/tts_audio/, auto-cleaned to last 30 files. Controllable via `tts_highlights` feature toggle. Max message length: 300 chars (TTS_MAX_LENGTH).

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

**Fallback method — per-component Tesseract OCR:** If template matching fails for any individual component, that component falls back to Tesseract PSM 10 (single character) with dilated and original image variants. Other components that matched templates keep their results. This handles double-digit numbers where the "1" in the tens place may not match any template.

**Auto-collection:** After sanity checks pass, confirmed digit crops are saved as new templates (max 20 per digit). The template library grows automatically over time.

**State persistence:** KDA state saves to `data/kda_state.json` on every successful read (K/D/A values, match stats, timestamp). On startup, if the saved state is less than 30 minutes old (`STATE_STALE_SECONDS`), it's restored as the baseline. If older, the detector starts fresh. This allows mid-game bot restarts without losing KDA tracking.

**Startup validation:** The first `STARTUP_REQUIRED_READS` (3) consecutive reads must all agree before the baseline is accepted. If restored state exists, the validated read is compared against it — if live reads >= saved values, the delta is applied; if live reads < saved values (new match or bad data), it starts fresh.

**Sanity checks:** Rejects any frame where KDA decreases (misread). Rejects any frame where a single value jumps by more than `MAX_KDA_JUMP_BASE` (5) + seconds_elapsed * `MAX_KDA_JUMP_PER_SEC` (1/3) — scales with time since last successful read. Non-gameplay screen detection checks HUD area for low std/mean — if missing for 3 consecutive frames, assumes game ended and resets stats.

**Multi-kill classification:** Tracks kill timestamps within MULTIKILL_WINDOW (10s) to classify double, triple, quadra, and penta kills. Batched kills (+2 or more from missed frames) clear the window and fire as player_kill.

**Chat announcements:** Optional toggle sends Twitch chat message on each kill, death, or assist with updated KDA line.

**Callbacks:** on_kill, on_multikill, on_death, on_assist — set in main.py to push events to the webserver overlay queue.

**Debug:** set_debug(True) saves crops to data/killdetect_debug/ (archived into timestamped subfolders on each session start). File logging writes all [KillDetector] output to data/killdetect.log (overwritten per session, flushed on every write).

**Overlay:** /overlay/kills polls /api/kill_events, shows popup (red for kills, gold for multi-kills, gray for deaths) with K/D counter. Events and stats available via /api/kill_stats (includes running, debug, announce_chat, ocr_available, last_kda, last_read_ago). Test via actions: test_kill (with optional kill_type param), test_death. Debug panel actions: start_kill_detect, stop_kill_detect, kd_toggle_debug, kd_toggle_announce.

**Dependencies:** Pillow, opencv-python, numpy. Optional: pytesseract + Tesseract binary (fallback OCR, not needed if template library has full digit coverage).

**Calibration tool:** `tools/obs_screenshot.py` captures an OBS screenshot from the exact "Smite 2" source at 1920x1080 and saves a KDA crop for coordinate verification.

### Voice Line Redemptions (voicelines.py + webserver + voicelines.html)
Three channel point rewards (God Joke 500pts, God Taunt 500pts, God Laugh 200pts) auto-created on Twitch on first startup (requires `channel:manage:redemptions` broadcaster scope, re-auth with `python -m core.auth --broadcaster` if needed). Reward IDs are persisted to `data/voiceline_rewards.json` so they're not recreated each launch. The EventSub subscription is created manually in `bot.py setup_hook()` via `_manual_channel_points_subscribe()` because TwitchIO v3.2.1 picks the wrong token for this subscription type (see EventSub section above). When redeemed, picks a random .ogg from the current god's voice line folder (e.g. `data/smite_voicelines/achilles/jokes/`). If an MP4 animation exists in `data/smite_animations/<god>/joke.mp4` (or `laugh.mp4`, `taunt.mp4`), it plays alongside the audio in the OBS overlay. The overlay at /overlay/voicelines polls /api/voiceline_events, plays video+audio together, fades out when both finish (15s safety timeout). Events queue so rapid redemptions play in sequence. Current god is tracked via the kill detector's portrait matcher and the SmitePlugin god detection — the last known god is used as a fallback between matches (defaults to Sylvanus on a fresh install). Voice lines are pre-downloaded via `python tools/download_voicelines.py` (uses curl_cffi for Cloudflare bypass).

**Dynamic reward prompts:** The reward prompts (descriptions viewers see when clicking the reward) auto-update on Twitch whenever the current god changes. Templates in `REWARD_DEFS["prompt_template"]` use `{god}` as a placeholder — "Play a joke for {god}", "Play a taunt for {god}", "{god} laughs". `set_current_god()` schedules `_update_reward_prompts()` as a background task whenever a new god is detected, which PATCHes each reward via `PATCH /helix/channel_points/custom_rewards`. On startup, `_ensure_rewards()` also calls the prompt updater so restored state from disk is reflected on Twitch. Note: Twitch's API does not allow updating the reward image/icon, only the prompt, title, cost, color, and other text fields.

### Gamble Sound + Visual Alerts
Triggers on wagers >= 100 Hats (GAMBLE_ALERT_MIN_WAGER). Jackpot always triggers. OBS overlay at /overlay/sound_alerts shows dice roll animation (1.5s cycling), then result with color-coded popup. Sounds synthesized via Web Audio API. Multiple rapid gambles queue server-side (/api/gamble_queue drain pattern). Auto-hides after 5s.

### God Economy / Hatmas Market (economy.py + 7 overlays)
Stock-market-style system where viewers invest Hats in Smite 2 gods as shares. God prices move based on Hatmaster's match performance. Full details in CURRENT_TASK.md.

**Database:** SQLite via `aiosqlite` (async, WAL mode). Tables: `god_prices` (current prices, games played), `price_history` (timestamped for sparklines), `portfolios` (per-viewer holdings with avg cost), `transactions` (buy/sell/dividend/free_share), `dividends` (payout records). Path: `data/economy.db`.

**Match lifecycle:** God detected → 5% dividend to holders → live price ticks (+1.5% per kill, -2% per death, +0.5% per assist) → match ends → W/L + final KDA locks price → 1 free share to all viewers in chat. Trading is always open — viewers can buy/sell any god at any time, not just the current one.

**Twitch viewer list:** Free share distribution uses the Helix `GET /helix/chat/chatters` API with pagination (up to 1000 per page) to get all users connected to chat, including lurkers. Requires `moderator:read:chatters` broadcaster scope. Falls back to TwitchIO's IRC-based `channel.chatters` if the API call fails. Also includes existing portfolio holders (who may have closed chat but invested before). The `token_manager` is passed to EconomyPlugin from main.py for authenticated Helix calls.

**Price formula:** Win base +3% to +15% depending on KDA, loss base -5% to -13%. Volatility multiplier scales by games played (1-4 games: 2x penny stock, 20+: 1x blue chip). Price floor at 10 hats. Starting price 100 hats.

**No transaction fees** — removed to keep it fun. Position limit: max 30% of total hats in one god.

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

### Actions (POST /api/action)
toggle_feature, skip_song, youtube_ended, youtube_started, youtube_progress, snap, go_live, stop_stream, resolve_prediction, set_title_templates, update_title_now, record_result, test_sound, test_kill, test_death, test_tts, add_rotation_command, remove_rotation_command, send_chat, god_donation, god_skip, god_clear, clear_suggestions, start_kill_detect, stop_kill_detect, kd_toggle_debug, kd_toggle_announce, sim_economy (with optional force + god/outcome/kills/deaths/assists params), test_overlay (overlay param: ticker/dividend/leaderboard/portfolio/tradefeed/match_end), reload_prices

---

## Feature Toggles

All controllable via dashboard or API. Default: all enabled.

song_requests, predictions, snap, claude_chat, smite_tracking, gamble, now_playing_overlay, auto_scene_switch, auto_title, god_requests, auto_shoutout, tts_highlights, kill_detection, voicelines, economy

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
| ECONOMY_TRANSACTION_FEE | Fee per trade, 0.0 = disabled (default: 0.0) |
| ECONOMY_POSITION_LIMIT | Max % of hats in one god (default: 0.30) |
| ECONOMY_DIVIDEND_RATE | Dividend % paid on god pick (default: 0.05) |
| ECONOMY_KILL_TICK / DEATH_TICK / ASSIST_TICK | Live price change per event (+1.5%, -2%, +0.5%) |
| ECONOMY_FREE_SHARE_COUNT | Free shares to chatters on win (default: 1) |