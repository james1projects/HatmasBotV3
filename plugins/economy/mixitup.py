"""
plugins/economy/mixitup.py
==========================
MixItUp Developer API client for the Hats currency.

MixItUp runs a local Developer API on http://localhost:8911 that lets
us read/write per-user currency balances and inventory items. We use
it as the source of truth for Hats — the game economy plugin issues
ADD/SUBTRACT operations against viewer balances on every !buy / !sell
and dividend payout.

This mixin owns the GET/PATCH primitives plus the higher-level
"resolve currency id by name", "get user id by Twitch username", and
"get/adjust balance" wrappers. Everything else in the plugin uses
these instead of touching MIXITUP_API_BASE directly.

Notes:
  * MixItUp's API uses User-IDs, not usernames. Every balance op needs
    a `_get_user_id` lookup first.
  * Network errors (MixItUp closed, port blocked) silently degrade to
    "no balance" rather than crashing the trade — the !buy/!sell
    handlers check the result and surface a friendly error to chat.
"""

from __future__ import annotations

from typing import Optional

import aiohttp

from core.config import MIXITUP_API_BASE, ECONOMY_CURRENCY_NAME


class _MixItUpMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self.session        aiohttp.ClientSession (created in on_ready)
      self._currency_id   resolved on _resolve_currency_id success
      self._connected     True iff currency was found in MixItUp

    All set up by EconomyPlugin.__init__ (or .on_ready for the session).
    """

    async def _miu_get(self, path):
        try:
            async with self.session.get(f"{MIXITUP_API_BASE}{path}") as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except aiohttp.ClientConnectorError:
            return None
        except Exception as e:
            print(f"[Economy] MixItUp GET error: {e}")
            return None

    async def _miu_patch(self, path, data):
        try:
            async with self.session.patch(
                f"{MIXITUP_API_BASE}{path}",
                json=data,
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            print(f"[Economy] MixItUp PATCH error: {e}")
            return None

    async def _resolve_currency_id(self):
        currencies = await self._miu_get("/currency")
        if not currencies:
            print(f"[Economy] Could not connect to MixItUp API")
            return
        for curr in currencies:
            if curr.get("Name", "").lower() == ECONOMY_CURRENCY_NAME.lower():
                self._currency_id = curr["ID"]
                self._connected = True
                print(f"[Economy] MixItUp connected — currency: {self._currency_id}")
                return
        print(f"[Economy] Currency '{ECONOMY_CURRENCY_NAME}' not found!")

    async def _get_user_id(self, twitch_username: str) -> Optional[str]:
        data = await self._miu_get(f"/users/Twitch/{twitch_username}")
        if data and "User" in data:
            return data["User"]["ID"]
        return None

    async def _get_balance(self, twitch_username: str) -> Optional[int]:
        if not self._connected:
            return None
        user_id = await self._get_user_id(twitch_username)
        if not user_id:
            return None
        data = await self._miu_get(f"/currency/{self._currency_id}/{user_id}")
        if data:
            return data.get("Amount", 0)
        return 0

    async def _adjust_balance(self, twitch_username: str, amount: int) -> bool:
        """Add or subtract hats. Positive = add, negative = subtract."""
        if not self._connected:
            return False
        user_id = await self._get_user_id(twitch_username)
        if not user_id:
            return False
        result = await self._miu_patch(
            f"/currency/{self._currency_id}/{user_id}",
            {"Amount": amount}
        )
        return result is not None
