"""
Basic Commands Plugin
======================
Simple utility commands for HatmasBot.
Updated for TwitchIO v3.
"""

import json
import time
from datetime import datetime
from core.config import DATA_DIR

SUGGESTIONS_FILE = DATA_DIR / "suggestions.json"
SUGGEST_COOLDOWN = 60  # Seconds between suggestions per user


class BasicPlugin:
    def __init__(self):
        self.bot = None
        self._suggest_cooldowns = {}  # username -> last suggestion timestamp
        self._suggestions = []
        self._load_suggestions()

    def _load_suggestions(self):
        try:
            if SUGGESTIONS_FILE.exists():
                with open(SUGGESTIONS_FILE) as f:
                    self._suggestions = json.load(f)
        except Exception as e:
            print(f"[Basic] Failed to load suggestions: {e}")
            self._suggestions = []

    def _save_suggestions(self):
        try:
            with open(SUGGESTIONS_FILE, "w") as f:
                json.dump(self._suggestions, f, indent=2)
        except Exception as e:
            print(f"[Basic] Failed to save suggestions: {e}")

    def setup(self, bot):
        self.bot = bot
        bot.register_command("hello", self.cmd_hello)
        bot.register_command("commands", self.cmd_commands)
        bot.register_command("uptime", self.cmd_uptime)
        bot.register_command("socials", self.cmd_socials)
        bot.register_command("suggest", self.cmd_suggest)
        bot.register_command("suggestions", self.cmd_suggestions, mod_only=True)
        bot.register_command("clearsuggestions", self.cmd_clearsuggestions, mod_only=True)

    async def cmd_hello(self, message, args, whisper=False):
        name = message.chatter.display_name if message.chatter else "friend"
        await self.bot.send_reply(
            message,
            f"Hello {name}!",
            whisper
        )

    async def cmd_commands(self, message, args, whisper=False):
        cmds = sorted(self.bot._custom_commands.keys())
        if message.chatter and not self.bot.is_mod(message.chatter):
            cmds = [c for c in cmds if not self.bot._custom_commands[c]["mod_only"]]
        await self.bot.send_reply(
            message,
            f"Commands: {', '.join('!' + c for c in cmds)}",
            whisper
        )

    async def cmd_uptime(self, message, args, whisper=False):
        uptime = self.bot.get_uptime()
        await self.bot.send_reply(
            message, f"HatmasBot has been running for {uptime}", whisper
        )

    async def cmd_socials(self, message, args, whisper=False):
        await self.bot.send_reply(
            message,
            "YouTube: youtube.com/@hatmaster | "
            "Bluesky: @hatmasteryt.bsky.social | "
            "Twitch: twitch.tv/hatmaster",
            whisper
        )

    # === SUGGESTION BOX ===

    async def cmd_suggest(self, message, args, whisper=False):
        """!suggest <suggestion> - Submit a suggestion for the stream."""
        if not args or not args.strip():
            await self.bot.send_reply(message, "Usage: !suggest <your suggestion>", whisper)
            return

        username = message.chatter.name if message.chatter else "unknown"
        display_name = message.chatter.display_name if message.chatter else username

        # Cooldown check
        now = time.time()
        last = self._suggest_cooldowns.get(username.lower(), 0)
        if now - last < SUGGEST_COOLDOWN:
            remaining = int(SUGGEST_COOLDOWN - (now - last))
            await self.bot.send_reply(message, f"Cooldown: {remaining}s", whisper)
            return

        self._suggest_cooldowns[username.lower()] = now

        suggestion = {
            "user": display_name,
            "text": args.strip()[:500],  # Cap at 500 chars
            "timestamp": datetime.now().isoformat(),
        }
        self._suggestions.append(suggestion)
        self._save_suggestions()

        count = len(self._suggestions)
        await self.bot.send_reply(
            message,
            f"Suggestion #{count} saved! Thanks {display_name}.",
            whisper
        )
        print(f"[Suggest] #{count} from {display_name}: {args.strip()[:80]}")

    async def cmd_suggestions(self, message, args, whisper=False):
        """!suggestions - Mods: show recent suggestions count or list."""
        count = len(self._suggestions)
        if count == 0:
            await self.bot.send_reply(message, "No suggestions yet.", whisper)
            return

        # Show the last 3
        recent = self._suggestions[-3:]
        parts = [f"#{len(self._suggestions) - len(recent) + i + 1} {s['user']}: {s['text'][:60]}" for i, s in enumerate(recent)]
        await self.bot.send_reply(
            message,
            f"{count} total. Recent: {' | '.join(parts)}",
            whisper
        )

    async def cmd_clearsuggestions(self, message, args, whisper=False):
        """!clearsuggestions - Mods: clear all suggestions."""
        count = len(self._suggestions)
        self._suggestions = []
        self._save_suggestions()
        await self.bot.send_reply(message, f"Cleared {count} suggestions.", whisper)
