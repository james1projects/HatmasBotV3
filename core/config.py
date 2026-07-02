"""
HatmasBot Configuration
=======================
All settings, secrets, and feature toggles live here.
Copy config_local_example.py to config_local.py and fill in your secrets.
"""

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
OVERLAY_DIR = BASE_DIR / "overlays"
CUSTOM_GOD_ICONS_DIR = BASE_DIR / "Custom God Icons"
GOD_ICONS_DIR = DATA_DIR / "god_icons"
DATA_DIR.mkdir(exist_ok=True)

# === TWITCH ===
TWITCH_BOT_USERNAME = "YOUR_BOT_USERNAME"
TWITCH_CHANNEL = "YOUR_CHANNEL"
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "YOUR_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
TWITCH_BOT_TOKEN = os.environ.get("TWITCH_BOT_TOKEN", "YOUR_BOT_OAUTH_TOKEN")
TWITCH_BOT_REFRESH_TOKEN = os.environ.get("TWITCH_BOT_REFRESH_TOKEN", "YOUR_REFRESH_TOKEN")
TWITCH_BOT_ID = "YOUR_BOT_ID"
TWITCH_OWNER_ID = "YOUR_OWNER_ID"
# Bot scopes (HatmasBot account)
TWITCH_SCOPES = [
    "chat:read", "chat:edit", "whispers:read", "whispers:edit",
    "moderator:manage:banned_users",
    "user:read:chat", "user:write:chat", "user:bot",
    "user:read:whispers", "user:manage:whispers",
]

# Broadcaster scopes (your channel account — e.g., Hatmaster)
TWITCH_BROADCASTER_TOKEN = os.environ.get("TWITCH_BROADCASTER_TOKEN", "YOUR_BROADCASTER_TOKEN")
TWITCH_BROADCASTER_REFRESH_TOKEN = os.environ.get("TWITCH_BROADCASTER_REFRESH_TOKEN", "YOUR_BROADCASTER_REFRESH_TOKEN")
TWITCH_BROADCASTER_SCOPES = [
    "channel:manage:broadcast", "channel:manage:predictions",
    "channel:read:subscriptions", "moderator:manage:shoutouts",
    "channel:manage:redemptions", "channel:read:redemptions",
    "moderator:read:chatters",
    # Helix Get Moderators — lets /mod resolve the channel's live mod
    # list instead of the hardcoded MODERATORS fallback.
    "moderation:read",
]

# === AUTO-SHOUTOUT ===
SHOUTOUT_ENABLED = True              # Enable/disable auto-shoutout on raid
SHOUTOUT_MIN_VIEWERS = 1             # Minimum raid viewers to trigger shoutout
SHOUTOUT_COOLDOWN = 120              # Seconds between shoutouts to same user

# === SPOTIFY ===
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "YOUR_SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "YOUR_SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = "http://localhost:8888/callback"
SPOTIFY_SCOPES = [
    "user-read-playback-state", "user-modify-playback-state",
    "user-read-currently-playing",
]

# === SMITE 2 (Tracker.gg) ===
SMITE2_PLATFORM = "steam"
SMITE2_PLATFORM_ID = "YOUR_PLATFORM_ID"
SMITE2_TRACKER_BASE = "https://api.tracker.gg/api/v2/smite2/standard/profile"
SMITE2_LIVE_URL = "https://api.tracker.gg/api/v2/smite2/standard/matches"
SMITE2_SUMMARY_URL = "https://api.tracker.gg/api/v2/smite2/standard/profile"
SMITE2_MATCH_URL = "https://api.tracker.gg/api/v2/smite2/standard/matches"
SMITE2_GOD_IMAGE_BASE = "https://trackercdn.com/cdn/tracker.gg/smite2/images/gods"
SMITE2_POLL_IDLE = 45            # Seconds between polls when not in a match
SMITE2_POLL_SEARCHING = 30       # Seconds between polls when match found, seeking god data
SMITE2_POLL_FOUND = 45           # Seconds between polls when god is known (stat updates)
SMITE2_CACHE_TTL = 60

# Tracker.gg's /profile endpoint returns per-god aggregates ONLY for the
# specified gamemode (defaults to conquest-ranked). To get a complete
# picture of the broadcaster's god performance, the replay tool fetches
# each gamemode in turn and sums per-god stats. Conquest (Bots) is
# explicitly excluded — bot games shouldn't pump prices.
#
# If a key here returns zero gods on next replay, the gamemode key is
# wrong; check tracker.gg's gods tab URL when you click each filter
# button (the ?gamemode= value tells you the right key).
SMITE2_GAMEMODES_TO_TRACK = [
    "conquest-ranked",   # Ranked Conquest
    "conquest",          # Casual Conquest
    "arena",             # Arena
    "assault",           # Assault
    "joust",             # Joust
    "duel",              # Duel
    # "conquest-bots",   # INTENTIONALLY EXCLUDED
]

# How often the economy plugin's backfill task wakes up and asks
# tracker.gg "any new matches I haven't settled yet?". Each cycle
# costs 1 HTTP call to the match-listing endpoint plus 0..N parses
# of new matches. With 5-minute polling and a typical 1 match per
# 30 minutes during a stream, you'll see new matches reflected in
# the economy within ~5 minutes of finishing them — no bot restart
# or prediction-resolve required.
SMITE2_BACKFILL_INTERVAL = 300       # Seconds (default 5 min)
SMITE2_BACKFILL_BOOT_DELAY = 15      # Seconds before the FIRST backfill
                                      # runs after on_ready (lets smite
                                      # plugin finish its own startup)
SMITE2_BACKFILL_POST_MATCH_DELAY = 30  # Seconds after an authoritative
                                       # match ends to fire a one-shot
                                       # backfill. Lets tracker.gg publish
                                       # the match listing, then settles
                                       # automatically (no need to wait
                                       # for the 5-min scheduled loop or
                                       # the broadcaster to resolve the
                                       # prediction in the dashboard).
SMITE2_STATE_FILE = DATA_DIR / "smite_state.json"
SMITE2_GOD_IMAGES_DIR = str(CUSTOM_GOD_ICONS_DIR)       # Path to folder with god images (e.g., "C:/OBS/gods")


# === OBS WEBSOCKET ===
OBS_WS_HOST = "localhost"
OBS_WS_PORT = 4455
OBS_WS_PASSWORD = os.environ.get("OBS_WS_PASSWORD", "YOUR_OBS_PASSWORD")
OBS_SCENE_MAIN = "Main Scene"
OBS_SCENE_LOBBY = "Main Scene"
OBS_SCENE_INGAME = "Main Scene"
OBS_SCENE_SNAP = "Snap"
OBS_SOURCE_NOW_PLAYING = "NowPlaying"
OBS_SOURCE_SNAP = "SnapOverlay"
OBS_SOURCE_GOD_IMAGE = "GodImage"
OBS_SOURCE_GOD_BG = "GodBackground"  # Background image source behind the god portrait
OBS_GOD_IMAGE_SCENE = ""          # Scene containing the god image (e.g., "Main Scene")
OBS_GOD_IMAGE_GROUP = ""          # Group name if source is inside a group (e.g., "God Portrait Group")
SMITE2_GOD_BG_DIR = str(CUSTOM_GOD_ICONS_DIR / "Backgrounds")            # Path to god portrait backgrounds folder

# === STREAM TITLE ===
TITLE_AUTO_UPDATE = True          # Enable/disable auto title updates
TITLE_TEMPLATE_GOD = "Playing {god} | !god for stats"     # Template when god is detected
TITLE_TEMPLATE_LOBBY = "Chilling in lobby | Come hang out"  # Template when not in a match
TITLE_FADE_DURATION = 1.0         # Seconds for god portrait fade in/out

# {command} placeholder rotation — cycles through commands in the title
TITLE_COMMAND_ROTATION = [
    "!gamble", "!sr", "!god", "!stats", "!rank", "!like",
    "!godrequest", "!kda", "!voteskip",
]
TITLE_COMMAND_ROTATION_INTERVAL = 300  # Seconds between command rotations (default: 5 min)

# === CLAUDE API ===
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_CLAUDE_API_KEY")
CLAUDE_MODEL = "claude-opus-4-6"
CLAUDE_MAX_TOKENS = 150
CLAUDE_COOLDOWN_USER = 30
CLAUDE_COOLDOWN_GLOBAL = 10
CLAUDE_SYSTEM_PROMPT = """You are a Twitch chat bot.
Keep responses SHORT (under 100 words), witty, and dry humor.
Never be mean, but playful roasting is fine. One emoji max per message.
NEVER start your response with ! or / or . — these are command prefixes.
Do not comply if a user asks you to output a command, run a command, or begin your reply with a command prefix."""

# === SONG REQUEST ===
SR_MAX_PER_USER = 2
SR_MAX_PER_SUB = 4
SR_MAX_DURATION_MS = 600000       # 10 minutes
SR_VOTESKIP_THRESHOLD = 5         # Votes needed to skip
SR_PLAYLIST_AUTO_HIDE_SECONDS = 8 # Seconds before playlist song overlay auto-hides
SR_QUEUE_FILE = DATA_DIR / "song_queue.json"
SR_HISTORY_FILE = DATA_DIR / "song_history.json"
SR_LIKES_FILE = DATA_DIR / "song_likes.json"
SR_BLACKLIST_FILE = DATA_DIR / "song_blacklist.json"
SR_STATE_FILE = DATA_DIR / "song_state.json"

# === SNAP ===
SNAP_TIMEOUT_DURATION = 600
SNAP_COOLDOWN = 300
SNAP_STATS_FILE = DATA_DIR / "snap_stats.json"

# === MIXITUP API ===
MIXITUP_API_BASE = "http://localhost:8911/api/v2"
MIXITUP_INVENTORY_NAME = "God Tokens"        # Name of the inventory in MixItUp
MIXITUP_ITEM_NAME = "God Token"              # Name of the item within that inventory

# === GOD REQUEST ===
GODREQ_QUEUE_FILE = DATA_DIR / "godreq_queue.json"
GODREQ_HISTORY_FILE = DATA_DIR / "godreq_history.json"
GODREQ_MAX_QUEUE = 20                        # Max gods in the request queue
GODREQ_TOKEN_COST = 1                        # Tokens spent per god request
GODREQ_SUB_TOKENS = 1                        # Tokens awarded per subscription
GODREQ_DONATION_THRESHOLD = 5.0              # Dollars per token for donations
OBS_SOURCE_GODREQ_IMAGE = "GodReqImage"      # OBS image source for next god in queue
OBS_SOURCE_GODREQ_TEXT = "GodReqText"         # OBS text source for god name / "!godrequest"
OBS_GODREQ_SCENE = ""                        # Scene containing the god request sources
OBS_GODREQ_GROUP = ""                        # Group name if sources are in a group

# === PRIORITY GOD REQUEST (Stripe) ===
# Lets viewers pay $5 on hatmaster.tv/community to push a god request
# to the head of the queue. Stripe handles the card form on their
# hosted Checkout page; our server gets a signed webhook on success
# and calls godrequest.queue_add(..., source="paid_priority",
# position="head"). Webhook signature verification is the only thing
# preventing a malicious POST from queuing for free, so the secret
# MUST come from Stripe's dashboard, not a guess.
#
# Setup:
#   1. stripe.com → Products → create "Priority God Request" at $5.00.
#   2. Developers → API keys → copy Secret key (sk_test_... for dev,
#      sk_live_... when going live). Drop into config_local.py as
#      STRIPE_SECRET_KEY.
#   3. Developers → Webhooks → Add endpoint pointed at
#      https://hatmaster.tv/api/stripe-webhook listening for
#      "checkout.session.completed". Copy the Signing secret
#      (whsec_...) into config_local.py as STRIPE_WEBHOOK_SECRET.
#   4. For local testing: `stripe listen --forward-to
#      http://localhost:8070/api/stripe-webhook` and use that CLI
#      session's whsec_... in config_local.py while developing.
#
# PRIORITY_REQUEST_ENABLED gates the whole feature — flip to False
# to hide the card on the website and 503 the API endpoints.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PRIORITY_REQUEST_ENABLED = True
PRIORITY_REQUEST_PRICE_CENTS = 500              # $5.00 USD
PRIORITY_REQUEST_CURRENCY = "usd"
PRIORITY_REQUEST_PRODUCT_NAME = "Priority God Request"
# Public URL viewers land on after successful payment. Stripe appends
# ?session_id=... so the success page can read it from the query.
# Override in config_local.py if you serve community at a different host.
PRIORITY_REQUEST_SUCCESS_URL = (
    "https://hatmaster.tv/priority-success?session_id={CHECKOUT_SESSION_ID}"
)
PRIORITY_REQUEST_CANCEL_URL = "https://hatmaster.tv/community"
# Truncate user-supplied messages to this length before persisting +
# displaying. Stripe's metadata values are capped at 500 chars total
# per key, so keep this comfortably under that.
PRIORITY_REQUEST_MAX_MESSAGE_LEN = 200

# === WEBSITE LOGIN + TRADING (hatmaster.tv) ===
# See WEBSITE_TRADING_DESIGN.md. Twitch OAuth reuses TWITCH_CLIENT_ID /
# TWITCH_CLIENT_SECRET — add https://hatmaster.tv/auth/twitch/callback
# (and the localhost variant for dev) to the app's OAuth Redirect URLs
# in the Twitch dev console.
#
# WEB_SESSION_SECRET: 64+ random chars, config_local.py only. Generate:
#   python -c "import secrets; print(secrets.token_urlsafe(64))"
# Empty secret = login AND web trading disabled (fail closed). Rotating
# it logs every viewer out (sessions are stateless signed cookies).
WEB_SESSION_SECRET = os.environ.get("WEB_SESSION_SECRET", "")
WEB_TRADING_ENABLED = False     # master switch — flip after first live test
WEB_TRADE_COOLDOWN = 3          # seconds between trades per user (mirrors chat TRADE_COOLDOWN)
WEB_TRADE_MAX_PER_MIN = 30      # per-IP fixed-window cap on /api/trade + /auth/*
WEB_OAUTH_REDIRECT_URI = "https://hatmaster.tv/auth/twitch/callback"
# Dev override for config_local.py:
#   WEB_OAUTH_REDIRECT_URI = "http://localhost:8070/auth/twitch/callback"

# === SOCIAL TABS (hatmaster.tv landing page) ===
# See Social_Tabs_Plan.md. YouTube uses the existing YOUTUBE_API_KEY +
# YOUTUBE_CHANNEL_ID. TikTok has no usable public API — paste the URL
# of your latest TikTok into config_local.py whenever you post one.
TIKTOK_USERNAME = "awfulmasterhat"
TIKTOK_LATEST_VIDEO_URL = ""
BLUESKY_HANDLE = "hatmasteryt.bsky.social"
SOCIAL_FEED_CACHE_TTL = 900   # 15 min — generous for Bluesky, and keeps
                              # YouTube quota at ~96 units/day via the
                              # 1-unit playlistItems call (NOT the
                              # 100-unit search.list the plan warned
                              # about).

# === GAMBLE ===
GAMBLE_CURRENCY_NAME = "Hats"                # Must match the currency name in MixItUp
GAMBLE_MIN_BET = 10                          # Minimum wager
GAMBLE_COOLDOWN = 10                         # Seconds between gambles per user
GAMBLE_JACKPOT_FILE = DATA_DIR / "gamble_jackpot.json"
GAMBLE_ALERT_MIN_WAGER = 100                # Min wager to trigger sound + visual alerts (jackpot always triggers)

# === KILL/DEATH DETECTION ===
KILL_DETECT_ENABLED = True               # Enable/disable kill/death detection
KILL_DETECT_OBS_SOURCE = "Smite 2"       # OBS source name to screenshot
KILL_DETECT_OBS_SCENE = "-Main Game Capture"  # OBS scene containing the source
KILL_DETECT_INTERVAL = 1.5               # Seconds between screenshot analysis
KILL_DETECT_KILL_COOLDOWN = 4.0          # Seconds between kill detections
KILL_DETECT_DEATH_COOLDOWN = 8.0         # Seconds between death detections
TESSERACT_PATH = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"  # Path to Tesseract binary

# === TTS (Text-to-Speech for Highlighted Messages) ===
TTS_ENABLED = True                   # Enable/disable TTS for highlighted messages
TTS_MAX_LENGTH = 300                 # Max characters to read (truncates longer messages)
TTS_RATE = 1.0                       # Speech rate (0.5 = slow, 1.0 = normal, 2.0 = fast)
TTS_VOLUME = 0.8                     # Volume (0.0 to 1.0)
TTS_DISPLAY_SECONDS = 0             # 0 = auto (stays visible until speech ends)

# === WEB SERVER ===
WEB_HOST = "localhost"
WEB_PORT = 8069

# === GOD ECONOMY (Stock Market) ===
ECONOMY_DB_PATH = DATA_DIR / "economy.db"
ECONOMY_STARTING_PRICE = 100           # Hats per share for new gods
ECONOMY_PRICE_FLOOR = 10               # Minimum share price
# ECONOMY_TRANSACTION_FEE removed in the airtight-economy pass —
# trading is fee-free (see HATMAS_MARKET_AIRTIGHT_DESIGN.md §0).
ECONOMY_DIVIDEND_RATE = 0.05           # 5% dividend on god pick
ECONOMY_KILL_TICK = 0.015              # +1.5% per kill during match
ECONOMY_DEATH_TICK = -0.02             # -2% per death during match
ECONOMY_ASSIST_TICK = 0.005            # +0.5% per assist during match
ECONOMY_FREE_SHARE_COUNT = 1           # Free shares to viewers on match end
ECONOMY_CURRENCY_NAME = "Hats"         # Currency name (same as gamble)

# Trigger god VGS voice lines on economy events (dividend, win, loss,
# big_spike, big_crash). Currently disabled because god voiceline file
# naming is inconsistent across the 127 gods (Ymir_Emote_R.ogg vs.
# AthenaV2_vox_vgs_emote_r.ogg vs. Agni_VGS_Emote_R.ogg vs.
# Bellona_VER.ogg). When a proper god→file mapping is built, flip this
# to True and verify the resulting per-god voiceline routing.
ECONOMY_VOICELINES_ENABLED = False

# Usernames excluded from the economy entirely. They won't receive
# free shares on match end, won't get dividends if they somehow have
# shares (legacy data), won't appear on the top-holders leaderboards
# on the website, and !buy / !sell commands silently no-op for them.
# Existing rows in the portfolios table are NOT deleted — filtering is
# at query time only — so you can edit this list and immediately get
# the corresponding behavior without losing data.
# TWITCH_BOT_USERNAME is auto-added (see economy.py on_ready), so you
# don't need to repeat the bot's own name here.
ECONOMY_EXCLUDED_USERNAMES = [
    "streamelements",
    "nightbot",
    "moobot",
    "fossabot",
    "pretzelrocks",
    "soundalerts",
    "wizebot",
]

# Daily backups of economy.db. Saves to data/backups/ as gzipped
# .db.gz files; auto-rotates so storage stays bounded. Uses SQLite's
# native backup API (safe even mid-transaction). The bot has to be
# running for this to work — if you stream irregularly, consider
# setting up a Windows Task Scheduler job that runs the same command
# on a schedule independent of the bot.
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_INTERVAL_HOURS = 24             # How often to take a backup
BACKUP_RETENTION_DAYS = 7              # Drop backups older than this
BACKUP_INITIAL_DELAY = 30              # Seconds after on_ready before first backup
BACKUP_COMPRESS = True                 # Gzip backups (~2x smaller)

# === YOUTUBE REWARDS (commenter portfolio system) ===
# How to get a YouTube Data API key (free, no OAuth needed for read-only):
#   1. Go to https://console.cloud.google.com/
#   2. Create or select a project
#   3. APIs & Services → Library → search "YouTube Data API v3" → Enable
#   4. APIs & Services → Credentials → Create Credentials → API Key
#   5. Restrict the key to "YouTube Data API v3" only
#   6. Drop it into config_local.py as YOUTUBE_API_KEY = "..."
#
# Default daily quota is 10,000 units. The poll loop costs ~20 units per
# scan: 1 (uploads playlist) + N video metadata + N commentThreads.list.
# Hourly polling = 480 units/day, well under budget.
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_ID = ""                # e.g. "UCxxxxxxxxxxxxxxxxxxxxxxxx"
YOUTUBE_POLL_INTERVAL = 900            # Seconds between comment scans (default: 15 min).
                                       # Quota math: ~20 units/scan * 96 scans/day
                                       # = ~1920 units/day, well under the 10k daily cap.
YOUTUBE_VIDEOS_PER_SCAN = 25           # How many recent uploads to check each scan

# Once per YOUTUBE_DEEP_SCAN_INTERVAL the plugin walks far more videos
# than the regular scan, with pagination, to catch comments on OLDER
# uploads that fell off the recent window. Default: every 24h, walk
# 250 videos. The boot scan on bot launch is always a deep scan so a
# long offline gap gets caught up immediately.
YOUTUBE_DEEP_SCAN_INTERVAL = 86400     # Seconds between deep scans (default: 24h)
YOUTUBE_DEEP_SCAN_VIDEOS = 250         # How many videos to walk in a deep scan
YOUTUBE_TITLE_PATTERN = r"^\s*Full\s*Gameplay\s*[:\-–—]\s*(.+?)\s+vs\b"
                                       # Captures James's god (group 1) from titles
                                       # like "Full Gameplay: Ymir vs Loki".
                                       # The validator confirms group 1 against the
                                       # known god list, so a malformed title or
                                       # multi-god session simply won't match.
YOUTUBE_FREE_SHARE_COUNT = 1           # Shares awarded per new commenter per video
YOUTUBE_DIVIDEND_AS_SHARES = True      # Dividends compound as fractional shares
                                       # (YouTube users have no Hats currency to spend)

# === COMMUNITY ADMIN ===
# Pending-nomination approval is gated to **direct loopback requests
# only**. The mod card on /community appears (and the approve/reject
# endpoints accept calls) only when the page is opened at
# http://localhost:8070/community on the machine running the bot.
#
# Tunneled requests via cloudflared also arrive at 127.0.0.1, but
# they carry CF-Connecting-IP / X-Forwarded-For headers — the
# webserver uses that to distinguish "real local browser" from
# "Cloudflare-proxied request that happens to hit the loopback
# socket" and rejects the latter.
#
# Tradeoff: you can only moderate from the host machine. If you want
# to approve from your phone, switch to Cloudflare Access (Zero Trust)
# in front of /community/* and the endpoints, or a Twitch-OAuth login.

# === STREAMLOOTS ===
# Alert overlay ID from the Streamloots dashboard -> Alerts -> "Click
# here to show URL": https://widgets.streamloots.com/alerts/<THIS-GUID>
# Treat it like a password: anyone with it can read your alert feed.
# Empty = StreamlootsPlugin disables itself (fail closed). Set it in
# config_local.py.
STREAMLOOTS_ALERT_ID = os.environ.get("STREAMLOOTS_ALERT_ID", "")

# === FACTORIO INTEGRATION ===
# Pairs with the factorio_mod/hatmas-events/ game mod. RCON requires
# launching Factorio with --rcon-port/--rcon-password and hosting the
# save as multiplayer. Empty password = FactorioPlugin disabled
# (fail closed). Set the password in config_local.py.
FACTORIO_RCON_HOST = "127.0.0.1"
FACTORIO_RCON_PORT = 27015
FACTORIO_RCON_PASSWORD = os.environ.get("FACTORIO_RCON_PASSWORD", "")
# Folder Factorio writes script output to. Empty = auto-detect
# %APPDATA%\Factorio\script-output. The mod's event outbox lives at
# <script-output>/hatmas/events.jsonl.
FACTORIO_SCRIPT_OUTPUT = ""
# Announce mod outbox events (pet deaths, boss kills, ...) in chat.
FACTORIO_ANNOUNCE_EVENTS = True
# SEED ONLY: copied into data/factorio_cards.json on first run, then
# never read again — manage live mappings at /factorio/cards on the
# dashboard webserver instead. Card-name matching is case-insensitive
# and whitespace-trimmed. Actions: adopt_pet, grow_pet, pet_say,
# boss_attack. Cooldowns are per-card, in seconds, enforced bot-side.
FACTORIO_CARD_MAP = {
    "Adopt a Pet":  {"action": "adopt_pet",  "cooldown": 0},
    "Grow My Pet":  {"action": "grow_pet",   "cooldown": 0},
    "Pet Speaks":   {"action": "pet_say",    "cooldown": 5},
    "Boss Attack":  {"action": "boss_attack", "cooldown": 120},
}

# === FEATURE TOGGLES ===
# Defaults only. The dashboard's features card flips these live, and
# flips persist across restarts in data/feature_overrides.json (sparse:
# only toggles moved away from these defaults are stored there; flipping
# one back to its default removes it from the file).
DEFAULT_FEATURES = {
    "song_requests": True, "predictions": False, "snap": True,
    "claude_chat": True, "smite_tracking": True, "gamble": True,
    "now_playing_overlay": True, "auto_scene_switch": True,
    "auto_title": True, "god_requests": True, "auto_shoutout": True,
    "tts_highlights": True,
    "kill_detection": True,
    "voicelines": True,
    "economy": True,
    "youtube_rewards": True,
    "web_trading": True,   # dashboard kill-switch; WEB_TRADING_ENABLED still gates
    "streamloots": True,   # gates event dispatch; connection stays up
    "factorio": True,      # gates card handling + chat announcements
    "spacegame": False,    # off = commands silent + hidden from /mod, game page 404s
}

# === DISCORD (plugins/discord_bridge.py) ===
# See Discord_Integration_Plan.md. Overridden in config_local.py; the
# bridge stays inert unless DISCORD_ENABLED is True and a token is set.
DISCORD_ENABLED = False
DISCORD_BOT_TOKEN = ""             # Bot token from the Discord dev portal
DISCORD_GUILD_ID = 0               # Server ID (right-click server, Copy Server ID)
DISCORD_DEFAULT_CHANNEL_ID = 0     # Default channel for send_message()/!discordtest

# Phase 2 go-live announcements. Max ONE per calendar day (persisted
# in data/discord_announce.json), so bot/stream restarts never
# double-announce. Second announcements are manual-only.
DISCORD_ANNOUNCE_ENABLED = False
DISCORD_ANNOUNCE_CHANNEL_ID = 0    # falls back to DISCORD_DEFAULT_CHANNEL_ID
DISCORD_ANNOUNCE_ROLE_ID = 0       # optional @role to ping (0 = no ping)

# === MODERATORS ===
MODERATORS = []


def load_local_config():
    local_config = BASE_DIR / "core" / "config_local.py"
    if local_config.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("config_local", local_config)
        local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local)
        g = globals()
        for key in dir(local):
            if key.isupper():
                g[key] = getattr(local, key)

load_local_config()


def _load_persisted_title_templates():
    """
    Override TITLE_TEMPLATE_GOD / TITLE_TEMPLATE_LOBBY from
    data/title_templates.json if the user has saved templates from the
    dashboard.

    Why this runs at config-import time (rather than in
    SmitePlugin.on_ready, which is what used to do it):

      • The dashboard polls /api/state, which reads
        core.config.TITLE_TEMPLATE_GOD directly.
      • If the dashboard polls between webserver-start and
        smite.on_ready, it sees the config_local.py value, caches it
        (titleTemplatesLoaded flag in control_panel.html), and never
        updates again — so the user's saved templates appear to be
        ignored across restarts.
      • Loading here closes the race: by the time anything else
        imports core.config, the JSON is already authoritative.

    Precedence (least → most authoritative):
        1. defaults in this file
        2. core/config_local.py
        3. data/title_templates.json   ← user-edited via dashboard
    """
    import json as _json
    path = DATA_DIR / "title_templates.json"
    if not path.exists():
        return
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[Config] Could not parse {path.name}: {e}")
        return
    g = globals()
    if isinstance(data, dict):
        if "god" in data and isinstance(data["god"], str):
            g["TITLE_TEMPLATE_GOD"] = data["god"]
        if "lobby" in data and isinstance(data["lobby"], str):
            g["TITLE_TEMPLATE_LOBBY"] = data["lobby"]


_load_persisted_title_templates()
