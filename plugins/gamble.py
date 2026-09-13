"""
Gamble Plugin
==============
Viewers wager "Hats" (core/wallet.py) on a dice roll.

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

from core import db as _shared_db
from core import wallet as _wallet
from core.config import (
    GAMBLE_MIN_BET, GAMBLE_COOLDOWN, GAMBLE_JACKPOT_FILE,
    GAMBLE_ALERT_MIN_WAGER
)


class GamblePlugin:
    def __init__(self):
        self.bot = None
        self._db = None  # shared economy.db connection (core/db.py)

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
            from core.atomic_io import atomic_write_json
            atomic_write_json(GAMBLE_JACKPOT_FILE,
                              {"jackpot_pool": self.jackpot_pool}, indent=None)
        except Exception as e:
            print(f"[Gamble] Failed to save jackpot: {e}")

    # === SETUP ===

    def setup(self, bot):
        self.bot = bot
        bot.register_command("gamble", self.cmd_gamble,
                             description="Wager Hats: amount, all, half, or quarter", identity=True, plugin="gamble")
        bot.register_command("jackpot", self.cmd_jackpot,
                             description="Current jackpot pool", platforms=("twitch", "discord"), plugin="gamble")

    async def on_ready(self):
        self._db = await _shared_db.get_db()
        if self._db is None:
            print("[Gamble] DB unavailable — gambling disabled")

    @property
    def _connected(self) -> bool:
        return self._db is not None

    # === WALLET ===

    async def _get_balance(self, user_uuid):
        """Get a user's Hats balance."""
        if not self._connected or not user_uuid:
            return None
        return await _wallet.get(self._db, user_uuid, "hats")

    async def _adjust_balance(self, user_uuid, amount, roll=None, wager=None):
        """Add or subtract hats. Positive = add, negative = subtract."""
        if not self._connected or not user_uuid or not amount:
            return False
        res = await _wallet.adjust(
            self._db, user_uuid, "hats", int(amount),
            "gamble_win" if amount > 0 else "gamble_loss",
            note=f"roll {roll} wager {wager}" if roll is not None else None)
        return res is not None

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
                message, "Gambling isn't available right now (wallet not ready).", whisper
            )
            return

        username = message.chatter.name.lower()
        display_name = message.chatter.display_name or message.chatter.name
        now = time.time()
        user_uuid = await self.bot.user_uuid_for(message.chatter)
        if not user_uuid:
            await self.bot.send_reply(message, "Hats are still loading. Try again in a moment.", whisper)
            return

        # Cooldown check. The slot is CLAIMED here, before the awaited
        # balance fetch below - previously it was only recorded after
        # all validation, so two rapid !gamble messages from the same
        # user could both pass this check and double-bet a balance
        # that covers only one wager. Validation failures release the
        # claim (see _release_cooldown call sites) so a typo doesn't
        # burn the cooldown.
        last_use = self._cooldowns.get(username, 0)
        if now - last_use < GAMBLE_COOLDOWN:
            remaining = int(GAMBLE_COOLDOWN - (now - last_use))
            await self.bot.send_reply(message, f"Cooldown: {remaining}s", whisper)
            return
        self._cooldowns[username] = now

        def _release_cooldown():
            self._cooldowns[username] = last_use

        if not args:
            _release_cooldown()
            await self.bot.send_reply(
                message,
                f"Usage: !gamble <amount|all|half|quarter> (min: {GAMBLE_MIN_BET} hats)",
                whisper
            )
            return

        # Get current balance
        balance = await self._get_balance(user_uuid)
        if balance is None:
            _release_cooldown()
            await self.bot.send_reply(message, "Couldn't check your balance. Try again.", whisper)
            return

        # Parse wager amount
        wager = self._parse_wager(args.strip().lower(), balance)
        if wager is None:
            _release_cooldown()
            await self.bot.send_reply(
                message,
                f"Invalid amount. Use a number, 'all', 'half', or 'quarter'.",
                whisper
            )
            return

        if wager < GAMBLE_MIN_BET:
            _release_cooldown()
            await self.bot.send_reply(
                message,
                f"Minimum bet is {GAMBLE_MIN_BET} hats. You tried to bet {wager}.",
                whisper
            )
            return

        if wager > balance:
            _release_cooldown()
            await self.bot.send_reply(
                message,
                f"You only have {balance:,} hats but tried to bet {wager:,}.",
                whisper
            )
            return

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
            await self._adjust_balance(user_uuid, net, roll, wager)
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
            await self._adjust_balance(user_uuid, net, roll, wager)
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
            await self._adjust_balance(user_uuid, net, roll, wager)
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
            await self._adjust_balance(user_uuid, -wager, roll, wager)
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
        self._db = None
