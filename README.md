# HatmasBot v2.0

A Twitch chat bot for Hatmaster's stream. Built by Hatmaster & Claude, April 2026.

## Features

- **Smite 2 Stats** — Live stat lookups via tracker.gg (`!stats`, `!god`, `!damage`, `!kda`)
- **Auto Predictions** — Detects live matches and creates channel point predictions
- **Song Requests** — Dual Spotify/YouTube with queue, likes system, and Now Playing overlay
- **The Snap** — Randomly eliminates half of chat with Thanos-style OBS integration
- **Claude Chat** — `@HatmasBot` triggers AI responses with Hatmaster personality
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

### 6. OBS Setup

Add browser sources in OBS:
- **Now Playing**: `http://localhost:8069/overlay/nowplaying` (450x120)
- **Control Panel**: Open `http://localhost:8069/` in your browser (second monitor)

## Commands

### Everyone
| Command | Description |
|---------|-------------|
| `!stats` | Your ranked conquest stats |
| `!god <name>` | Stats for a specific god |
| `!damage` | Total damage stats (the flex command) |
| `!kda` | KDA ratios |
| `!rank` | Win/loss record |
| `!match` | Check if currently in a match |
| `!sr <song>` | Request a song (name, Spotify URL, or YouTube URL) |
| `!song` | What's currently playing |
| `!songlist` | View the queue |
| `!wrongsong` | Remove your last request |
| `!like` | Like the current song |
| `!mysongs` | Your song request stats |
| `!toprequester` | Top requesters by likes |
| `!snapstats` | Snap statistics |
| `!commands` | List all commands |
| `!hello` | Say hi |
| `!uptime` | Bot uptime |
| `!socials` | Social media links |
| `@HatmasBot` | Talk to Claude AI |

### Mods Only
| Command | Description |
|---------|-------------|
| `!skip` | Skip current song |
| `!snap` | Execute the snap |
| `!scene <name>` | Switch OBS scene |
| `!overlay <on/off/auto>` | Control Now Playing overlay |

## Architecture

```
hatmasbot/
  main.py              — Entry point
  core/
    bot.py             — Twitch connection & command router
    config.py          — All settings and secrets
    auth.py            — Twitch OAuth generator
    cache.py           — API response caching
    webserver.py       — Local server for overlays & control panel
  plugins/
    basic.py           — Simple commands
    smite.py           — Smite 2 stat lookups & predictions
    songrequest.py     — Spotify/YouTube queue management
    snap.py            — The Thanos snap
    obs.py             — OBS WebSocket control
    claude_chat.py     — Claude AI chat responses
  overlays/
    nowplaying.html    — Now Playing browser source
    control_panel.html — Mission control dashboard
  data/                — Runtime data (gitignored)
```

## Adding New Features

Create a new file in `plugins/`. Implement a class with `setup(bot)` method.
Register commands with `bot.register_command()`. Add it to `main.py`.
That's it.
