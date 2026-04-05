"""
Smite 2 Plugin
===============
Stat lookups via tracker.gg API, live match detection, auto predictions.
"""

import asyncio
import aiohttp
import json
from datetime import datetime

from core.config import (
    SMITE2_PLATFORM, SMITE2_PLATFORM_ID, SMITE2_TRACKER_BASE,
    SMITE2_POLL_INTERVAL, SMITE2_CACHE_TTL, TWITCH_CHANNEL
)
from core.cache import Cache


class SmitePlugin:
    def __init__(self):
        self.bot = None
        self.cache = Cache()
        self.session = None
        self.is_in_match = False
        self.prediction_id = None
        self._poll_task = None

    def setup(self, bot):
        self.bot = bot
        bot.register_command("stats", self.cmd_stats)
        bot.register_command("god", self.cmd_god)
        bot.register_command("match", self.cmd_match)
        bot.register_command("rank", self.cmd_rank)
        bot.register_command("winrate", self.cmd_winrate)
        bot.register_command("kda", self.cmd_kda)
        bot.register_command("damage", self.cmd_damage)

    async def on_ready(self):
        self.session = aiohttp.ClientSession(headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://tracker.gg/smite2",
            "Origin": "https://tracker.gg",
        })
        if self.bot.is_feature_enabled("smite_tracking"):
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _fetch_profile(self, platform=None, player_id=None):
        platform = platform or SMITE2_PLATFORM
        player_id = player_id or SMITE2_PLATFORM_ID

        cache_key = f"profile_{platform}_{player_id}"
        cached = self.cache.get(cache_key, ttl=SMITE2_CACHE_TTL)
        if cached:
            return cached

        url = f"{SMITE2_TRACKER_BASE}/{platform}/{player_id}"
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.cache.set(cache_key, data)
                    return data
                else:
                    print(f"[Smite] API returned {resp.status}")
                    return None
        except Exception as e:
            print(f"[Smite] Fetch error: {e}")
            return None

    def _get_gamemode_stats(self, data, gamemode="conquest-ranked"):
        if not data or "data" not in data:
            return None
        segments = data["data"].get("segments", [])
        for seg in segments:
            if seg.get("type") == "gamemode":
                attrs = seg.get("attributes", {})
                if attrs.get("gamemode") == gamemode:
                    return seg.get("stats", {})
        return None

    def _get_god_stats(self, data, god_name):
        if not data or "data" not in data:
            return None
        segments = data["data"].get("segments", [])
        for seg in segments:
            if seg.get("type") == "god":
                meta = seg.get("metadata", {})
                if meta.get("name", "").lower() == god_name.lower():
                    return seg
        return None

    def _format_stat(self, stats, key):
        stat = stats.get(key, {})
        return stat.get("displayValue", "N/A")

    # === COMMANDS ===

    async def cmd_stats(self, message, args, whisper=False):
        data = await self._fetch_profile()
        if not data:
            await self.bot.send_reply(message, "Couldn't fetch stats right now.", whisper)
            return

        stats = self._get_gamemode_stats(data)
        if not stats:
            await self.bot.send_reply(message, "No ranked conquest stats found.", whisper)
            return

        kills = self._format_stat(stats, "kills")
        deaths = self._format_stat(stats, "deaths")
        assists = self._format_stat(stats, "assists")
        winrate = self._format_stat(stats, "matchesWinPct")
        matches = self._format_stat(stats, "matchesPlayed")
        kda = self._format_stat(stats, "kdaRatio")

        await self.bot.send_reply(
            message,
            f"Ranked Conquest: {matches} games | {winrate} WR | "
            f"K/D/A: {kills}/{deaths}/{assists} | KDA: {kda}",
            whisper
        )

    async def cmd_god(self, message, args, whisper=False):
        if not args:
            await self.bot.send_reply(message, "Usage: !god <god name>", whisper)
            return

        god_name = args.strip()
        data = await self._fetch_profile()
        if not data:
            await self.bot.send_reply(message, "Couldn't fetch stats right now.", whisper)
            return

        god = self._get_god_stats(data, god_name)
        if not god:
            await self.bot.send_reply(
                message, f"No stats found for {god_name}.", whisper
            )
            return

        stats = god.get("stats", {})
        name = god.get("metadata", {}).get("name", god_name)
        kills = self._format_stat(stats, "kills")
        deaths = self._format_stat(stats, "deaths")
        assists = self._format_stat(stats, "assists")
        matches = self._format_stat(stats, "matchesPlayed")
        winrate = self._format_stat(stats, "matchesWinPct")

        await self.bot.send_reply(
            message,
            f"{name}: {matches} games | {winrate} WR | K/D/A: {kills}/{deaths}/{assists}",
            whisper
        )

    async def cmd_match(self, message, args, whisper=False):
        data = await self._fetch_profile()
        if not data:
            await self.bot.send_reply(message, "Couldn't fetch stats right now.", whisper)
            return

        meta = data.get("data", {}).get("metadata", {})
        live = meta.get("liveMatch", False)

        if live:
            await self.bot.send_reply(message, "Hatmaster is currently in a match!", whisper)
        else:
            await self.bot.send_reply(message, "No active match right now.", whisper)

    async def cmd_rank(self, message, args, whisper=False):
        data = await self._fetch_profile()
        if not data:
            await self.bot.send_reply(message, "Couldn't fetch stats right now.", whisper)
            return

        stats = self._get_gamemode_stats(data)
        if not stats:
            await self.bot.send_reply(message, "No ranked stats found.", whisper)
            return

        matches = self._format_stat(stats, "matchesPlayed")
        wins = self._format_stat(stats, "matchesWon")
        losses = self._format_stat(stats, "matchesLost")

        await self.bot.send_reply(
            message, f"Ranked: {matches} games | {wins}W {losses}L", whisper
        )

    async def cmd_winrate(self, message, args, whisper=False):
        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch stats.", whisper)
            return
        wr = self._format_stat(stats, "matchesWinPct")
        await self.bot.send_reply(message, f"Win rate: {wr}", whisper)

    async def cmd_kda(self, message, args, whisper=False):
        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch stats.", whisper)
            return
        kda = self._format_stat(stats, "kdaRatio")
        kad = self._format_stat(stats, "kadRatio")
        await self.bot.send_reply(message, f"KDA: {kda} | KA/D: {kad}", whisper)

    async def cmd_damage(self, message, args, whisper=False):
        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch stats.", whisper)
            return
        total = self._format_stat(stats, "damage")
        per_match = self._format_stat(stats, "damagePerMatch")
        per_min = self._format_stat(stats, "damagePerMinute")
        matches = self._format_stat(stats, "matchesPlayed")
        await self.bot.send_reply(
            message,
            f"Total damage: {total} across {matches} games | "
            f"{per_match}/game | {per_min}/min. Any questions?",
            whisper
        )

    # === LIVE MATCH DETECTION & PREDICTIONS ===

    async def _poll_loop(self):
        """Poll tracker.gg for live match status changes."""
        while True:
            try:
                if self.bot.is_feature_enabled("smite_tracking"):
                    await self._check_match_status()
            except Exception as e:
                print(f"[Smite Poll] Error: {e}")
            await asyncio.sleep(SMITE2_POLL_INTERVAL)

    async def _check_match_status(self):
        self.cache.clear()  # Force fresh data for match detection
        data = await self._fetch_profile()
        if not data:
            return

        meta = data.get("data", {}).get("metadata", {})
        live = meta.get("liveMatch", False)

        if live and not self.is_in_match:
            # Game started
            self.is_in_match = True
            print("[Smite] Match started!")

            if self.bot.is_feature_enabled("predictions"):
                await self._create_prediction()

            if self.bot.is_feature_enabled("auto_scene_switch"):
                if "obs" in self.bot.plugins:
                    await self.bot.plugins["obs"].switch_to_game()

        elif not live and self.is_in_match:
            # Game ended
            self.is_in_match = False
            print("[Smite] Match ended!")

            if self.prediction_id:
                # Check the most recent match result
                await self._check_match_result(data)

            if self.bot.is_feature_enabled("auto_scene_switch"):
                if "obs" in self.bot.plugins:
                    await self.bot.plugins["obs"].switch_to_lobby()

    async def _create_prediction(self):
        """Create a Twitch prediction for the match."""
        try:
            # Use Twitch API to create prediction
            # This requires the channel:manage:predictions scope
            headers = {
                "Client-ID": self.bot._http.client_id,
                "Authorization": f"Bearer {self.bot._http.token}",
                "Content-Type": "application/json",
            }
            payload = {
                "broadcaster_id": await self._get_broadcaster_id(),
                "title": "Will Hatmaster win this game?",
                "outcomes": [
                    {"title": "Win"},
                    {"title": "Loss"}
                ],
                "prediction_window": 120,  # 2 minutes to bet
            }
            async with self.session.post(
                "https://api.twitch.tv/helix/predictions",
                headers=headers,
                json=payload
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.prediction_id = data["data"][0]["id"]
                    print(f"[Smite] Prediction created: {self.prediction_id}")
                    await self.bot.send_chat(
                        "A new game has started! Will Hatmaster win? "
                        "Place your bets!"
                    )
                else:
                    print(f"[Smite] Prediction creation failed: {resp.status}")
        except Exception as e:
            print(f"[Smite] Prediction error: {e}")

    async def _check_match_result(self, data):
        """Check if we can determine the match result."""
        stats = self._get_gamemode_stats(data)
        if not stats:
            print("[Smite] Can't determine match result - manual resolve needed")
            return

        # We can't easily determine the LAST match result from overview stats
        # The prediction should be manually resolved via control panel
        print("[Smite] Match ended - resolve prediction via control panel")

    async def resolve_prediction(self, outcome):
        """Manually resolve a prediction. outcome = 'win' or 'loss'"""
        if not self.prediction_id:
            print("[Smite] No active prediction to resolve")
            return

        try:
            headers = {
                "Client-ID": self.bot._http.client_id,
                "Authorization": f"Bearer {self.bot._http.token}",
                "Content-Type": "application/json",
            }
            # Determine winning outcome ID
            outcome_index = 0 if outcome == "win" else 1

            payload = {
                "broadcaster_id": await self._get_broadcaster_id(),
                "id": self.prediction_id,
                "status": "RESOLVED",
                "winning_outcome_id": outcome_index,
            }
            async with self.session.patch(
                "https://api.twitch.tv/helix/predictions",
                headers=headers,
                json=payload
            ) as resp:
                if resp.status == 200:
                    print(f"[Smite] Prediction resolved: {outcome}")
                    self.prediction_id = None
                else:
                    print(f"[Smite] Resolve failed: {resp.status}")
        except Exception as e:
            print(f"[Smite] Resolve error: {e}")

    async def _get_broadcaster_id(self):
        """Get the broadcaster's Twitch user ID."""
        cache_key = "broadcaster_id"
        cached = self.cache.get(cache_key, ttl=3600)
        if cached:
            return cached

        headers = {
            "Client-ID": self.bot._http.client_id,
            "Authorization": f"Bearer {self.bot._http.token}",
        }
        async with self.session.get(
            f"https://api.twitch.tv/helix/users?login={TWITCH_CHANNEL}",
            headers=headers
        ) as resp:
            data = await resp.json()
            user_id = data["data"][0]["id"]
            self.cache.set(cache_key, user_id)
            return user_id

    async def cleanup(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self.session:
            await self.session.close()
