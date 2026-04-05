"""
HatmasBot Core
===============
Handles Twitch EventSub connection, command routing, and plugin management.
Built for TwitchIO v3.
"""

import asyncio
import traceback
from datetime import datetime

import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from core.config import (
    TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET,
    TWITCH_BOT_TOKEN, TWITCH_BOT_USERNAME, TWITCH_CHANNEL,
    DEFAULT_FEATURES
)


class HatmasBot(commands.Bot):
    def __init__(self, web_server=None, bot_id=None, owner_id=None):
        super().__init__(
            client_id=TWITCH_CLIENT_ID,
            client_secret=TWITCH_CLIENT_SECRET,
            bot_id=bot_id or "",
            owner_id=owner_id,
            prefix="!",
        )
        self.plugins = {}
        self.features = dict(DEFAULT_FEATURES)
        self.web_server = web_server
        self.start_time = datetime.now()
        self.command_count = 0
        self._custom_commands = {}
        self._mention_handlers = []
        self._raw_handlers = []
        self._owner_id = owner_id
        self._bot_id = bot_id

    # =============================================================
    # SETUP
    # =============================================================

    async def setup_hook(self):
        """Called when the bot is ready to subscribe to events."""
        if self._owner_id and self._bot_id:
            payload = eventsub.ChatMessageSubscription(
                broadcaster_user_id=self._owner_id,
                user_id=self._bot_id,
            )
            await self.subscribe_websocket(payload=payload)
            print(f"[HatmasBot] Subscribed to chat events")

            try:
                whisper_payload = eventsub.UserWhisperMessageSubscription(
                    user_id=self._bot_id,
                )
                await self.subscribe_websocket(payload=whisper_payload)
                print(f"[HatmasBot] Subscribed to whisper events")
            except Exception as e:
                print(f"[HatmasBot] Whisper subscription failed (non-critical): {e}")

        print(f"[HatmasBot] Commands: {list(self._custom_commands.keys())}")
        print(f"[HatmasBot] Plugins: {list(self.plugins.keys())}")

        for name, plugin in self.plugins.items():
            if hasattr(plugin, "on_ready"):
                try:
                    await plugin.on_ready()
                except Exception as e:
                    print(f"[Plugin] {name} on_ready failed: {e}")

    # =============================================================
    # PLUGIN SYSTEM
    # =============================================================

    def register_plugin(self, name, plugin_instance):
        self.plugins[name] = plugin_instance
        if hasattr(plugin_instance, "setup"):
            plugin_instance.setup(self)
        print(f"[Plugin] {name} loaded")

    def register_command(self, name, handler, mod_only=False):
        self._custom_commands[name.lower()] = {
            "handler": handler,
            "mod_only": mod_only,
        }

    def register_mention_handler(self, handler):
        self._mention_handlers.append(handler)

    def register_raw_handler(self, handler):
        self._raw_handlers.append(handler)

    def is_mod(self, chatter):
        if hasattr(chatter, "moderator") and chatter.moderator:
            return True
        if hasattr(chatter, "broadcaster") and chatter.broadcaster:
            return True
        if hasattr(chatter, "badges"):
            badges = chatter.badges or []
            for badge in badges:
                badge_id = badge.id if hasattr(badge, "id") else str(badge)
                if badge_id in ("broadcaster", "moderator"):
                    return True
        name = chatter.name.lower() if hasattr(chatter, "name") else str(chatter).lower()
        return name == TWITCH_CHANNEL.lower()

    def is_sub(self, chatter):
        if hasattr(chatter, "subscriber") and chatter.subscriber:
            return True
        if hasattr(chatter, "badges"):
            badges = chatter.badges or []
            for badge in badges:
                badge_id = badge.id if hasattr(badge, "id") else str(badge)
                if badge_id == "subscriber":
                    return True
        return False

    def is_feature_enabled(self, feature):
        return self.features.get(feature, False)

    def set_feature(self, feature, enabled):
        if feature in self.features:
            self.features[feature] = enabled
            print(f"[Feature] {feature} = {enabled}")

    # =============================================================
    # EVENT HANDLERS
    # =============================================================

    async def event_message(self, payload: twitchio.ChatMessage):
        chatter = payload.chatter
        if chatter and chatter.name.lower() == TWITCH_BOT_USERNAME.lower():
            return

        content = payload.text.strip()

        if content.startswith("!"):
            self.command_count += 1

        for handler in self._raw_handlers:
            try:
                await handler(payload)
            except Exception as e:
                print(f"[Raw Handler Error] {e}")

        if TWITCH_BOT_USERNAME.lower() in content.lower():
            for handler in self._mention_handlers:
                try:
                    await handler(payload)
                except Exception as e:
                    print(f"[Mention Handler Error] {e}")
            return

        if content.startswith("!"):
            parts = content[1:].split(maxsplit=1)
            cmd_name = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            if cmd_name in self._custom_commands:
                cmd = self._custom_commands[cmd_name]

                if cmd["mod_only"] and not self.is_mod(chatter):
                    return

                try:
                    await cmd["handler"](payload, args)
                except Exception as e:
                    print(f"[Command Error] !{cmd_name}: {e}")
                    traceback.print_exc()

        await self.process_commands(payload)

    # =============================================================
    # UTILITY METHODS
    # =============================================================

    async def send_chat(self, text):
        if self._owner_id and self._bot_id:
            broadcaster = self.create_partialuser(int(self._owner_id))
            sender = self.create_partialuser(int(self._bot_id))
            try:
                await broadcaster.send_message(sender=sender, message=text)
            except Exception as e:
                print(f"[Chat] Send failed: {e}")

    async def send_reply(self, message, text, whisper=False):
        chatter = message.chatter if hasattr(message, "chatter") else None
        chatter_name = chatter.display_name if chatter else "user"

        if whisper and chatter:
            try:
                sender = self.create_partialuser(int(self._bot_id))
                await chatter.send_whisper(sender=sender, message=text)
                return
            except Exception:
                pass

        try:
            await message.respond(f"@{chatter_name} {text}")
        except Exception:
            await self.send_chat(f"@{chatter_name} {text}")

    def get_uptime(self):
        delta = datetime.now() - self.start_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {seconds}s"
