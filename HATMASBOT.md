# HatmasBot

Complete reference for HatmasBot. This file is the single source of truth for Claude when working on this codebase across sessions.

**v2.1 | April 2026 | Built by Hatmaster & Claude**

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
8. KillDeathDetector - real-time kill/death/assist detection via OBS screenshot OCR

SnapPlugin exists but is commented out in main.py.

Bot also receives `token_manager` directly for raid shoutout and TTS API calls.

KillDeathDetector hooks into SmitePlugin's match lifecycle: starts on match start, stops on match end. It uses OBS WebSocket to grab screenshots of the "Smite 2" source every 0.8s and reads the K/D/A numbers from the HUD bar using OCR (connected component analysis + Tesseract). When K, D, or A increases, it fires callbacks that push events to the webserver queue for the overlay. Optional chat announcement toggle sends KDA updates to Twitch chat.

---

## File Structure

```
main.py                     Entry point. Creates TokenManager, WebServer, Bot, registers plugins.
core/
  bot.py                    HatmasBot class. EventSub subs, command routing, raid handler, TTS handler.
  config.py                 All settings. Loads config_local.py at bottom.
  token_manager.py          Async OAuth token manager with auto-refresh for bot + broadcaster tokens.
  webserver.py              aiohttp server. Overlays, API, control panel, TTS/gamble queues.
  auth.py                   OAuth browser flow for bot and broadcaster tokens.
  cache.py                  Simple TTL cache.
  nsfw_check.py             Album art NSFW classification.
plugins/
  basic.py                  !hello, !commands, !uptime, !socials, !suggest, !suggestions, !clearsuggestions
  smite.py                  Match tracking, god detection, predictions, title, record, commands.
  songrequest.py            Spotify/YouTube queue, likes, now playing overlay, blacklist.
  obs.py                    OBS WebSocket control, scene switching, fade effects.
  godrequest.py             God request queue, token economy, MixItUp integration, auto-complete.
  claude_chat.py            Claude API responses for @mentions. Per-user history, safety prompt.
  gamble.py                 Dice roll gambling with Hats currency, jackpot pool, sound/visual alerts.
  killdetector.py           Real-time kill/death detection via OBS screenshot analysis + OCR.
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
  godreq_queue.json         God request queue.
  godreq_history.json       Completed god requests with status.
  snap_stats.json           Snap statistics.
  spotify_token.json        Spotify access/refresh tokens.
  gamble_jackpot.json       Current jackpot pool.
  claude_history.json       Per-user Claude conversation history.
  nsfw_cache.json           NSFW album art classification cache.
  twitch_token.json         Bot OAuth token (auto-refreshed).
  twitch_broadcaster_token.json  Broadcaster OAuth token (auto-refreshed).
  tts_audio/                Generated TTS MP3 files (auto-cleaned, keeps last 30).
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

### Stream Title Placeholders
Templates in TITLE_TEMPLATE_GOD and TITLE_TEMPLATE_LOBBY support:
- `{god}` - current god name
- `{command}` - rotates through TITLE_COMMAND_ROTATION every 5min (configurable)
- `{record}` - daily W-L (e.g. "3-1")
- `{song}` - current song ("Title - Artist" or "No song playing")

### Song Request Automation
Polls Spotify every 3s. Pushes next song to queue 30s before current ends (gapless playback). Coordinates Spotify pause/resume with YouTube playback. Auto-skips blacklisted songs. Detects Spotify disconnection, slows polling, notifies chat on reconnect.

### Auto-Shoutout on Raid (bot.py)
Fires on ChannelRaidSubscription. Fetches raider's last game via GET /helix/channels. Sends chat message with raider name, viewer count, and last game. Calls POST /helix/chat/shoutouts for official shoutout card. 120s per-raider cooldown. Configurable min viewers (SHOUTOUT_MIN_VIEWERS). Controllable via `auto_shoutout` feature toggle.

### TTS for Highlighted Messages (bot.py + webserver + tts.html)
Detected in event_message by checking `payload.type == "channel_points_highlighted"`. Message text is sent to webserver's trigger_tts() which generates an MP3 via gTTS and queues it. Overlay at /overlay/tts polls /api/tts_queue, plays audio via Web Audio API (AudioContext + decodeAudioData for OBS compatibility), shows purple message box with username + text + sound wave animation. Auto-hides after speech ends. Queue processes one at a time. Audio files stored in data/tts_audio/, auto-cleaned to last 30 files. Controllable via `tts_highlights` feature toggle. Max message length: 300 chars (TTS_MAX_LENGTH).

### God Request Auto-Complete
When smite plugin fires god_detected, godrequest plugin checks if detected god matches next in queue. If so, removes request, logs to history, announces in chat, advances OBS display.

### Kill/Death/Assist Detection (killdetector.py + webserver + kills.html)
Runs during active matches only (hooks into smite plugin match start/end callbacks). Can also be started/stopped manually from the debug panel for testing in jungle practice. Grabs OBS screenshots of the "Smite 2" source via GetSourceScreenshot every 0.8s. Detection method: KDA HUD number tracking via OCR.
- KDA region: Reads the K/D/A numbers from the HUD bar above the god portrait at pixel region (625, 905, 725, 932) on a 1920x1080 source. The bar shows sword icon + kills, skull icon + deaths, hand icon + assists.
- OCR pipeline: Crops the KDA region, scales 8x, Otsu binarization, connected component analysis to separate icons (h>88px) from digit blobs, groups digits into K/D/A by the two largest x-gaps between components, isolates each group, inverts to black-on-white, then runs Tesseract OCR (PSM 7, digit whitelist) on each group independently.
- Sanity checks: Rejects any frame where KDA decreases (OCR misread). Rejects any frame where a single value jumps by more than MAX_KDA_JUMP (5) in one read (catches misreads like "19" → "49"). Store/scoreboard filter skips frames when left 60% of screen has >65% dark pixels (KDA bar is hidden behind overlays).
- Multi-kill classification: Tracks kill timestamps within a MULTIKILL_WINDOW (10s) to classify double, triple, quadra, and penta kills.
- Chat announcements: Optional toggle ("Announce K/D/A in chat") sends a Twitch chat message on each kill, death, or assist with the updated KDA line.
- Callbacks: on_kill, on_multikill, on_death, on_assist — set in main.py to push events to the webserver overlay queue.
Overlay at /overlay/kills polls /api/kill_events, shows popup (red for kills, gold for multi-kills, gray for deaths) with K/D counter. Events and stats available via /api/kill_stats (includes running, debug, announce_chat, ocr_available, last_kda, last_read_ago). Test via actions: test_kill (with optional kill_type param), test_death. Debug panel actions: start_kill_detect, stop_kill_detect, kd_toggle_debug, kd_toggle_announce.
Dependencies: pytesseract + Tesseract binary (required), Pillow, opencv-python, numpy.

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
| MIXITUP_INVENTORY_NAME / ITEM_NAME | Must match MixItUp exactly ("God Tokens" / "God Token") |
| SMITE2_GOD_IMAGES_DIR | Folder with god portrait images |

---

## God Name Matching

Fuzzy match against 100+ Smite 2 gods. Resolution: exact match, then starts-with, then contains. Normalizes input (lowercase, strips apostrophes/hyphens). Returns None if ambiguous.

## OBS Sources

- GodImage / GodBackground - god portrait + team-colored background (in configured scene/group)
- GodReqImage / GodReqText - next requested god portrait + name (in configured scene/group)
- NowPlaying - now playing overlay
- SnapOverlay - snap animation

## Queue/Drain Pattern

Used for gamble alerts and TTS. Server appends to a list, caps at 20. Dedicated GET endpoint returns the list and clears it. Overlay polls every 500ms, drains into local queue, processes one at a time. Prevents missed events during rapid activity.
