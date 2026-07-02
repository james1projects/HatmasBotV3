# HatmasBot v2.8

A Twitch chat bot, stream-automation suite, and companion website for Hatmaster's Smite 2 stream. Built by Hatmaster & Claude, April–June 2026.

> `HatmasBot.md` is the full technical reference and single source of truth. This README is the quick tour.

## Features

- **Kill/Death/Assist Tracker** — Real-time KDA detection from OBS screenshots via template-based digit matching, with overlay popups for kills, multi-kills, deaths, and assists. Works in practice, customs, and ranked.
- **Early God Detection** — Identifies the current god from the in-game portrait via HSV histogram matching, 2–5 min faster than tracker.gg, and reconciles if tracker.gg later disagrees.
- **Smite 2 Stats** — Live tracker.gg lookups (`!stats`, `!god`, `!damage`, `!kda`, `!rank`, `!team`, `!record`, …).
- **Auto Predictions & Scene Switching** — Detects live matches, opens channel-point predictions, switches OBS scenes, and rewrites the stream title from templates.
- **Song Requests** — Dual Spotify/YouTube queue with likes, vote-skip, and a Now Playing overlay; gapless Spotify↔YouTube handoff.
- **God Requests** — Viewers spend God Tokens to request gods, with auto-complete when the god is detected in-match.
- **Priority God Requests (Stripe)** — Viewers pay $5 on `hatmaster.tv/community` to jump the queue, with a crash-safe webhook lifecycle and refund/dispute handling.
- **Hatmas Market** — A stock market for gods: viewers buy/sell shares in Hats, prices settle on Hatmaster's verified match results, dividends pay holders, and 7 overlays render the ticker, trades, dividends, leaderboard, and portfolios.
- **God Pool / Spin Wheel** — Viewers `!nominate` gods into a pool; a mod `!spin` picks a weighted-random winner with a slot-machine overlay. Also triggerable silently from a Stream Deck (see below).
- **Gamble** — Wager Hats on a dice roll with a jackpot pool and sound/visual alerts.
- **Voice Line Redemptions** — Channel-point rewards play a random god voice line (plus optional MP4 animation), with prompts that auto-update to the current god.
- **TTS Highlights** — Highlighted messages read aloud via gTTS with an on-screen display.
- **Claude Chat** — `@HatmasBot` triggers AI responses with the Hatmaster personality.
- **Daily Death Counter** — Auto-resetting death tally overlay across every gameplay session.
- **Streamloots Hub** — Listens to the alert SSE stream and dispatches card redemptions, chest purchases, and gifts to consumers.
- **Factorio Integration** — The `hatmas-events` Factorio mod (viewer pets, boss biters) driven by Streamloots cards.
- **Discord Bridge** — Cross-posts stream status / events to Discord.
- **Public Website** — `hatmaster.tv`: a home page with the live Twitch embed + YouTube/TikTok/Bluesky tabs, the Hatmas Market at `/market` (god prices, portfolios, trading with Twitch login), and the community/god-request page.
- **Auto-Shoutout on Raid**, **OBS Control**, and a browser **Control Panel** for your second monitor.

Offline tooling also lives in `tools/`: a VOD highlight pipeline, end-of-stream recording sorter, YouTube live-badge thumbnail swap, and a matchup thumbnail builder. See `HatmasBot.md` for those.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Targets Python 3.14 on Windows but runs on 3.10+. Optional extras (Tesseract OCR fallback, `.psd` thumbnail export) are commented at the bottom of `requirements.txt`.

### 2. Configure secrets

```bash
cp core/config_local_example.py core/config_local.py
```

Edit `config_local.py` with your tokens, or set environment variables:

- `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
- `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`
- `ANTHROPIC_API_KEY`
- `OBS_WS_PASSWORD`
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` (priority god requests)
- `STREAMLOOTS_ALERT_ID` (Streamloots hub)
- Discord bot token (Discord bridge)

### 3. Authenticate Twitch

```bash
python -m core.auth                # bot account
python -m core.auth --broadcaster  # your channel — predictions, channel points, chatters
```

Each opens a browser for OAuth. Tokens are saved and auto-refresh.

### 4. Authenticate Spotify

Set your Spotify credentials in config, then authorize once. The access/refresh token is stored at `data/spotify_token.json` and auto-refreshes on every run.

### 5. Run

```bash
python main.py
```

Type `quit`, `exit`, `stop`, or `close` in the console for clean shutdown. Ctrl+C also works.

### 6. OBS browser sources

Add these from the dashboard server on port **8069**:

- **Now Playing**: `http://localhost:8069/overlay/nowplaying`
- **Kill Events**: `http://localhost:8069/overlay/kills`
- **God Overlay**: `http://localhost:8069/overlay/god`
- **Sound Alerts**: `http://localhost:8069/overlay/sound_alerts`
- **TTS**: `http://localhost:8069/overlay/tts`
- **Voice Lines**: `http://localhost:8069/overlay/voicelines`
- **Death Counter**: `http://localhost:8069/overlay/deaths`
- **Spin Wheel**: `http://localhost:8069/overlay/spin`
- **Economy** (ticker, live, match-end, dividend, leaderboard, tradefeed, portfolio): `http://localhost:8069/overlay/economy_*`
- **Control Panel**: open `http://localhost:8069/` on your second monitor.

### Stream Deck: spin without chat

Bind a key to `http://localhost:8069/api/spin` (GET or POST) to run the wheel exactly like `!spin` but without posting to Twitch chat. Use a background HTTP-request action (e.g. BarRaider's **Web Requests** plugin) so no browser window opens. If the pool is empty or every god is already queued, the spin overlay flashes a short toast so the button never looks dead.

## Commands

### Everyone

| Command | Description |
|---------|-------------|
| `!god [name]` | Current god stats, or look up any god |
| `!stats` / `!kda` / `!damage` | Ranked Conquest stats, KDA ratios, damage |
| `!rank` / `!winrate` / `!record` | SR + tier, win %, today's W-L |
| `!match` / `!team` / `!lastmatch` | Live match info, team comp, last result |
| `!sr <song/URL>` | Request a song (Spotify name/URL or YouTube URL) |
| `!song` / `!songlist` / `!songstatus` | Now playing, queue, your wait times |
| `!like` / `!voteskip` / `!wrongsong` | Like, vote-skip, remove your last request |
| `!mysongs` / `!toprequester` / `!topsongs` | Song-likes leaderboards |
| `!godrequest <god>` | Spend 1 God Token to request a god |
| `!godqueue` / `!godlist` / `!godtokens` | Queue preview, full queue, token balance |
| `!nominate <god>` / `!pool` | Add a god to the spin pool (1/day), view the pool |
| `!buy [god] [amt\|all]` / `!sell [god] [amt\|all]` | Trade god shares for Hats |
| `!portfolio` / `!price [god]` | Holdings + P&L; current price and trend |
| `!market` / `!stocks` / `!dividend` | Top movers; latest dividend |
| `!gamble <amt\|all\|half\|quarter>` / `!jackpot` | Wager Hats; show jackpot pool |
| `!hello` / `!uptime` / `!socials` / `!suggest <text>` | Misc |
| `@HatmasBot` | Talk to Claude AI |

### Mods only

| Command | Description |
|---------|-------------|
| `!skip` / `!blacklistsong` | Skip / blacklist a song |
| `!scene [name]` / `!overlay <on/off/auto>` | OBS scene + Now Playing overlay |
| `!godreq <god>` / `!godskip` / `!remove <pos>` / `!godclear` | Manage the god queue |
| `!spin` / `!poolclear` | Spin the wheel / wipe the pool |
| `!suggestions` / `!clearsuggestions` | View / clear suggestions |
| `!discordstatus` / `!discordtest` | Discord bridge status / test |

## Architecture

Plugin-based. Each plugin implements `setup(bot)`, `on_ready()`, and `cleanup()`, and registers commands with `bot.register_command()`. Two aiohttp servers run side by side: the **dashboard** on `8069` (control panel, OBS overlays, and the `POST /api/action` endpoints that mutate state) and a deliberately read-only **public** server on `8070` (GETs + WebSocket only). A Cloudflare Tunnel points only at `8070`, so the dashboard is unreachable from the internet.

```
main.py                — Entry point; registers ~21 plugins; graceful shutdown
core/
  bot.py               — Twitch EventSub connection & command router
  config.py            — Settings and secrets (override in config_local.py)
  token_manager.py     — OAuth token management with auto-refresh
  webserver.py         — Dashboard server (overlays, API, control panel) :8069
  public_webserver.py  — Read-only public site (hatmaster.tv) :8070
  overlay_manager.py   — WebSocket overlay rules engine
  auth.py              — Twitch OAuth browser flow
  god_matcher.py       — God identification via HSV histogram matching
  digit_matcher.py     — Template-based digit recognition for KDA numbers
  nsfw_check.py        — Album art NSFW classification
plugins/
  basic, smite/, songrequest, godrequest, priority_request,
  gamble, claude_chat, obs, killdetector, deathcounter,
  voicelines, economy/, god_pool, streamloots, factorio/,
  discord_bridge, youtube_rewards, stream_status,
  youtube_live_badge, backup_manager, custom_commands
overlays/              — OBS browser-source HTML + shared theme/client
public/                — hatmaster.tv front-end (login, trading, community)
tools/                 — Ops + offline pipelines (VOD highlights, thumbnails, etc.)
data/                  — Runtime data (gitignored)
```

## Adding new features

Create a file in `plugins/`, implement a class with `setup(bot)` / `on_ready()` / `cleanup()`, register commands with `bot.register_command()`, and add it to `main.py`. Overlays connect through `core/overlay_manager.py` with rules in `core/overlay_rules.json`.

For full technical details, see `HatmasBot.md`.
