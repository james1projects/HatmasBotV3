"""
Snap Plugin
============
The Thanos snap. Randomly eliminates half of chat for 10 minutes.
Triggers OBS scene, tracks stats.
"""

import asyncio
import json
import random
import time
from datetime import datetime

from core.config import (
    SNAP_TIMEOUT_DURATION, SNAP_COOLDOWN, SNAP_STATS_FILE,
    TWITCH_CHANNEL
)


class SnapPlugin:
    def __init__(self):
        self.bot = None
        self.last_snap = 0
        self.stats = {"total_snaps": 0, "total_snapped": 0, "victims": {}}
        self._load_stats()

    def _load_stats(self):
        if SNAP_STATS_FILE.exists():
            with open(SNAP_STATS_FILE) as f:
                self.stats = json.load(f)

    def _save_stats(self):
        with open(SNAP_STATS_FILE, "w") as f:
            json.dump(self.stats, f, indent=2)

    def setup(self, bot):
        self.bot = bot
        bot.register_command("snap", self.cmd_snap, mod_only=True)
        bot.register_command("snapstats", self.cmd_snapstats)

    async def cmd_snap(self, message, args, whisper=False):
        if not self.bot.is_feature_enabled("snap"):
            await self.bot.send_reply(message, "Snap is currently disabled.", whisper)
            return

        now = time.time()
        if now - self.last_snap < SNAP_COOLDOWN:
            remaining = int(SNAP_COOLDOWN - (now - self.last_snap))
            await self.bot.send_reply(
                message,
                f"The gauntlet needs {remaining}s to recharge.",
                whisper
            )
            return

        await self.execute_snap()

    async def execute_snap(self):
        """Execute the snap - can be called from command or control panel."""
        self.last_snap = time.time()

        # Trigger OBS snap scene
        if "obs" in self.bot.plugins:
            await self.bot.plugins["obs"].trigger_snap_scene()

        await self.bot.send_chat("Hatmaster raises the gauntlet...")
        await asyncio.sleep(3)

        # Get chatters
        try:
            chatters = await self._get_chatters()
        except Exception as e:
            print(f"[Snap] Error getting chatters: {e}")
            await self.bot.send_chat("The snap fizzled. Try again later.")
            return

        if not chatters:
            await self.bot.send_chat("No one is here to snap...")
            return

        # Filter out mods and the broadcaster
        snappable = [
            c for c in chatters
            if c.lower() != TWITCH_CHANNEL.lower()
            and c.lower() != "hatmasbot"
        ]

        if not snappable:
            await self.bot.send_chat("Everyone is immune to the snap!")
            return

        # Snap half
        random.shuffle(snappable)
        half = len(snappable) // 2
        victims = snappable[:max(half, 1)]

        await self.bot.send_chat(
            f"*snap* {len(victims)} of {len(snappable)} chatters "
            f"have been eliminated."
        )

        # Timeout victims
        snapped_names = []
        for victim in victims:
            try:
                channel = self.bot.get_channel(TWITCH_CHANNEL)
                if channel:
                    await channel.send(
                        f"/timeout {victim} {SNAP_TIMEOUT_DURATION} "
                        f"Snapped by Hatmaster's gauntlet"
                    )
                    snapped_names.append(victim)
            except Exception as e:
                print(f"[Snap] Failed to timeout {victim}: {e}")

        # Update stats
        self.stats["total_snaps"] += 1
        self.stats["total_snapped"] += len(snapped_names)
        for victim in snapped_names:
            v = victim.lower()
            self.stats["victims"][v] = self.stats["victims"].get(v, 0) + 1
        self._save_stats()

        # Return to normal scene after a delay
        if "obs" in self.bot.plugins:
            await asyncio.sleep(5)
            await self.bot.plugins["obs"].return_from_snap()

    async def _get_chatters(self):
        """Get list of current chatters in the channel."""
        # TwitchIO provides chatters through the channel object
        channel = self.bot.get_channel(TWITCH_CHANNEL)
        if channel:
            chatters = channel.chatters
            if chatters:
                return [c.name for c in chatters]
        return []

    async def cmd_snapstats(self, message, args, whisper=False):
        if args:
            # Check stats for a specific user
            username = args.strip().lower().replace("@", "")
            times_snapped = self.stats["victims"].get(username, 0)
            await self.bot.send_reply(
                message,
                f"{username} has been snapped {times_snapped} time(s).",
                whisper
            )
        else:
            # Overall stats
            total = self.stats["total_snaps"]
            snapped = self.stats["total_snapped"]

            # Find most snapped viewer
            most_snapped = None
            most_count = 0
            for name, count in self.stats["victims"].items():
                if count > most_count:
                    most_count = count
                    most_snapped = name

            text = f"Total snaps: {total} | Viewers eliminated: {snapped}"
            if most_snapped:
                text += f" | Most snapped: {most_snapped} ({most_count}x)"

            await self.bot.send_reply(message, text, whisper)
