"""
HatmasBot Local Configuration
===============================
Copy this file to config_local.py and fill in your actual values.
This file is gitignored — your secrets stay local.

Only include the values you want to override.
Everything else uses defaults from config.py.
"""

# === TWITCH IDENTITY ===
TWITCH_BOT_USERNAME = "YourBotName"
TWITCH_CHANNEL = "your_channel"
TWITCH_BOT_ID = "your_bot_user_id"
TWITCH_OWNER_ID = "your_twitch_user_id"

# === TWITCH AUTH ===
TWITCH_CLIENT_ID = "your_twitch_client_id_here"
TWITCH_CLIENT_SECRET = "your_twitch_client_secret_here"
TWITCH_BOT_TOKEN = "oauth:your_bot_token_here"
TWITCH_BOT_REFRESH_TOKEN = "your_refresh_token_here"

# === SPOTIFY ===
SPOTIFY_CLIENT_ID = "your_spotify_client_id_here"
SPOTIFY_CLIENT_SECRET = "your_spotify_client_secret_here"

# === SMITE 2 (Tracker.gg) ===
SMITE2_PLATFORM = "steam"              # "steam", "epic", "xbox", "psn"
SMITE2_PLATFORM_ID = "your_platform_id_here"  # e.g. Steam64 ID

# === OBS ===
OBS_WS_PASSWORD = "your_obs_websocket_password"
OBS_SOURCE_GOD_IMAGE = "GodImage"        # Name of your OBS Image source for god portrait
OBS_SOURCE_GOD_BG = "GodBackground"      # Name of your OBS Image source for portrait background
OBS_GOD_IMAGE_SCENE = "Main Scene"       # Scene containing the god portrait
OBS_GOD_IMAGE_GROUP = "God Portrait Group"  # Group name if sources are inside a group

# === SMITE 2 GOD IMAGES ===
# Folder with funny god images — filenames must match god names (e.g., Atlas.png, Janus.png)
SMITE2_GOD_IMAGES_DIR = r"C:\path\to\your\god\images"
# Folder with role/team backgrounds (bg_chaos_red.png, bg_carry_gold.png, etc.)
SMITE2_GOD_BG_DIR = r"C:\path\to\your\god\images\Backgrounds"

# === CLAUDE ===
CLAUDE_API_KEY = "sk-ant-your_key_here"
CLAUDE_SYSTEM_PROMPT = """You are YourBotName, a Twitch chat bot.
Keep responses SHORT (under 100 words), witty, and dry humor.
Never be mean, but playful roasting is fine. One emoji max per message."""

# === OBS SCENE NAMES (match your OBS setup) ===
# OBS_SCENE_MAIN = "Main"
# OBS_SCENE_LOBBY = "Lobby"
# OBS_SCENE_INGAME = "In Game"
# OBS_SCENE_SNAP = "Snap"

# === MODERATORS (add your mod usernames) ===
MODERATORS = [
    "your_channel",
    # "mod_username_1",
    # "mod_username_2",
]
