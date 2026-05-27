"""
plugins/smite/twitch_api.py
===========================
Tiny header-builder helpers for Twitch Helix API calls. Kept separate
because they're shared by the title path and the predictions path —
both want token-manager-aware headers with auto-refresh on 401.

Both methods consult `self._token_manager` first (the auto-refresh
TokenManager from `core/token_manager.py`); fall back to the static
config values only when token_manager is None (e.g. running outside
the bot for debugging).
"""

from __future__ import annotations

from core.config import (
    TWITCH_CLIENT_ID, TWITCH_BOT_TOKEN, TWITCH_BROADCASTER_TOKEN,
)


class _TwitchAPIMixin:
    """
    Mixed into SmitePlugin. Reads:
      self._token_manager   (set in __init__ from the constructor arg)
    """

    async def _twitch_headers(self):
        """Headers for Twitch Helix API calls using the bot token."""
        if self._token_manager:
            return await self._token_manager.get_bot_headers()
        return {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {TWITCH_BOT_TOKEN}",
            "Content-Type": "application/json",
        }

    async def _broadcaster_headers(self):
        """Headers for Twitch Helix API calls that require broadcaster auth.
        Used for channel title updates, predictions, etc.
        Falls back to bot token if no broadcaster token is configured."""
        if self._token_manager:
            return await self._token_manager.get_broadcaster_headers()
        token = TWITCH_BROADCASTER_TOKEN or TWITCH_BOT_TOKEN
        return {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
