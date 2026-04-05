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
TWITCH_BOT_USERNAME = "HatmasBot"
TWITCH_CHANNEL = "hatmaster"
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "YOUR_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
TWITCH_BOT_TOKEN = os.environ.get("TWITCH_BOT_TOKEN", "YOUR_BOT_OAUTH_TOKEN")
TWITCH_BOT_REFRESH_TOKEN = os.environ.get("TWITCH_BOT_REFRESH_TOKEN", "YOUR_REFRESH_TOKEN")
TWITCH_BOT_ID = "234224228"
TWITCH_OWNER_ID = "33955087"
TWITCH_SCOPES = [
    "chat:read", "chat:edit", "whispers:read", "whispers:edit",
    "channel:manage:predictions", "moderator:manage:banned_users",
    "channel:read:subscriptions", "user:read:chat", "user:write:chat",
]

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
SMITE2_PLATFORM_ID = "76561198035860161"
SMITE2_TRACKER_BASE = "https://api.tracker.gg/api/v2/smite2/standard/profile"
SMITE2_POLL_INTERVAL = 30
SMITE2_CACHE_TTL = 60

# === OBS WEBSOCKET ===
OBS_WS_HOST = "localhost"
OBS_WS_PORT = 4455
OBS_WS_PASSWORD = os.environ.get("OBS_WS_PASSWORD", "YOUR_OBS_PASSWORD")
OBS_SCENE_MAIN = "Main"
OBS_SCENE_LOBBY = "Lobby"
OBS_SCENE_INGAME = "In Game"
OBS_SCENE_SNAP = "Snap"
OBS_SOURCE_NOW_PLAYING = "NowPlaying"
OBS_SOURCE_SNAP = "SnapOverlay"

# === CLAUDE API ===
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_CLAUDE_API_KEY")
CLAUDE_MODEL = "claude-opus-4-6"
CLAUDE_MAX_TOKENS = 150
CLAUDE_COOLDOWN_USER = 30
CLAUDE_COOLDOWN_GLOBAL = 10
CLAUDE_SYSTEM_PROMPT = """You are HatmasBot, the Twitch chat bot for Hatmaster's stream. 
Hatmaster is a Smite content creator known for playing guardians with damage builds. 
His main gods are Sylvanus and Ymir. He has 35K subscribers. 
Keep responses SHORT (under 100 words), witty, and dry humor.
Never be mean, but playful roasting is fine. One emoji max per message."""

# === SONG REQUEST ===
SR_MAX_PER_USER = 2
SR_MAX_PER_SUB = 4
SR_QUEUE_FILE = DATA_DIR / "song_queue.json"
SR_HISTORY_FILE = DATA_DIR / "song_history.json"
SR_LIKES_FILE = DATA_DIR / "song_likes.json"

# === SNAP ===
SNAP_TIMEOUT_DURATION = 600
SNAP_COOLDOWN = 300
SNAP_STATS_FILE = DATA_DIR / "snap_stats.json"

# === WEB SERVER ===
WEB_HOST = "localhost"
WEB_PORT = 8069

# === FEATURE TOGGLES ===
DEFAULT_FEATURES = {
    "song_requests": True, "predictions": True, "snap": True,
    "claude_chat": True, "smite_tracking": True,
    "now_playing_overlay": True, "auto_scene_switch": True,
}

# === MODERATORS ===
MODERATORS = ["hatmaster"]


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
