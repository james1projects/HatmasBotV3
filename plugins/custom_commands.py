"""
Custom Commands Plugin
=======================
Mod-created text commands, managed from the /mod page (no code, no
restart). Each command replies with a fixed text response. Supports
{user} (display name) and {args} placeholders.

Storage: data/custom_commands.json
  {"lurk": {"response": "...", "created_by": "modname",
            "created_at": "...", "updated_at": "..."}}

Rules:
  - Names: 1-32 chars, a-z 0-9 _ - (also satisfies Discord slash rules).
  - Responses: 1-450 chars (Twitch chat limit minus the @name prefix).
  - Built-in command names cannot be taken. This plugin registers LAST
    in main.py, so collisions are detected and skipped with a warning.
  - Custom commands get the full /mod treatment: platform toggles,
    cooldowns, audit log. Discord-eligible by default.
"""

import json
import re
from datetime import datetime

from core.config import DATA_DIR

STORE_FILE = DATA_DIR / "custom_commands.json"
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_RESPONSE = 450
MAX_COMMANDS = 100


class CustomCommandsPlugin:
    def __init__(self):
        self.bot = None
        self.commands = {}   # name -> {"response", "created_by", ...}
        self._load()

    # === persistence ===

    def _load(self):
        try:
            if STORE_FILE.exists():
                data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.commands = data
        except Exception as e:
            print(f"[CustomCmds] Failed to load store: {e}")
            self.commands = {}

    def _save(self):
        try:
            STORE_FILE.write_text(
                json.dumps(self.commands, indent=2, sort_keys=True),
                encoding="utf-8")
        except Exception as e:
            print(f"[CustomCmds] Failed to save store: {e}")

    # === plugin lifecycle ===

    def setup(self, bot):
        self.bot = bot
        registered = 0
        for name in sorted(self.commands):
            if self._register(name):
                registered += 1
        if registered:
            print(f"[CustomCmds] Registered {registered} custom command(s)")

    def _register(self, name):
        """Register one stored command with the bot. False on collision."""
        existing = self.bot._custom_commands.get(name)
        if existing is not None and not existing.get("custom"):
            print(f"[CustomCmds] Skipping !{name}: name taken by a "
                  f"built-in ({existing['plugin']})")
            return False
        entry = self.commands[name]
        self.bot.register_command(
            name, self._make_handler(name),
            description=entry["response"][:100],
            platforms=("twitch", "discord"),
            plugin="custom")
        self.bot._custom_commands[name]["custom"] = True
        return True

    def _make_handler(self, name):
        async def handler(message, args, whisper=False):
            entry = self.commands.get(name)
            if entry is None:
                return
            user = "user"
            if getattr(message, "chatter", None) is not None:
                user = (getattr(message.chatter, "display_name", None)
                        or getattr(message.chatter, "name", "user"))
            text = (entry["response"]
                    .replace("{user}", str(user))
                    .replace("{args}", args or ""))
            await self.bot.send_reply(message, text, whisper)
        return handler

    # === management API (called by the /mod webserver) ===

    @staticmethod
    def validate(name, response):
        """Returns an error string, or None if valid."""
        if not NAME_RE.match(name or ""):
            return "Name must be 1-32 chars: a-z 0-9 _ -"
        if not response or not response.strip():
            return "Response text is required."
        if len(response) > MAX_RESPONSE:
            return f"Response too long (max {MAX_RESPONSE} chars)."
        return None

    async def add_or_update(self, name, response, created_by):
        """Upsert a custom command. Returns (ok, error_or_action)."""
        name = (name or "").lower().strip()
        response = (response or "").strip()
        err = self.validate(name, response)
        if err:
            return False, err
        existing_builtin = self.bot._custom_commands.get(name)
        if existing_builtin is not None and not existing_builtin.get("custom"):
            return False, f"!{name} is a built-in command."
        is_new = name not in self.commands
        if is_new and len(self.commands) >= MAX_COMMANDS:
            return False, f"Limit of {MAX_COMMANDS} custom commands reached."
        now = datetime.now().isoformat(timespec="seconds")
        if is_new:
            self.commands[name] = {"response": response,
                                   "created_by": created_by,
                                   "created_at": now, "updated_at": now}
        else:
            self.commands[name].update(response=response, updated_at=now)
        self._save()
        if is_new:
            self._register(name)
        else:
            # refresh description so /mod and Discord show new text
            self.bot._custom_commands[name]["description"] = response[:100]
        await self.bot.notify_catalog_changed()
        return True, "added" if is_new else "updated"

    async def delete(self, name):
        """Remove a custom command. Returns (ok, error_or_action)."""
        name = (name or "").lower().strip()
        if name not in self.commands:
            return False, "No such custom command."
        del self.commands[name]
        self._save()
        cmd = self.bot._custom_commands.get(name)
        if cmd is not None and cmd.get("custom"):
            self.bot.unregister_command(name)
        await self.bot.notify_catalog_changed()
        return True, "deleted"

    def get_response(self, name):
        entry = self.commands.get((name or "").lower())
        return entry["response"] if entry else None
