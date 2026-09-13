"""
plugins/economy/mixitup.py
==========================
MixItUp Developer API client for the Hats currency.

MixItUp runs a local Developer API on http://localhost:8911 that lets
us read/write per-user currency balances and inventory items. We use
it as the source of truth for Hats — the game economy plugin issues
ADD/SUBTRACT operations against viewer balances on every !buy / !sell
and dividend payout.

Callers pass a user_uuid (core/users.py). MixItUp only knows Twitch
usernames, so this mixin resolves the uuid to the user's Twitch login
first; a YouTube-only viewer has no login and therefore no balance
until the local wallet (docs/WALLET_PLAN.md) replaces MixItUp.

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
from core import users as _users


class _MixItUpMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self.session        aiohttp.ClientSession (created in on_ready)
      self._currency_id   resolved on _resolve_currency_id success
      self._connected     True iff currency was found in MixItUp
      self._db            shared connection, for uuid -> login lookups

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

    async def _twitch_login_for(self, user_uuid: str) -> Optional[str]:
        """uuid -> Twitch login (lowercase) or None. Cached per uuid;
        a merge clears core.users' cache, not this one, but logins
        only ever gain a Twitch identity so a stale None is refreshed
        by the next miss."""
        cache = getattr(self, "_login_by_uuid", None)
        if cache is None:
            cache = self._login_by_uuid = {}
        login = cache.get(user_uuid)
        if login:
            return login
        db = getattr(self, "_db", None)
        if db is None or not user_uuid:
            return None
        try:
            login = await _users.twitch_login_of(db, user_uuid)
        except Exception as e:
            print(f"[Economy] login lookup failed for {user_uuid}: {e}")
            return None
        if login:
            cache[user_uuid] = login
        return login

    async def _get_user_id(self, twitch_username: str) -> Optional[str]:
        data = await self._miu_get(f"/users/Twitch/{twitch_username}")
        if data and "User" in data:
            return data["User"]["ID"]
        return None

    async def _get_balance(self, user_uuid: str) -> Optional[int]:
        if not self._connected:
            return None
        login = await self._twitch_login_for(user_uuid)
        if not login:
            return None
        miu_id = await self._get_user_id(login)
        if not miu_id:
            return None
        data = await self._miu_get(f"/currency/{self._currency_id}/{miu_id}")
        if data:
            return data.get("Amount", 0)
        return 0

    async def _adjust_balance(self, user_uuid: str, amount: int) -> bool:
        """Add or subtract hats. Positive = add, negative = subtract."""
        if not self._connected:
            return False
        login = await self._twitch_login_for(user_uuid)
        if not login:
            return False
        miu_id = await self._get_user_id(login)
        if not miu_id:
            return False
        result = await self._miu_patch(
            f"/currency/{self._currency_id}/{miu_id}",
            {"Amount": amount}
        )
        return result is not None
