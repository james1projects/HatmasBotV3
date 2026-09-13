"""
HatmasBot Configuration
=======================
All settings, secrets, and feature toggles live here.
Copy config_local_example.py to config_local.py and fill in your secrets.
"""

import os
import shutil
from pathlib import Path

# Safety net for tools that import config before touching the network:
# points SSL_CERT_FILE at certifi's bundle (see core/tls_trust.py).
from core import tls_trust  # noqa: F401

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
OVERLAY_DIR = BASE_DIR / "overlays"
SOUNDS_DIR = BASE_DIR / "assets" / "sounds"   # served at /assets/sounds/ (alert box samples, CC0 Kenney packs)
CUSTOM_GOD_ICONS_DIR = BASE_DIR / "assets" / "Custom God Icons"
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

# === GOD NAME RESOLVER (core/god_resolver.py) ===
# The last-resort LLM tier asks local Ollama to match garbled chat
# input ("the snake hair lady") to a god. It shares the GPU with
# Smite and OBS, so the timeout is strict and a cold model simply
# means the tier is skipped. Must be a NON-thinking model.
GOD_RESOLVER_LLM_ENABLED = True
GOD_RESOLVER_LLM_MODEL = "qwen3-coder:30b"
GOD_RESOLVER_LLM_TIMEOUT = 2.5  # seconds

# === SONG REQUEST ===
SR_MAX_PER_USER = 2
SR_MAX_PER_SUB = 4
SR_MAX_DURATION_MS = 600000       # 10 minutes
SR_VOTESKIP_THRESHOLD = 5         # Votes needed to skip
SR_PLAYLIST_AUTO_HIDE_SECONDS = 8 # Seconds before playlist song overlay auto-hides
# !vipsr <song>: cut the song queue for Hats (core/wallet.py, reason
# priority_sr). Capped per viewer per rolling hour so a big balance can
# never own the queue; the normal per-user song cap still applies.
# Feature toggle "priority_sr" (also needs song_requests on).
SR_PRIORITY_COST = 200            # Hats per cut
SR_PRIORITY_MAX_PER_HOUR = 2      # cuts per viewer per rolling hour

# === BURN (plugins/burn.py) ===
# !burn <amount>: destroy your own Hats on stream (alert box kind "burn",
# chat flex line, biggest-of-the-stream and all-time records). Pure sink.
# The minimum keeps it a statement rather than spam; there is no cap.
BURN_MIN_HATS = 500
SR_QUEUE_FILE = DATA_DIR / "song_queue.json"
SR_HISTORY_FILE = DATA_DIR / "song_history.json"
SR_LIKES_FILE = DATA_DIR / "song_likes.json"
SR_BLACKLIST_FILE = DATA_DIR / "song_blacklist.json"
SR_STATE_FILE = DATA_DIR / "song_state.json"

# === SNAP ===
SNAP_TIMEOUT_DURATION = 600
SNAP_COOLDOWN = 300
SNAP_STATS_FILE = DATA_DIR / "snap_stats.json"

# === WALLET (core/wallet.py — docs/WALLET_PLAN.md) ===
# Hats and God Tokens live in economy.db now (MixItUp's Developer API
# was the source of truth until 2026-09-13; tools/import_mixitup.py
# copies the balances over once). Passive earning: while live, everyone
# in chat gets WALLET_EARN_HATS_PER_TICK every WALLET_EARN_INTERVAL_MIN
# minutes (James: "5 hats a minute"). The loop also honours the
# "wallet_earn" feature toggle on the dashboard.
WALLET_EARN_ENABLED = True
WALLET_EARN_INTERVAL_MIN = 5           # minutes between ticks
WALLET_EARN_HATS_PER_TICK = 25         # 5 hats/min x 5 min
WALLET_EARN_SUB_MULTIPLIER = 1.0       # subs who chatted since the last tick earn this multiple
WALLET_EARN_OFFLINE = False            # pay ticks while not live
WALLET_EARN_CHAT_BONUS = 0             # extra hats per tick for viewers who chatted since the last tick
WALLET_BONUS_SUB = 0                   # one-off hats on a sub / resub / each gifted sub
WALLET_BONUS_RAID = 0                  # one-off hats to the raider
WALLET_BONUS_BITS_PER_100 = 0          # hats per 100 bits (no cheer hook yet)
WALLET_BONUS_FIRST_MSG = 0             # hats on a viewer's first message of a stream (no hook yet)

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
# Setup (no dashboard Product needed — checkout uses inline price_data):
#   1. Developers → API keys → copy Secret key (sk_test_... for dev,
#      sk_live_... when going live). Drop into config_local.py as
#      STRIPE_SECRET_KEY.
#   2. Developers → Webhooks → Add endpoint pointed at
#      https://hatmaster.tv/api/stripe-webhook listening for
#      "checkout.session.completed", "charge.refunded", and
#      "charge.dispute.created" (all three — refunds/disputes must
#      unqueue). Copy the Signing secret (whsec_...) into
#      config_local.py as STRIPE_WEBHOOK_SECRET.
#   3. For local testing: `stripe listen --forward-to
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

# ── "Log in with YouTube" (Google OAuth) ──
# Same zero-storage model as the Twitch login: the Google token is
# used once server-side to resolve the viewer's YouTube channel id
# (youtube.readonly scope + one channels.list(mine=true) call), then
# discarded. The signed session cookie is the only artifact.
#
# Setup (console.cloud.google.com):
#   1. Create a project (or reuse the one holding YOUTUBE_API_KEY).
#   2. APIs & Services → Enable "YouTube Data API v3".
#   3. OAuth consent screen → External. Add the youtube.readonly
#      scope. Until Google verifies the app, users see an
#      "unverified app" warning and a ~100-user cap — fine for
#      launch, submit for verification when it matters.
#   4. Credentials → Create OAuth client ID → Web application.
#      Authorized redirect URIs: https://hatmaster.tv/auth/google/callback
#      (plus the localhost variant below for dev).
#   5. Drop client id + secret into config_local.py.
# Empty values = the YouTube login button simply doesn't render
# (fail closed, same as the Twitch login).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
WEB_GOOGLE_REDIRECT_URI = "https://hatmaster.tv/auth/google/callback"
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

# === CLOUDFLARE TUNNEL ===
# When enabled, main.py launches `cloudflared tunnel run <name>` as a
# child process tied to the bot's lifetime, so `python main.py` also
# brings hatmaster.tv online (and takes it down on shutdown). See
# core/cloudflared.py.
#
# The documented production setup is the auto-starting cloudflared
# Windows service (HatmasBot.md → "Cloudflare Tunnel"). If that service
# is healthy, set CLOUDFLARED_ENABLED = False in config_local.py to
# avoid running a redundant second tunnel replica.
CLOUDFLARED_ENABLED = True
CLOUDFLARED_TUNNEL_NAME = "HatBot"       # matches `cloudflared tunnel create HatBot`
CLOUDFLARED_PATH = ""                     # "" = auto-detect (PATH, then MSI install dirs)

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

# === ASK THE VOD (vodsearch/ + core/vod_web.py — hatmaster.tv/vod) ===
# Searchable stream archive: GPU transcription of the mic/Discord tracks
# of every sorted recording + the detector's kill/death sidecars, in one
# SQLite FTS index. Indexed offline by tools/vod_index.py (the Stream
# Deck sorter calls it after filing new recordings); served read-only
# by the public site. Clips are rendered on demand (H.264 720p) and
# cached under VOD_CLIPS_DIR so the 80 Mbps HEVC sources never leave
# the PC. Access: the local browser always sees everything (and the
# /vod/review page); tunneled visitors see only recordings marked public,
# and only while the "web_vod" toggle is on. New recordings are private.
VOD_RECORDINGS_DIR = BASE_DIR / "recordings"
VOD_DB_PATH = DATA_DIR / "vod" / "vod_index.db"
VOD_CLIPS_DIR = DATA_DIR / "vod" / "clips"
VOD_WHISPER_MODEL = "large-v3"          # faster-whisper model id (large-v3 ~10x realtime on the 5090)
VOD_WHISPER_PROMPT = None               # None = vodsearch's neutral punctuation prompt; "" = no prompt
VOD_EMBED_HOST = "http://localhost:11434"   # local Ollama; semantic search vectors never leave the PC
VOD_EMBED_MODEL = "nomic-embed-text"    # pulled once (~270 MB); indexing embeds new lines automatically
VOD_SEMANTIC_MIN_SCORE = 0.55           # cosine floor for "matched by meaning" results
VOD_TRACKS = "1:hatmaster,2:friends,3:friends"   # OBS audio tracks to transcribe as index:speaker (identical tracks auto-skipped)
VOD_CLIP_AUDIO_TRACKS = (0, 1, 2, 3)     # tracks mixed into rendered clips (game + voices)
VOD_CLIP_HEIGHT = 720
VOD_CLIP_ENCODER = "h264_nvenc"          # falls back to libx264 automatically
VOD_CLIP_MAX_CONCURRENT = 2              # simultaneous ffmpeg renders on the public server
VOD_CLIP_CACHE_MAX_MB = 4096             # oldest cached clips are evicted past this
VOD_STREAM_MAX_S = 1800                  # "full recording from here" streams stop after 30 min
VOD_STREAM_MAX_CONCURRENT = 2            # simultaneous live transcodes (each holds an NVENC session)
VOD_FFMPEG = "ffmpeg"
VOD_FFPROBE = "ffprobe"

# --- Other channels (tools/vod_channels.py + vodsearch/channels.py) ---
# "Ask the VOD" for any Twitch channel: add a channel by login, its
# archive VODs are downloaded with yt-dlp into VOD_CHANNELS_ROOT/<login>/,
# scanned by the same offline detector (with a per-channel detector
# profile, see core/detector_profile.py), transcribed and indexed into
# the same vod_index.db with recordings.channel = <login>. Those rows are
# LOCAL ONLY: the Store refuses to publish them and tunneled visitors
# never see them, whatever the review page says. Downloading another
# creator's archives is for private analysis; never republish. Helix
# calls use an app-access token (core/twitch_app.py) cached at
# VOD_APP_TOKEN_FILE so a CLI never touches the bot's user tokens.
VOD_CHANNELS_FILE = DATA_DIR / "vod" / "channels.json"   # the channel registry (hand-editable)
VOD_CHANNELS_ROOT = Path(r"D:\Recordings\channels")      # <root>/<login>/ per channel (D: has the space)
VOD_CHANNELS_QUALITY = "best[height<=1080]/best"          # yt-dlp format; 1080p is what the detector is calibrated for
VOD_CHANNELS_FRAGMENTS = 4                                # yt-dlp concurrent HLS fragment downloads
VOD_CHANNELS_MAX_PER_SYNC = 5                             # newest archives fetched per `sync` unless --max
VOD_CHANNELS_KEEP = 20                                    # VODs kept on disk per channel (oldest evicted); 0 = unlimited
VOD_CHANNELS_MIN_FREE_GB = 25                             # refuse to download below this much free space on the root drive
VOD_APP_TOKEN_FILE = DATA_DIR / "twitch_app_token.json"   # cached client-credentials token (not a user token)

# === CO-CASTER (plugins/cocaster/) ===
# Stage 1 of the AI co-caster. Two channels, both gated by the "cocaster"
# feature toggle (default OFF):
#   EAR    private earpiece: every COCASTER_EAR_INTERVAL_S, one spoken
#          sentence summarizing new chat, played on COCASTER_EAR_DEVICE
#          (a substring of the output device name; "Headphones" = the
#          Elgato XLR Dock headphone jack, which is NOT in the stream mix).
#   LINES  persona one-liners on multikills/deaths, written to
#          data/cocaster/lines.jsonl + emitted as the "cocaster_line"
#          overlay event. Spoken on COCASTER_STREAM_DEVICE only when
#          COCASTER_STREAM_VOICE is True (default: text only).
# Independent of the toggle, every chat message is logged to CHAT_LOG_DB
# (local telemetry; never leaves data/).
# Privacy: the LLM sees chat text + match state only. "claude" = Anthropic
# API (default); "ollama" = fully local, shares the GPU with Smite.
COCASTER_LLM_BACKEND = "claude"
COCASTER_MODEL = "claude-opus-5"
COCASTER_EFFORT = "low"                   # fast, short summaries
COCASTER_OLLAMA_HOST = "http://localhost:11434"
COCASTER_OLLAMA_MODEL = "qwen3.6:27b"
COCASTER_EAR_DEVICE = "Headphones"        # output device substring for the private channel
COCASTER_EAR_VOICE = "Microsoft Zira Desktop"   # any installed SAPI voice; "" = default
COCASTER_EAR_RATE = 1                     # SAPI rate -10..10
COCASTER_EAR_INTERVAL_S = 75
COCASTER_EAR_MIN_MSGS = 3                 # fewer new messages than this = stay quiet
COCASTER_EAR_MAX_WORDS = 35
COCASTER_STREAM_VOICE = False             # speak caster lines on stream (text-only until tuned)
COCASTER_STREAM_DEVICE = "SFX"            # Wave Link virtual input that IS in the stream mix
COCASTER_STREAM_VOICE_NAME = "Microsoft David Desktop"
COCASTER_STREAM_RATE = 0
COCASTER_LINE_COOLDOWN_S = 45
COCASTER_LINE_MAX_WORDS = 22
COCASTER_PERSONA_FILE = BASE_DIR / "plugins" / "cocaster" / "persona.md"
COCASTER_DIR = DATA_DIR / "cocaster"
CHAT_LOG_DB = DATA_DIR / "chat_log.db"

# === STREAM BINGO (plugins/bingo/ — hatmaster.tv/bingo) ===
# One round per stream (BINGO START on the deck / dashboard). Viewers get
# a free 5x5 card with a Twitch login and can buy up to BINGO_MAX_CARDS
# at BINGO_CARD_PRICES (Hats via the economy plugin). Squares are marked
# by the kill detector / economy (auto) or by James (manual: deck keys,
# the dashboard /bingo page, !bingocall). First line wins the pot =
# BINGO_BASE_PRIZE + BINGO_POT_SHARE of the Hats spent on extra cards.
# Edit the square pool in data/bingo/pool.json (created with defaults).
BINGO_BASE_PRIZE = 500
BINGO_CARD_PRICES = (50, 100, 200)      # 2nd, 3rd, 4th card; the first is free
BINGO_MAX_CARDS = 4
BINGO_POT_SHARE = 0.5
BINGO_POOL_FILE = DATA_DIR / "bingo" / "pool.json"
BINGO_DB = DATA_DIR / "bingo.db"

# === HATMASTER ALERT BOX (core/alert_box.py — docs/ALERT_BOX.md) ===
# One full-canvas OBS source (localhost:8069/overlay/alerts?box=main) that
# plays gamble / TTS / voicelines / economy / spin / bingo alerts as kinds,
# each with its own place on the 1920x1080 scene, lane (queue), duration,
# sound and volume. Edited on the dashboard at /alerts/layout; the file is
# created with defaults (every migrated kind off) on first run.
ALERTS_FILE = DATA_DIR / "alerts.json"

# === TIMED MESSAGES (plugins/timed_messages.py, managed on hatmaster.tv/mod) ===
# Rotating chat messages in lanes, the last MixItUp feature rebuilt. The
# file is created with one empty "general" lane on first use; everything
# else (lanes, intervals, live-only, the global gap) is edited on /mod.
TIMED_MESSAGES_FILE = DATA_DIR / "timed_messages.json"
TIMED_MESSAGES_DEFAULT_INTERVAL_MIN = 10   # a new lane's interval
TIMED_MESSAGES_DEFAULT_GAP_SEC = 60        # first-run global gap between any two posts
TIMED_MESSAGES_TICK_SEC = 1.0              # how often the loop checks the lanes

# === FEATURE TOGGLES ===
# Defaults only. The dashboard's features card flips these live, and
# flips persist across restarts in data/feature_overrides.json (sparse:
# only toggles moved away from these defaults are stored there; flipping
# one back to its default removes it from the file).
DEFAULT_FEATURES = {
    "song_requests": True, "predictions": False, "snap": True,
    "priority_sr": True,   # !vipsr (Hats to cut the song queue); needs song_requests too
    "burn": True,          # !burn (destroy Hats for the flex; alert box kind "burn")
    "claude_chat": True, "smite_tracking": True, "gamble": True,
    "now_playing_overlay": True, "auto_scene_switch": True,
    "auto_title": True, "god_requests": True, "auto_shoutout": True,
    "tts_highlights": True,
    "kill_detection": True,
    "voicelines": True,
    "economy": True,
    "youtube_rewards": True,
    "web_trading": True,   # dashboard kill-switch; WEB_TRADING_ENABLED still gates
    "web_profile": True,   # off = hatmaster.tv/me 404s (invisibility contract)
    "web_live": True,      # off = hatmaster.tv/live 404s + /ws/live refuses
    "web_vod": False,      # visitors get /vod only when ON, and then only recordings marked public on /vod/review; localhost always works
    "streamloots": True,   # gates event dispatch; connection stays up
    "factorio": True,      # gates card handling + chat announcements
    "spacegame": False,    # off = commands silent + hidden from /mod, game page 404s
    "findit": False,       # off = hatmaster.tv/FindIt 404s + no GPU worker runs
    "cocaster": False,     # off = no earpiece summaries, no caster lines (chat log still records)
    "bingo": True,         # off = hatmaster.tv/bingo 404s and no squares get marked
    "wallet_earn": True,   # off = no passive Hats while live (balances, trading, gamble keep working)
    "timed_messages": True,  # off = the /mod rotation posts nothing (lanes keep ticking)
}

# === FINDIT (plugins/findit/ — hatmaster.tv/FindIt, early development) ===
# "Ctrl+F for real life": phone camera + open-vocabulary object detection
# on the GPU. The heavy deps (torch/ultralytics) live in a dedicated venv
# (.venv-findit), NOT the bot's environment — the bot only launches
# plugins/findit/worker.py as a child process when the "findit" feature
# toggle is on AND someone opens the page. Idle or toggled off = no
# process, no VRAM.
FINDIT_PYTHON = str(BASE_DIR / ".venv-findit" / "Scripts" / "python.exe")
FINDIT_WORKER_PORT = 8474            # localhost-only detection worker
FINDIT_MODEL = "yolov8l-worldv2.pt"  # resolved in data/findit/ (worker cwd)
FINDIT_EMBED_MODEL = "dinov2"        # custom-item embedder: "dinov2" (instance-
                                     # level, 2026-07-03 benchmark: hard-AUC 1.0
                                     # vs CLIP 0.995, 3x wider pos/neg gap) or
                                     # "clip" (legacy, ultralytics' ViT-B/32)
FINDIT_SIM_THRESHOLD = 0.55          # cosine sim to relabel a custom item.
                                     # Swept 2026-07-03 (tools/findit_bench.py):
                                     # 0.55 = best recall/FP balance for dinov2;
                                     # use 0.80 for clip
FINDIT_MAX_SESSIONS = 3              # concurrent phone connections allowed
FINDIT_IDLE_TIMEOUT = 900            # secs with no phones before worker stops
FINDIT_STARTUP_TIMEOUT = 240         # secs to wait for model warmup (first-ever
                                     # run also installs CLIP + downloads weights)

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
