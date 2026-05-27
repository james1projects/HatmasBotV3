"""
plugins/economy/helpers.py
==========================
Cross-cutting helpers used by multiple mixins:

  * `_build_excluded_set()` / `EXCLUDED_USERS_LOWER` - the bot-account
    exclusion list, computed once at module import. Read by the
    dividend, free-share, and !buy/!sell paths.

  * `_HelpersMixin` - instance methods that pull viewer data from
    Twitch (Helix API + TwitchIO fallback) and translate it into
    economy actions: `_get_profile_image`, `_get_chatters`,
    `is_excluded_user`, `_distribute_free_shares`. Also
    `_is_broadcaster_live()` - the gate for celebration side-effects.

These don't fit neatly into trading/match/dividends because every one
of those concerns relies on them. Keeping them in their own file means
the mixin order in `plugin.py` puts helpers near the bottom (everything
else can call into them, but they don't reach back).
"""

from __future__ import annotations

import aiohttp
from typing import Dict, List, Optional

from core.config import (
    TWITCH_CHANNEL, TWITCH_OWNER_ID, TWITCH_BOT_USERNAME,
    ECONOMY_STARTING_PRICE, ECONOMY_FREE_SHARE_COUNT,
    ECONOMY_EXCLUDED_USERNAMES,
)


def _build_excluded_set():
    """Build a lowercased set of usernames to exclude from the economy.
    Includes the configured list plus the bot's own username so it
    can never accidentally accumulate its own shares."""
    out = {u.lower() for u in (ECONOMY_EXCLUDED_USERNAMES or []) if u}
    if TWITCH_BOT_USERNAME and TWITCH_BOT_USERNAME != "YOUR_BOT_USERNAME":
        out.add(TWITCH_BOT_USERNAME.lower())
    return out


# Computed once at module import. Re-imported by dividends.py and
# referenced from is_excluded_user() below - same set for every caller.
EXCLUDED_USERS_LOWER = _build_excluded_set()


class _HelpersMixin:
    """
    Twitch / chatter helpers used by free-share distribution, the
    portfolio overlay's profile image, and dividend payouts.

    Reads `self.token_manager`, `self.bot`, `self._db`, `self._prices`,
    plus calls into `_TradingMixin._add_shares`. All of those are set
    up by EconomyPlugin.__init__ + later mixins.
    """

    def _is_broadcaster_live(self) -> bool:
        """
        Is the broadcaster currently live on Twitch?

        The economy uses this to gate "celebration" side-effects
        (dividends, free shares, the match-end overlay popup, the
        leaderboard refresh) so they only fire when there's actually
        an audience watching.

        Reads from the StreamStatusPlugin, which polls the Helix
        `/streams` endpoint every 60 seconds. If the plugin isn't
        loaded (e.g., during early bot init, or a stripped-down dev
        config), this returns False - failing closed is the safe
        default: it just means a dividend doesn't pay during a
        broken stream-status state, which is far less bad than
        accidentally paying one to a no-one-watching session.

        Tests (the simulator in `testing.py`) can force this to True
        by setting `self._sim_force_live = True` before driving a
        synthetic match through the lifecycle.
        """
        if getattr(self, "_sim_force_live", False):
            return True
        if not self.bot:
            return False
        plugins = getattr(self.bot, "plugins", None)
        if not plugins:
            return False
        ss = plugins.get("stream_status")
        if ss is None or not hasattr(ss, "get_status"):
            return False
        try:
            return bool(ss.get_status().get("is_live"))
        except Exception:
            return False

    async def _get_profile_image(self, username: str) -> Optional[str]:
        """Fetch a Twitch user's profile image URL via Helix API."""
        if not self.token_manager:
            return None
        try:
            headers = await self.token_manager.get_broadcaster_headers()
            url = f"https://api.twitch.tv/helix/users?login={username}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        users = data.get("data", [])
                        if users:
                            return users[0].get("profile_image_url")
                    elif resp.status == 401 and self.token_manager:
                        refreshed = await self.token_manager.handle_401("broadcaster")
                        if refreshed:
                            headers = await self.token_manager.get_broadcaster_headers()
                            async with session.get(url, headers=headers) as retry:
                                if retry.status == 200:
                                    data = await retry.json()
                                    users = data.get("data", [])
                                    if users:
                                        return users[0].get("profile_image_url")
        except Exception as e:
            print(f"[Economy] Failed to fetch profile image for {username}: {e}")
        return None

    async def _get_chatters(self) -> List[str]:
        """
        Get list of current chatters via the Twitch Helix API.
        Uses GET /helix/chat/chatters with pagination to get ALL chatters
        (including lurkers connected to chat), not just those who typed.
        Falls back to TwitchIO channel.chatters if the API call fails.
        """
        # Try Helix API first (requires moderator:read:chatters scope)
        if self.token_manager:
            try:
                headers = await self.token_manager.get_broadcaster_headers()
                chatters = []
                cursor = None
                base_url = (
                    f"https://api.twitch.tv/helix/chat/chatters"
                    f"?broadcaster_id={TWITCH_OWNER_ID}"
                    f"&moderator_id={TWITCH_OWNER_ID}"
                    f"&first=1000"
                )

                async with aiohttp.ClientSession() as session:
                    while True:
                        url = base_url
                        if cursor:
                            url += f"&after={cursor}"

                        async with session.get(url, headers=headers) as resp:
                            if resp.status == 401 and self.token_manager:
                                # Token expired - refresh and retry once
                                refreshed = await self.token_manager.handle_401("broadcaster")
                                if refreshed:
                                    headers = await self.token_manager.get_broadcaster_headers()
                                    async with session.get(url, headers=headers) as retry_resp:
                                        if retry_resp.status == 200:
                                            data = await retry_resp.json()
                                        else:
                                            print(f"[Economy] Helix chatters retry failed: {retry_resp.status}")
                                            break
                                else:
                                    break
                            elif resp.status == 200:
                                data = await resp.json()
                            else:
                                body = await resp.text()
                                print(f"[Economy] Helix chatters API error: {resp.status} {body}")
                                break

                            for user in data.get("data", []):
                                name = user.get("user_login", "").lower()
                                if name and name != "hatmasbot":
                                    chatters.append(name)

                            # Check for more pages
                            cursor = data.get("pagination", {}).get("cursor")
                            if not cursor:
                                break

                if chatters:
                    print(f"[Economy] Helix chatters API: {len(chatters)} users in chat")
                    return chatters

            except Exception as e:
                print(f"[Economy] Helix chatters API failed, falling back to TwitchIO: {e}")

        # Fallback: TwitchIO channel.chatters (IRC-based, unreliable for large channels)
        if not self.bot:
            return []
        try:
            channel = self.bot.get_channel(TWITCH_CHANNEL)
            if channel and channel.chatters:
                names = [c.name for c in channel.chatters
                         if c.name.lower() != "hatmasbot"]
                print(f"[Economy] TwitchIO fallback chatters: {len(names)} users")
                return names
        except Exception as e:
            print(f"[Economy] Error getting chatters: {e}")
        return []

    @staticmethod
    def is_excluded_user(username: Optional[str]) -> bool:
        """True if `username` is on the bot-account exclusion list and
        should not earn shares, dividends, or appear on leaderboards."""
        if not username:
            return False
        return username.lower() in EXCLUDED_USERS_LOWER

    async def _distribute_free_shares(self, god_name: str) -> Dict:
        """
        Give 1 free share to all current viewers (chatters) at match end.
        Only people currently in chat get free shares - not offline
        portfolio holders. Excluded users (StreamElements, the bot
        itself, etc.) are filtered out so bots don't accumulate shares.
        """
        # Get live chatters from Twitch (Helix API with TwitchIO fallback)
        all_chatters = await self._get_chatters()
        viewers = {u for u in all_chatters if not self.is_excluded_user(u)}
        skipped = len(all_chatters) - len(viewers)

        price = self._prices.get(god_name, ECONOMY_STARTING_PRICE)

        for username in viewers:
            await self._add_shares(username, god_name, ECONOMY_FREE_SHARE_COUNT, 0)  # Cost basis 0 for free shares
            await self._db.execute("""
                INSERT INTO transactions (username, god_name, type, shares, price, total, fee)
                VALUES (?, ?, 'free_share', ?, ?, 0, 0)
            """, (username, god_name, ECONOMY_FREE_SHARE_COUNT, price))

        await self._db.commit()

        info = {
            "god_name": god_name,
            "shares_each": ECONOMY_FREE_SHARE_COUNT,
            "viewer_count": len(viewers),
            "share_value": round(price),
        }
        suffix = f" (skipped {skipped} excluded)" if skipped else ""
        print(f"[Economy] Free shares: {len(viewers)} chatters "
              f"each got {ECONOMY_FREE_SHARE_COUNT} share(s) of {god_name}"
              f"{suffix}")
        return info
