"""
HatmasBot Configuration
=======================
All settings, secrets, and feature toggles live here.
Copy config_local_example.py to config_local.py and fill in your secrets.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
OVERLAY_DIR = BASE_DIR / "overlays"
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
TWITCH_BROADCASTER_TOKEN = os.environ.get("TWITCH_BROADCASTER_TOKEN", "5tdz6gvhatfa7f80txqz572imxe6m8")
TWITCH_BROADCASTER_REFRESH_TOKEN = os.environ.get("TWITCH_BROADCASTER_REFRESH_TOKEN", "iik6w9c3k0j22lmgffmy725bwbfhgzg579ti0y8isr4stbfc5k")
TWITCH_BROADCASTER_SCOPES = [
    "channel:manage:broadcast", "channel:manage:predictions",
    "channel:read:subscriptions", "moderator:manage:shoutouts",
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
SMITE2_STATE_FILE = DATA_DIR / "smite_state.json"
SMITE2_GOD_IMAGES_DIR = ""       # Path to folder with god images (e.g., "C:/OBS/gods")


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
SMITE2_GOD_BG_DIR = ""            # Path to god portrait backgrounds folder

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

# === GAMBLE ===
GAMBLE_CURRENCY_NAME = "Hats"                # Must match the currency name in MixItUp
GAMBLE_MIN_BET = 10                          # Minimum wager
GAMBLE_COOLDOWN = 10                         # Seconds between gambles per user
GAMBLE_JACKPOT_FILE = DATA_DIR / "gamble_jackpot.json"
GAMBLE_ALERT_MIN_WAGER = 1000               # Min wager to trigger sound + visual alerts (jackpot always triggers)

# === KILL/DEATH DETECTION ===
KILL_DETECT_ENABLED = True               # Enable/disable kill/death detection
KILL_DETECT_OBS_SOURCE = "Smite 2"       # OBS source name to screenshot
KILL_DETECT_OBS_SCENE = "-Main Game Capture"  # OBS scene containing the source
KILL_DETECT_INTERVAL = 1.5               # Seconds between screenshot analysis
KILL_DETECT_KILL_COOLDOWN = 4.0          # Seconds between kill detections
KILL_DETECT_DEATH_COOLDOWN = 8.0         # Seconds between death detections
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"  # Path to Tesseract binary

# === TTS (Text-to-Speech for Highlighted Messages) ===
TTS_ENABLED = True                   # Enable/disable TTS for highlighted messages
TTS_MAX_LENGTH = 300                 # Max characters to read (truncates longer messages)
TTS_RATE = 1.0                       # Speech rate (0.5 = slow, 1.0 = normal, 2.0 = fast)
TTS_VOLUME = 0.8                     # Volume (0.0 to 1.0)
TTS_DISPLAY_SECONDS = 0             # 0 = auto (stays visible until speech ends)

# === WEB SERVER ===
WEB_HOST = "localhost"
WEB_PORT = 8069

# === FEATURE TOGGLES ===
DEFAULT_FEATURES = {
    "song_requests": True, "predictions": True, "snap": True,
    "claude_chat": True, "smite_tracking": True, "gamble": True,
    "now_playing_overlay": True, "auto_scene_switch": True,
    "auto_title": True, "god_requests": True, "auto_shoutout": True,
    "tts_highlights": True,
    "kill_detection": True,
}

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