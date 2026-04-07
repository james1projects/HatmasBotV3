"""
Gamble Plugin
==============
Viewers wager "Hats" (MixItUp currency) on a dice roll.

Rolls 1-100:
  1-59   → Loss (lose wager)
  60-97  → Win (double wager)
  98-99  → Triple win (triple wager)
  100    → Jackpot (triple wager + jackpot pool, pool resets)

The jackpot pool accumulates from every wager placed.
Supports keywords: all, half, quarter, or a specific amount.
Minimum bet: GAMBLE_MIN_BET (default 10).
"""

import asyncio
import json
import random
import time
import aiohttp

from core.config import (
    MIXITUP_API_BASE, GAMBLE_CURRENCY_NAME, GAMBLE_MIN_BET,
    GAMBLE_COOLDOWN, GAMBLE_JACKPOT_FILE, GAMBLE_ALERT_MIN_WAGER
)


class GamblePlugin:
    def __init__(self):
        self.bot = None
        self.session = None

        # MixItUp currency ID (resolved on startup)
        self._currency_id = None
        self._connected = False

        # Jackpot pool
        self.jackpot_pool = 0
        self._load_jackpot()

        # Per-user cooldown tracking
        self._cooldowns = {}  # username -> timestamp

        # Counter for jackpot display (show every ~5th gamble)
        self._gamble_count = 0

    # === DATA PERSISTENCE ===

    def _load_jackpot(self):
        try:
            if GAMBLE_JACKPOT_FILE.exists():
                with open(GAMBLE_JACKPOT_FILE, "r") as f:
                    data = json.load(f)
                    self.jackpot_pool = data.get("jackpot_pool", 0)
                    print(f"[Gamble] Loaded jackpot pool: {self.jackpot_pool} hats")
        except Exception as e:
            print(f"[Gamble] Failed to load jackpot: {e}")

    def _save_jackpot(self):
        try:
            with open(GAMBLE_JACKPOT_FILE, "w") as f:
                json.dump({"jackpot_pool": self.jackpot_pool}, f)
        except Exception as e:
            print(f"[Gamble] Failed to save jackpot: {e}")

    # === SETUP ===

    def setup(self, bot):
        self.bot = bot
        bot.register_command("gamble", self.cmd_gamble)
        bot.register_command("jackpot", self.cmd_jackpot)

    async def on_ready(self):
        self.session = aiohttp.ClientSession()
        await self._resolve_currency_id()

    # === MIXITUP CURRENCY API ===

    async def _miu_get(self, path):
        try:
            async with self.session.get(f"{MIXITUP_API_BASE}{path}") as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except aiohttp.ClientConnectorError:
            return None
        except Exception as e:
            print(f"[Gamble] MixItUp GET error: {e}")
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
                else:
                    body = await resp.text()
                    print(f"[Gamble] MixItUp PATCH {path}: {resp.status} {body}")
                    return None
        except Exception as e:
            print(f"[Gamble] MixItUp PATCH error: {e}")
            return None

    async def _resolve_currency_id(self):
        """Look up the MixItUp currency ID for Hats."""
        currencies = await self._miu_get("/currency")
        if not currencies:
            print(f"[Gamble] Could not connect to MixItUp API at {MIXITUP_API_BASE}")
            print(f"[Gamble] Make sure MixItUp is running and Developer API is enabled")
            return

        for curr in currencies:
            if curr.get("Name", "").lower() == GAMBLE_CURRENCY_NAME.lower():
                self._currency_id = curr["ID"]
                self._connected = True
                print(f"[Gamble] MixItUp connected — currency '{GAMBLE_CURRENCY_NAME}': {self._currency_id}")
                return

        print(f"[Gamble] Currency '{GAMBLE_CURRENCY_NAME}' not found in MixItUp!")
        available = [c.get("Name") for c in currencies]
        print(f"[Gamble] Available currencies: {available}")

    async def _get_user_id(self, twitch_username):
        """Look up MixItUp internal user ID from Twitch username."""
        data = await self._miu_get(f"/users/Twitch/{twitch_username}")
        if data and "User" in data:
            return data["User"]["ID"]
        return None

    async def _get_balance(self, twitch_username):
        """Get a user's Hats balance."""
        if not self._connected:
            return None

        user_id = await self._get_user_id(twitch_username)
        if not user_id:
            return None

        data = await self._miu_get(f"/currency/{self._currency_id}/{user_id}")
        if data:
            return data.get("Amount", 0)
        return 0

    async def _adjust_balance(self, twitch_username, amount):
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

    # === COMMANDS ===

    async def cmd_jackpot(self, message, args, whisper=False):
        """Show the current jackpot pool."""
        await self.bot.send_reply(
            message,
            f"The jackpot pool is currently {self.jackpot_pool:,} hats! "
            f"Roll 100 on !gamble to win it all.",
            whisper
        )

    async def cmd_gamble(self, message, args, whisper=False):
        """Main gamble command."""
        if not self.bot.is_feature_enabled("gamble"):
            await self.bot.send_reply(message, "Gambling is currently disabled.", whisper)
            return

        if not self._connected:
            await self.bot.send_reply(
                message, "Gambling isn't available right now (MixItUp not connected).", whisper
            )
            return

        username = message.chatter.name.lower()
        display_name = message.chatter.display_name or message.chatter.name
        now = time.time()

        # Cooldown check
        last_use = self._cooldowns.get(username, 0)
        if now - last_use < GAMBLE_COOLDOWN:
            remaining = int(GAMBLE_COOLDOWN - (now - last_use))
            await self.bot.send_reply(message, f"Cooldown: {remaining}s", whisper)
            return

        if not args:
            await self.bot.send_reply(
                message,
                f"Usage: !gamble <amount|all|half|quarter> (min: {GAMBLE_MIN_BET} hats)",
                whisper
            )
            return

        # Get current balance
        balance = await self._get_balance(username)
        if balance is None:
            await self.bot.send_reply(message, "Couldn't check your balance. Try again.", whisper)
            return

        # Parse wager amount
        wager = self._parse_wager(args.strip().lower(), balance)
        if wager is None:
            await self.bot.send_reply(
                message,
                f"Invalid amount. Use a number, 'all', 'half', or 'quarter'.",
                whisper
            )
            return

        if wager < GAMBLE_MIN_BET:
            await self.bot.send_reply(
                message,
                f"Minimum bet is {GAMBLE_MIN_BET} hats. You tried to bet {wager}.",
                whisper
            )
            return

        if wager > balance:
            await self.bot.send_reply(
                message,
                f"You only have {balance:,} hats but tried to bet {wager:,}.",
                whisper
            )
            return

        # Set cooldown
        self._cooldowns[username] = now

        # Add wager to jackpot pool
        self.jackpot_pool += wager
        self._save_jackpot()

        # Roll!
        roll = random.randint(1, 100)
        self._gamble_count += 1

        if roll == 100:
            # JACKPOT — triple wager + jackpot pool (capped at 100x wager)
            jackpot_cap = wager * 100
            jackpot_payout = min(self.jackpot_pool, jackpot_cap)
            winnings = (wager * 3) + jackpot_payout
            net = winnings - wager
            await self._adjust_balance(username, net)
            self.jackpot_pool -= jackpot_payout
            self._save_jackpot()
            pool_msg = "The jackpot pool has been reset." if self.jackpot_pool == 0 else f"Jackpot pool remaining: {self.jackpot_pool:,} hats."
            await self.bot.send_reply(
                message,
                f"JACKPOT!!! {display_name} rolled {roll} and won "
                f"{winnings:,} hats (triple + jackpot)! {pool_msg}",
                whisper
            )
            # Jackpot ALWAYS triggers alert regardless of wager
            self._fire_gamble_alert("jackpot", display_name, roll, winnings, wager)

        elif roll >= 98:
            # Triple win
            winnings = wager * 3
            net = winnings - wager
            await self._adjust_balance(username, net)
            jackpot_text = self._maybe_jackpot_text()
            await self.bot.send_reply(
                message,
                f"TRIPLE WIN! {display_name} rolled {roll} and won "
                f"{winnings:,} hats!{jackpot_text}",
                whisper
            )
            if wager >= GAMBLE_ALERT_MIN_WAGER:
                self._fire_gamble_alert("big_win", display_name, roll, winnings, wager)

        elif roll >= 60:
            # Normal win — double
            winnings = wager * 2
            net = winnings - wager
            await self._adjust_balance(username, net)
            jackpot_text = self._maybe_jackpot_text()
            await self.bot.send_reply(
                message,
                f"{display_name} rolled {roll} and won {winnings:,} hats!{jackpot_text}",
                whisper
            )
            if wager >= GAMBLE_ALERT_MIN_WAGER:
                self._fire_gamble_alert("win", display_name, roll, winnings, wager)

        else:
            # Loss
            await self._adjust_balance(username, -wager)
            jackpot_text = self._maybe_jackpot_text()
            await self.bot.send_reply(
                message,
                f"{display_name} rolled {roll} and lost {wager:,} hats.{jackpot_text}",
                whisper
            )
            if wager >= GAMBLE_ALERT_MIN_WAGER:
                self._fire_gamble_alert("loss", display_name, roll, 0, wager)

    def _fire_gamble_alert(self, alert_type, display_name, roll, winnings, wager):
        """Trigger sound + visual alert for a gamble result."""
        if not self.bot.web_server:
            return
        self.bot.web_server.trigger_sound_alert(alert_type)
        self.bot.web_server.trigger_gamble_result({
            "type": alert_type,
            "player": display_name,
            "roll": roll,
            "winnings": winnings,
            "wager": wager,
        })

    # === HELPERS ===

    @staticmethod
    def _parse_wager(text, balance):
        """Parse wager from text. Returns int or None."""
        if text == "all":
            return balance
        elif text == "half":
            return balance // 2
        elif text == "quarter":
            return balance // 4
        else:
            try:
                amount = int(text)
                if amount > 0:
                    return amount
                return None
            except ValueError:
                return None

    def _maybe_jackpot_text(self):
        """~20% of the time, append jackpot pool info."""
        if self._gamble_count % 5 == 0 and self.jackpot_pool > 0:
            return f" (Jackpot pool: {self.jackpot_pool:,} hats)"
        return ""

    async def cleanup(self):
        self._save_jackpot()
        if self.session:
            await self.session.close()
