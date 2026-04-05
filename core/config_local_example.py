"""
HatmasBot Local Configuration
===============================
Copy this file to config_local.py and fill in your actual values.
This file is gitignored — your secrets stay local.

Only include the values you want to override.
Everything else uses defaults from config.py.
"""

# === TWITCH ===
TWITCH_CLIENT_ID = "your_twitch_client_id_here"
TWITCH_CLIENT_SECRET = "your_twitch_client_secret_here"
TWITCH_BOT_TOKEN = "oauth:your_bot_token_here"

# === SPOTIFY ===
SPOTIFY_CLIENT_ID = "your_spotify_client_id_here"
SPOTIFY_CLIENT_SECRET = "your_spotify_client_secret_here"

# === OBS ===
OBS_WS_PASSWORD = "your_obs_websocket_password"

# === CLAUDE ===
CLAUDE_API_KEY = "sk-ant-your_key_here"

# === OBS SCENE NAMES (match your OBS setup) ===
# OBS_SCENE_MAIN = "Main"
# OBS_SCENE_LOBBY = "Lobby"
# OBS_SCENE_INGAME = "In Game"
# OBS_SCENE_SNAP = "Snap"

# === MODERATORS (add your mod usernames) ===
MODERATORS = [
    "hatmaster",
    # "mod_username_1",
    # "mod_username_2",
]
