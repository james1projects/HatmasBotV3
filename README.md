# HatmasBot v2.3

A Twitch chat bot for Hatmaster's Smite 2 stream. Built by Hatmaster & Claude, April 2026.

## Features

- **Kill/Death/Assist Tracker** — Real-time KDA detection from OBS screenshots using template-based digit matching, with overlay popups for kills, multi-kills, deaths, and assists
- **Early God Detection** — Identifies the current god from the in-game portrait via HSV histogram matching, 2-5 min faster than tracker.gg
- **Smite 2 Stats** — Live stat lookups via tracker.gg (`!stats`, `!god`, `!damage`, `!kda`)
- **Auto Predictions** — Detects live matches and creates channel point predictions
- **Song Requests** — Dual Spotify/YouTube with queue, likes system, and Now Playing overlay
- **God Requests** — Viewers spend God Tokens to request gods, with auto-complete detection
- **Gamble** — Wager Hats on dice rolls with jackpot pool, sound/visual alerts via OBS overlay
- **TTS Highlights** — Highlighted messages read aloud via gTTS with on-screen display
- **Claude Chat** — `@HatmasBot` triggers AI responses with Hatmaster personality
- **The Snap** — Randomly eliminates half of chat with Thanos-style OBS integration
- **OBS Control** — Scene switching, overlay management, stream automation
- **Control Panel** — Browser-based mission control for your second monitor

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

Also install yt-dlp for YouTube support:
```bash
pip install yt-dlp
```

### 2. Configure secrets

Copy and edit the config:
```bash
cp core/config.py core/config_local.py
```

Edit `config_local.py` with your actual tokens and secrets.
Or set environment variables:
- `TWITCH_CLIENT_ID`
- `TWITCH_CLIENT_SECRET`  
- `TWITCH_BOT_TOKEN`
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `ANTHROPIC_API_KEY`
- `OBS_WS_PASSWORD`

### 3. Authenticate Twitch

```bash
python -m core.auth
```

This opens a browser for OAuth. Tokens are saved and auto-refresh.

### 4. Authenticate Spotify

```bash
python spotify_auth.py
```

### 5. Run

```bash
python main.py
```

Type `quit`, `exit`, `stop`, or `close` in the console for clean shutdown. Ctrl+C also works.

### 6. OBS Setup

Add browser sources in OBS:
- **Now Playing**: `http://localhost:8069/overlay/nowplaying` (450x120)
- **Kill Events**: `http://localhost:8069/overlay/kills` (kill/death/assist popups + K/D counter)
- **God Overlay**: `http://localhost:8069/overlay/god` (god match data)
- **Sound Alerts**: `http://localhost:8069/overlay/sound_alerts` (gamble dice roll + sounds)
- **TTS**: `http://localhost:8069/overlay/tts` (highlighted message readout)
- **Control Panel**: Open `http://localhost:8069/` in your browser (second monitor)

## Commands

### Everyone
| Command | Description |
|---------|-------------|
| `!stats` | Your ranked conquest stats |
| `!god <name>` | Stats for a specific god |
| `!damage` | Total damage stats (the flex command) |
| `!kda` | KDA ratios |
| `!rank` | Current SR and rank tier |
| `!match` | Check if currently in a match with live KDA |
| `!record` | Today's W-L record |
| `!winrate` | Ranked win percentage |
| `!team` | All players on team with gods and KDA |
| `!lastmatch` | Last completed match results |
| `!sr <song>` | Request a song (name, Spotify URL, or YouTube URL) |
| `!song` | What's currently playing |
| `!songlist` | View the queue |
| `!wrongsong` | Remove your last request |
| `!like` | Like the current song |
| `!voteskip` | Vote to skip (5 votes needed) |
| `!mysongs` | Your song request stats |
| `!toprequester` | Top requesters by likes |
| `!topsongs` | Top 5 most-liked songs |
| `!godrequest <god>` | Spend a God Token to request a god |
| `!godqueue` | Next 5 gods in queue |
| `!godtokens` | Check your token balance |
| `!gamble <amount>` | Wager Hats on a dice roll |
| `!jackpot` | Show current jackpot pool |
| `!commands` | List all commands |
| `!hello` | Say hi |
| `!uptime` | Bot uptime |
| `!socials` | Social media links |
| `!suggest <text>` | Submit a suggestion |
| `@HatmasBot` | Talk to Claude AI |

### Mods Only
| Command | Description |
|---------|-------------|
| `!skip` | Skip current song |
| `!blacklistsong` | Blacklist current or specific song |
| `!scene <name>` | Switch OBS scene |
| `!overlay <on/off/auto>` | Control Now Playing overlay |
| `!godreq <god>` | Add god to queue free |
| `!godskip` | Remove next god from queue |
| `!suggestions` | View recent suggestions |

## Architecture

Plugin-based system. Each plugin has `setup(bot)`, `on_ready()`, `cleanup()`. Web server serves overlays as OBS browser sources. State shared via `/api/state` JSON endpoint.

```
main.py                — Entry point, plugin registration, graceful shutdown
core/
  bot.py               — Twitch EventSub connection & command router
  config.py            — All settings and secrets
  token_manager.py     — OAuth token management with auto-refresh
  webserver.py         — aiohttp server for overlays, API, control panel
  auth.py              — Twitch OAuth browser flow
  cache.py             — API response caching
  god_matcher.py       — God identification via HSV histogram matching
  digit_matcher.py     — Template-based digit recognition for KDA numbers
  nsfw_check.py        — Album art NSFW classification
plugins/
  basic.py             — Simple commands (!hello, !commands, !suggest, etc.)
  smite.py             — Match tracking, god detection, predictions, stats
  songrequest.py       — Spotify/YouTube queue with likes system
  killdetector.py      — Real-time KDA detection via OBS screenshots
  godrequest.py        — God request queue with token economy
  gamble.py            — Dice roll gambling with jackpot pool
  claude_chat.py       — Claude AI chat responses
  obs.py               — OBS WebSocket control
  snap.py              — The Thanos snap
overlays/              — OBS browser source HTML files
data/                  — Runtime data (gitignored)
tools/
  obs_screenshot.py    — OBS screenshot capture for KDA calibration
```

## Adding New Features

Create a new file in `plugins/`. Implement a class with `setup(bot)`, `on_ready()`, and `cleanup()` methods. Register commands with `bot.register_command()`. Add it to `main.py`. That's it.

For full technical details, see `HatmasBot.md`.
