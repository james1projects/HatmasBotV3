# HatmasBot

Complete reference for HatmasBot. This file is the single source of truth for Claude when working on this codebase across sessions.

**v2.3 | April 2026 | Built by Hatmaster & Claude**

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
- gTTS for server-side text-to-speech
- MixItUp Developer API v2 on localhost:8911 for currency (Hats) and inventory (God Tokens)

## Architecture

Plugin system. Each plugin has `setup(bot)`, `on_ready()`, `cleanup()`. Bot core (core/bot.py) handles EventSub subscriptions, command routing, and plugin management. Web server (core/webserver.py) serves overlays as OBS browser sources and a control panel dashboard. State is shared via `/api/state` JSON endpoint, polled by overlays every 1s. Config in core/config.py with local overrides in core/config_local.py (gitignored).

## Plugin Registration Order (main.py)

1. BasicPlugin - chat commands, suggestion box
2. SmitePlugin - match tracking, god detection, predictions, title updates (receives token_manager)
3. SongRequestPlugin - Spotify/YouTube song requests with queue and likes
4. OBSPlugin - scene switching, source control, fade effects
5. GodRequestPlugin - god request queue with MixItUp token economy
6. ClaudeChatPlugin - AI chat responses via Claude API (per-user history, safety)
7. GamblePlugin - wager Hats on dice rolls with jackpot pool
8. KillDeathDetector - real-time kill/death/assist detection via OBS screenshot analysis + template matching

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
  basic.py                  !hello, !commands, !uptime, !socials, !suggest, !suggestions, !clearsuggestions
  smite.py                  Match tracking, god detection, predictions, title, record, commands.
  songrequest.py            Spotify/YouTube queue, likes, now playing overlay, blacklist.
  obs.py                    OBS WebSocket control, scene switching, fade effects.
  godrequest.py             God request queue, token economy, MixItUp integration, auto-complete.
  claude_chat.py            Claude API responses for @mentions. Per-user history, safety prompt.
  gamble.py                 Dice roll gambling with Hats currency, jackpot pool, sound/visual alerts.
  killdetector.py           Real-time kill/death detection via OBS screenshot analysis + template matching (Tesseract OCR fallback).
  snap.py                   Thanos snap. Times out random half of chat.
overlays/
  control_panel.html        Dashboard for stream management.
  nowplaying.html           Now Playing overlay (450x120).
  youtube_player.html       Hidden YouTube audio player.
  god_overlay.html          God match data overlay.
  sound_alerts.html         Gamble dice roll animation + sound effects.
  tts.html                  TTS overlay for highlighted messages. Plays gTTS audio, shows message.
  kills.html                Kill/death event overlay. Shows kill type popups + K/D counter.
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
  nsfw_cache.json           NSFW album art classification cache.
  twitch_token.json         Bot OAuth token (auto-refreshed).
  twitch_broadcaster_token.json  Broadcaster OAuth token (auto-refreshed).
  tts_audio/                Generated TTS MP3 files (auto-cleaned, keeps last 30).
  killdetect_debug/         Debug frame archives (timestamped subfolders, preserved for regression testing).
tools/
  obs_screenshot.py         Captures OBS "Smite 2" source screenshot for calibration. Saves full frame + KDA crop.
```

---

## EventSub Subscriptions (bot.py setup_hook)

All subscribed when both owner_id and bot_id are set:

1. ChatMessageSubscription - all chat messages
2. WhisperReceivedSubscription - incoming whispers
3. ChannelSubscribeSubscription - new subs (god token awards)
4. ChannelSubscribeMessageSubscription - resub messages (god token awards)
5. ChannelSubscriptionGiftSubscription - gift subs (god token awards to gifter)
6. ChannelRaidSubscription(to_broadcaster_user_id=OWNER_ID) - incoming raids (auto-shoutout)

---

## Commands Reference

### Basic (basic.py)
| Command | Access | Description |
|---------|--------|-------------|
| !hello | Everyone | Greets user |
| !commands | Everyone | Lists available commands (filters mod-only for non-mods) |
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
The tracker.gg API has a 2-5 minute delay before returning god data after a match starts. To show the god portrait immediately, the KillDeathDetector runs a portrait matcher on each game screenshot. It crops the in-game god portrait (bottom-center HUD, below K/D/A bar, region 635,942 to 715,1022 at 1920x1080) and compares it against a library of reference icons using HSV histogram correlation (cv2.compareHist with HISTCMP_CORREL). When a match is found above 0.80 confidence, it fires immediately — setting the OBS portrait, announcing in chat, and updating the stream title. When tracker.gg eventually responds, it confirms the detection and adds team/stats data without re-announcing. Reference icons are downloaded from the tracker.gg CDN via `download_god_icons.py` and stored in `data/god_icons/`. The matcher is initialized in KillDeathDetector.on_ready and runs on each frame in the detection loop until a god is identified for the current match.

### Stream Title Placeholders
Templates in TITLE_TEMPLATE_GOD and TITLE_TEMPLATE_LOBBY support:
- `{god}` - current god name
- `{command}` - rotates through TITLE_COMMAND_ROTATION every 5min (configurable)
- `{record}` - daily W-L (e.g. "3-1")
- `{song}` - current song ("Title - Artist" or "No song playing")

### Song Request Automation
Polls Spotify every 3s. Pushes next song to queue 30s before current ends (gapless playback). Coordinates Spotify pause/resume with YouTube playback. Auto-skips blacklisted songs. Detects Spotify disconnection, slows polling, notifies chat on reconnect. Now Playing overlay auto-hide relies on the `is_playlist` field in every `update_now_playing()` call: playlist songs auto-hide after SR_PLAYLIST_AUTO_HIDE_SECONDS (8s), requested songs stay visible. All update paths (startup restore, YouTube playback, Spotify monitor) include this field.

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

### Gamble Sound + Visual Alerts
Triggers on wagers >= 1000 Hats (GAMBLE_ALERT_MIN_WAGER). Jackpot always triggers. OBS overlay at /overlay/sound_alerts shows dice roll animation (1.5s cycling), then result with color-coded popup. Sounds synthesized via Web Audio API. Multiple rapid gambles queue server-side (/api/gamble_queue drain pattern). Auto-hides after 5s.

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
| /api/suggestions | GET | All viewer suggestions |

### Actions (POST /api/action)
toggle_feature, skip_song, youtube_ended, youtube_started, youtube_progress, snap, go_live, stop_stream, resolve_prediction, set_title_templates, update_title_now, record_result, test_sound, test_kill, test_death, test_tts, add_rotation_command, remove_rotation_command, send_chat, god_donation, god_skip, god_clear, clear_suggestions, start_kill_detect, stop_kill_detect, kd_toggle_debug, kd_toggle_announce

---

## Feature Toggles

All controllable via dashboard or API. Default: all enabled.

song_requests, predictions, snap, claude_chat, smite_tracking, gamble, now_playing_overlay, auto_scene_switch, auto_title, god_requests, auto_shoutout, tts_highlights, kill_detection

---

## Token Manager (core/token_manager.py)

Manages bot + broadcaster OAuth tokens. On startup: loads persisted tokens from data/, validates both, refreshes expired ones. Background task validates every ~50min. On 401: immediate refresh + retry. Persists refreshed tokens to data/twitch_token.json and data/twitch_broadcaster_token.json. Updates config module values in memory. Refresh throttled to 30s minimum per token.

Used by: SmitePlugin (title updates, predictions), bot.py (raid shoutout API calls).

## Authentication

- Bot token: `python -m core.auth` (log in as HatmasBot)
- Broadcaster token: `python -m core.auth --broadcaster` (log in as Hatmaster)
- Spotify: auto-generated on first run via browser OAuth

### Broadcaster Scopes
channel:manage:broadcast, channel:manage:predictions, channel:read:subscriptions, moderator:manage:shoutouts

---

## Key Config Values

| Setting | Description |
|---------|-------------|
| TWITCH_BOT_ID / TWITCH_OWNER_ID | Bot and broadcaster user IDs |
| TITLE_COMMAND_ROTATION | List of commands to cycle in {command} placeholder |
| TITLE_COMMAND_ROTATION_INTERVAL | Seconds between rotations (default: 300) |
| GAMBLE_ALERT_MIN_WAGER | Min wager for sound/visual alerts (default: 1000) |
| GAMBLE_CURRENCY_NAME | Must match MixItUp currency name ("Hats") |
| SHOUTOUT_MIN_VIEWERS | Min raid viewers for auto-shoutout (default: 1) |
| SHOUTOUT_COOLDOWN | Seconds between shoutouts to same user (default: 120) |
| TTS_MAX_LENGTH | Max chars for TTS messages (default: 300) |
| SR_PLAYLIST_AUTO_HIDE_SECONDS | Seconds before playlist overlay hides (default: 8) |
| MIXITUP_API_BASE | MixItUp API URL (default: localhost:8911) |
| MIXITUP_INVENTORY_NAME / ITEM_NAME | Must match MixItUp e