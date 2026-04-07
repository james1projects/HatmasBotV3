"""
Twitch Token Manager
=====================
Async token manager that auto-refreshes OAuth tokens when they expire
or receive 401 responses. Persists refreshed tokens to disk and updates
the in-memory config values so all consumers stay current.

Usage:
    token_mgr = TokenManager()
    await token_mgr.start()

    # Get current valid headers for API calls
    headers = await token_mgr.get_bot_headers()
    headers = await token_mgr.get_broadcaster_headers()

    # On shutdown
    await token_mgr.close()
"""

import asyncio
import time
import json
import aiohttp
from pathlib import Path

import core.config as config

TOKEN_DIR = Path(config.BASE_DIR) / "data"
BOT_TOKEN_FILE = TOKEN_DIR / "twitch_token.json"
BROADCASTER_TOKEN_FILE = TOKEN_DIR / "twitch_broadcaster_token.json"

# How often to proactively validate tokens (seconds)
VALIDATION_INTERVAL = 3000  # ~50 minutes

# Minimum time between refresh attempts to avoid hammering on repeated 401s
MIN_REFRESH_INTERVAL = 30


class TokenManager:
    """Manages Twitch OAuth tokens with automatic refresh on 401 or expiry."""

    def __init__(self):
        self._session = None
        self._validation_task = None

        # Bot token state
        self._bot_token = config.TWITCH_BOT_TOKEN
        self._bot_refresh = config.TWITCH_BOT_REFRESH_TOKEN
        self._bot_last_refresh = 0
        self._bot_lock = asyncio.Lock()

        # Broadcaster token state
        self._broadcaster_token = config.TWITCH_BROADCASTER_TOKEN
        self._broadcaster_refresh = config.TWITCH_BROADCASTER_REFRESH_TOKEN
        self._broadcaster_last_refresh = 0
        self._broadcaster_lock = asyncio.Lock()

    async def start(self):
        """Start the token manager — creates HTTP session and begins validation loop."""
        self._session = aiohttp.ClientSession()

        # Try loading persisted tokens (may be newer than config)
        self._load_persisted_tokens()

        # Validate both tokens on startup
        await self._validate_and_refresh_bot()
        await self._validate_and_refresh_broadcaster()

        # Start periodic validation
        self._validation_task = asyncio.create_task(self._validation_loop())
        print("[TokenManager] Started — tokens validated")

    async def close(self):
        """Shutdown the token manager."""
        if self._validation_task:
            self._validation_task.cancel()
        if self._session:
            await self._session.close()

    def _load_persisted_tokens(self):
        """Load tokens from disk if they exist (may be fresher than config_local.py)."""
        try:
            if BOT_TOKEN_FILE.exists():
                with open(BOT_TOKEN_FILE) as f:
                    data = json.load(f)
                if "access_token" in data and "refresh_token" in data:
                    self._bot_token = data["access_token"]
                    self._bot_refresh = data["refresh_token"]
                    print("[TokenManager] Loaded persisted bot token")
        except Exception as e:
            print(f"[TokenManager] Could not load bot token file: {e}")

        try:
            if BROADCASTER_TOKEN_FILE.exists():
                with open(BROADCASTER_TOKEN_FILE) as f:
                    data = json.load(f)
                if "access_token" in data and "refresh_token" in data:
                    self._broadcaster_token = data["access_token"]
                    self._broadcaster_refresh = data["refresh_token"]
                    print("[TokenManager] Loaded persisted broadcaster token")
        except Exception as e:
            print(f"[TokenManager] Could not load broadcaster token file: {e}")

    def _persist_token(self, token_file, access_token, refresh_token):
        """Save a refreshed token to disk."""
        try:
            token_file.parent.mkdir(exist_ok=True)
            with open(token_file, "w") as f:
                json.dump({
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "refreshed_at": time.time(),
                }, f, indent=2)
        except Exception as e:
            print(f"[TokenManager] Failed to persist token to {token_file}: {e}")

    async def _refresh_token(self, refresh_tok):
        """Exchange a refresh token for a new access + refresh token pair."""
        async with self._session.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": config.TWITCH_CLIENT_ID,
                "client_secret": config.TWITCH_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_tok,
            }
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                body = await resp.text()
                raise Exception(f"Refresh failed ({resp.status}): {body}")

    async def _validate_token(self, access_token):
        """Validate a token against Twitch. Returns True if valid."""
        try:
            async with self._session.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {access_token}"}
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    # --- Bot token ---

    async def _validate_and_refresh_bot(self):
        """Check bot token validity, refresh if needed."""
        async with self._bot_lock:
            if await self._validate_token(self._bot_token):
                return True
            return await self._do_refresh_bot()

    async def _do_refresh_bot(self):
        """Actually refresh the bot token. Must be called under _bot_lock."""
        now = time.time()
        if now - self._bot_last_refresh < MIN_REFRESH_INTERVAL:
            print("[TokenManager] Bot token refresh throttled")
            return False

        self._bot_last_refresh = now
        try:
            print("[TokenManager] Refreshing bot token...")
            data = await self._refresh_token(self._bot_refresh)
            self._bot_token = data["access_token"]
            self._bot_refresh = data["refresh_token"]

            # Update the global config so anything reading it directly stays current
            config.TWITCH_BOT_TOKEN = self._bot_token
            config.TWITCH_BOT_REFRESH_TOKEN = self._bot_refresh

            # Persist to disk
            self._persist_token(BOT_TOKEN_FILE, self._bot_token, self._bot_refresh)

            print("[TokenManager] Bot token refreshed successfully")
            return True
        except Exception as e:
            print(f"[TokenManager] Bot token refresh FAILED: {e}")
            return False

    async def get_bot_headers(self):
        """Get Twitch API headers using the bot token. Auto-refreshes if invalid."""
        return {
            "Client-ID": config.TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {self._bot_token}",
            "Content-Type": "application/json",
        }

    # --- Broadcaster token ---

    async def _validate_and_refresh_broadcaster(self):
        """Check broadcaster token validity, refresh if needed."""
        async with self._broadcaster_lock:
            if await self._validate_token(self._broadcaster_token):
                return True
            return await self._do_refresh_broadcaster()

    async def _do_refresh_broadcaster(self):
        """Actually refresh the broadcaster token. Must be called under _broadcaster_lock."""
        now = time.time()
        if now - self._broadcaster_last_refresh < MIN_REFRESH_INTERVAL:
            print("[TokenManager] Broadcaster token refresh throttled")
            return False

        self._broadcaster_last_refresh = now
        try:
            print("[TokenManager] Refreshing broadcaster token...")
            data = await self._refresh_token(self._broadcaster_refresh)
            self._broadcaster_token = data["access_token"]
            self._broadcaster_refresh = data["refresh_token"]

            # Update the global config
            config.TWITCH_BROADCASTER_TOKEN = self._broadcaster_token
            config.TWITCH_BROADCASTER_REFRESH_TOKEN = self._broadcaster_refresh

            # Persist to disk
            self._persist_token(BROADCASTER_TOKEN_FILE, self._broadcaster_token, self._broadcaster_refresh)

            print("[TokenManager] Broadcaster token refreshed successfully")
            return True
        except Exception as e:
            print(f"[TokenManager] Broadcaster token refresh FAILED: {e}")
            return False

    async def get_broadcaster_headers(self):
        """Get Twitch API headers using the broadcaster token. Auto-refreshes if invalid."""
        return {
            "Client-ID": config.TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {self._broadcaster_token}",
            "Content-Type": "application/json",
        }

    # --- 401 handler ---

    async def handle_401(self, token_type="broadcaster"):
        """Called when an API call returns 401. Triggers an immediate refresh.
        Returns True if refresh succeeded (caller should retry the request)."""
        if token_type == "bot":
            async with self._bot_lock:
                return await self._do_refresh_bot()
        else:
            async with self._broadcaster_lock:
                return await self._do_refresh_broadcaster()

    # --- Periodic validation ---

    async def _validation_loop(self):
        """Periodically validate tokens and refresh before they expire."""
        while True:
            try:
                await asyncio.sleep(VALIDATION_INTERVAL)
                print("[TokenManager] Periodic token validation...")
                await self._validate_and_refresh_bot()
                await self._validate_and_refresh_broadcaster()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TokenManager] Validation loop error: {e}")
                await asyncio.sleep(60)
