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
    DEFAULT_FEATURES, SHOUTOUT_ENABLED, SHOUTOUT_MIN_VIEWERS,
    SHOUTOUT_COOLDOWN, TWITCH_OWNER_ID, TTS_MAX_LENGTH,
)


class _WhisperChatterAdapter:
    """Mimics the ChatMessage.chatter interface using a Whisper's sender PartialUser."""

    def __init__(self, sender):
        self._user = sender
        self.name = sender.name
        self.display_name = getattr(sender, "display_name", None) or sender.name
        # Whisper users don't have badge info — safe defaults
        self.moderator = False
        self.broadcaster = False
        self.subscriber = False
        self.badges = []

    async def send_whisper(self, *, to_user, message):
        """Send a whisper back to this user."""
        await self._user.send_whisper(to_user=to_user, message=message)

    def __getattr__(self, item):
        # Forward anything else (like .id) to the underlying PartialUser
        return getattr(self._user, item)


class _WhisperMessageAdapter:
    """Wraps a Whisper payload to look like a ChatMessage for command handlers."""

    def __init__(self, original, sender, text):
        self._original = original
        self.chatter = _WhisperChatterAdapter(sender)
        self.text = text

    async def respond(self, text):
        """Whispers can't be replied to inline — no-op (send_reply handles it)."""
        pass

    def __getattr__(self, item):
        return getattr(self._original, item)


class HatmasBot(commands.Bot):
    def __init__(self, web_server=None, bot_id=None, owner_id=None, token_manager=None):
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
        self.token_manager = token_manager
        self.start_time = datetime.now()
        self.command_count = 0
        self._custom_commands = {}
        self._mention_handlers = []
        self._raw_handlers = []
        self._owner_id = owner_id
        self._bot_id = bot_id
        self._shoutout_cooldowns = {}  # raider_id -> last shoutout timestamp

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
                whisper_payload = eventsub.WhisperReceivedSubscription(
                    user_id=self._bot_id,
                )
                await self.subscribe_websocket(payload=whisper_payload)
                print(f"[HatmasBot] Subscribed to whisper events")
            except Exception as e:
                print(f"[HatmasBot] Whisper subscription failed (non-critical): {e}")

            # Subscribe to channel events for god token auto-awards
            try:
                sub_payload = eventsub.ChannelSubscribeSubscription(
                    broadcaster_user_id=self._owner_id,
                )
                await self.subscribe_websocket(payload=sub_payload)
                print(f"[HatmasBot] Subscribed to channel subscribe events")
            except Exception as e:
                print(f"[HatmasBot] Subscribe event subscription failed: {e}")

            try:
                resub_payload = eventsub.ChannelSubscribeMessageSubscription(
                    broadcaster_user_id=self._owner_id,
                )
                await self.subscribe_websocket(payload=resub_payload)
                print(f"[HatmasBot] Subscribed to channel resub message events")
            except Exception as e:
                print(f"[HatmasBot] Resub message subscription failed: {e}")

            try:
                gift_payload = eventsub.ChannelSubscriptionGiftSubscription(
                    broadcaster_user_id=self._owner_id,
                )
                await self.subscribe_websocket(payload=gift_payload)
                print(f"[HatmasBot] Subscribed to channel gift sub events")
            except Exception as e:
                print(f"[HatmasBot] Gift sub subscription failed: {e}")

            # Subscribe to incoming raids for auto-shoutout
            try:
                raid_payload = eventsub.ChannelRaidSubscription(
                    to_broadcaster_user_id=self._owner_id,
                )
                await self.subscribe_websocket(payload=raid_payload)
                print(f"[HatmasBot] Subscribed to channel raid events")
            except Exception as e:
                print(f"[HatmasBot] Raid subscription failed: {e}")

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

        # Check for highlighted message (channel points) → TTS
        try:
            msg_type = getattr(payload, "type", None)
            if msg_type == "channel_points_highlighted" and self.is_feature_enabled("tts_highlights"):
                display_name = chatter.display_name if hasattr(chatter, "display_name") else chatter.name
                tts_text = content
                if len(tts_text) > TTS_MAX_LENGTH:
                    tts_text = tts_text[:TTS_MAX_LENGTH] + "..."
                if tts_text and self.web_server:
                    print(f"[TTS] Highlighted message from {display_name}: {tts_text[:80]}...")
                    self.web_server.trigger_tts(display_name, tts_text)
        except Exception as e:
            print(f"[TTS] Error checking highlighted message: {e}")

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
                return  # Custom command handled — skip TwitchIO's process_commands

        # Only reach here for non-custom commands (e.g. TwitchIO @commands.command() decorated methods)
        await self.process_commands(payload)

    async def event_message_whisper(self, payload):
        """Handle incoming whisper messages (user.whisper.message EventSub).

        The Whisper payload has a different shape than ChatMessage (no .chatter,
        no .text at top level, etc.), so we wrap it in a lightweight adapter
        that gives command handlers the same interface they expect.
        """
        try:
            # Whisper payload attributes:
            #   .sender    — PartialUser who sent the whisper
            #   .recipient — PartialUser who received it (the bot)
            #   .text      — the whisper message text
            text = (payload.text or "").strip()
            sender = payload.sender
            sender_name = sender.name if sender else "unknown"

            print(f"[Whisper] From {sender_name}: {text}")

            if not text:
                return

            # Build a lightweight wrapper so command handlers can use
            # message.chatter.name / message.chatter.display_name / message.text
            # just like they do for normal chat messages.
            wrapped = _WhisperMessageAdapter(payload, sender, text)

            # Check for @HatmasBot mentions (route to mention handlers)
            if TWITCH_BOT_USERNAME.lower() in text.lower():
                for handler in self._mention_handlers:
                    try:
                        await handler(wrapped, whisper=True)
                    except Exception as e:
                        print(f"[Whisper Mention Error] {e}")
                        traceback.print_exc()
                return

            if not text.startswith("!"):
                return

            self.command_count += 1
            parts = text[1:].split(maxsplit=1)
            cmd_name = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            if cmd_name in self._custom_commands:
                cmd = self._custom_commands[cmd_name]
                try:
                    await cmd["handler"](wrapped, args, whisper=True)
                except Exception as e:
                    print(f"[Whisper Command Error] !{cmd_name}: {e}")
                    traceback.print_exc()
        except Exception as e:
            print(f"[Whisper Event Error] {e}")
            traceback.print_exc()
            print(f"[Whisper Debug] Payload type: {type(payload)}")
            print(f"[Whisper Debug] Payload attrs: {[a for a in dir(payload) if not a.startswith('_')]}")

    # =============================================================
    # SUBSCRIPTION / DONATION EVENT HANDLERS
    # =============================================================

    async def _award_god_tokens_for_sub(self, username):
        """Award God Tokens when someone subscribes (new, resub, or gifted)."""
        if "godrequest" in self.plugins and self.is_feature_enabled("god_requests"):
            try:
                await self.plugins["godrequest"].award_sub_tokens(username)
            except Exception as e:
                print(f"[HatmasBot] God token award failed for {username}: {e}")

    async def event_subscribe(self, payload):
        """Fired on new subscriptions (channel.subscribe)."""
        try:
            user = payload.user
            username = user.name if hasattr(user, "name") else str(user)
            print(f"[HatmasBot] New subscriber: {username}")
            await self._award_god_tokens_for_sub(username)
        except Exception as e:
            print(f"[HatmasBot] event_subscribe error: {e}")

    async def event_subscription_message(self, payload):
        """Fired on resub messages (channel.subscription.message)."""
        try:
            user = payload.user
            username = user.name if hasattr(user, "name") else str(user)
            print(f"[HatmasBot] Resub message from: {username}")
            await self._award_god_tokens_for_sub(username)
        except Exception as e:
            print(f"[HatmasBot] event_subscription_message error: {e}")

    async def event_subscription_gift(self, payload):
        """Fired on gift subs (channel.subscription.gift) — award to the gifter."""
        try:
            user = payload.user
            username = user.name if hasattr(user, "name") else str(user)
            total = getattr(payload, "total", 1)
            print(f"[HatmasBot] Gift sub from {username} (x{total})")
            # Award tokens for each gifted sub
            if "godrequest" in self.plugins and self.is_feature_enabled("god_requests"):
                from core.config import GODREQ_SUB_TOKENS
                try:
                    await self.plugins["godrequest"]._award_token(username, GODREQ_SUB_TOKENS * total)
                    await self.send_chat(
                        f"{username} earned {GODREQ_SUB_TOKENS * total} God Token(s) "
                        f"for gifting {total} sub(s)! Use !godrequest <god>."
                    )
                except Exception as e:
                    print(f"[HatmasBot] Gift sub token award failed: {e}")
        except Exception as e:
            print(f"[HatmasBot] event_subscription_gift error: {e}")

    # =============================================================
    # RAID EVENT HANDLER — AUTO-SHOUTOUT
    # =============================================================

    async def event_channel_raid(self, payload):
        """Fired when someone raids the channel. Sends a chat shoutout and
        triggers the official Twitch /shoutout API."""
        try:
            raider = payload.from_broadcaster  # PartialUser
            viewer_count = payload.viewer_count or 0
            raider_name = raider.name if hasattr(raider, "name") else str(raider)
            raider_id = str(raider.id) if hasattr(raider, "id") else None

            print(f"[HatmasBot] Raid from {raider_name} with {viewer_count} viewers!")

            if not self.is_feature_enabled("auto_shoutout"):
                return

            if viewer_count < SHOUTOUT_MIN_VIEWERS:
                print(f"[HatmasBot] Raid below minimum viewers ({SHOUTOUT_MIN_VIEWERS}), skipping shoutout")
                return

            # Cooldown check — avoid spamming shoutouts to the same raider
            import time
            now = time.time()
            if raider_id:
                last_shoutout = self._shoutout_cooldowns.get(raider_id, 0)
                if now - last_shoutout < SHOUTOUT_COOLDOWN:
                    print(f"[HatmasBot] Shoutout cooldown active for {raider_name}")
                    return
                self._shoutout_cooldowns[raider_id] = now

            # Fetch the raider's last game via Twitch API
            last_game = await self._fetch_raider_game(raider_id)
            game_text = f" They were last playing {last_game}." if last_game else ""

            # Send chat message
            await self.send_chat(
                f"Welcome raiders! Shoutout to @{raider_name} "
                f"bringing {viewer_count} viewers!{game_text} "
                f"check them out at twitch.tv/{raider_name} hatmasLove"
            )

            # Trigger the official Twitch shoutout API
            await self._send_official_shoutout(raider_id)

        except Exception as e:
            print(f"[HatmasBot] event_channel_raid error: {e}")
            traceback.print_exc()

    async def _fetch_raider_game(self, raider_id):
        """Fetch what game a raider was last playing via Twitch API."""
        if not raider_id or not self.token_manager:
            return None

        try:
            import aiohttp
            headers = await self.token_manager.get_broadcaster_headers()
            url = f"https://api.twitch.tv/helix/channels?broadcaster_id={raider_id}"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        channels = data.get("data", [])
                        if channels:
                            return channels[0].get("game_name") or None
                    elif resp.status == 401 and self.token_manager:
                        # Try refreshing and retrying once
                        refreshed = await self.token_manager.handle_401("broadcaster")
                        if refreshed:
                            headers = await self.token_manager.get_broadcaster_headers()
                            async with session.get(url, headers=headers) as retry_resp:
                                if retry_resp.status == 200:
                                    data = await retry_resp.json()
                                    channels = data.get("data", [])
                                    if channels:
                                        return channels[0].get("game_name") or None
        except Exception as e:
            print(f"[HatmasBot] Failed to fetch raider game: {e}")

        return None

    async def _send_official_shoutout(self, raider_id):
        """Call the Twitch /shoutout API to display the official shoutout card."""
        if not raider_id or not self.token_manager or not self._owner_id:
            return

        try:
            import aiohttp
            headers = await self.token_manager.get_broadcaster_headers()
            url = (
                f"https://api.twitch.tv/helix/chat/shoutouts"
                f"?from_broadcaster_id={self._owner_id}"
                f"&to_broadcaster_id={raider_id}"
                f"&moderator_id={self._owner_id}"
            )

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers) as resp:
                    if resp.status == 204:
                        print(f"[HatmasBot] Official shoutout sent for user {raider_id}")
                    elif resp.status == 401:
                        refreshed = await self.token_manager.handle_401("broadcaster")
                        if refreshed:
                            headers = await self.token_manager.get_broadcaster_headers()
                            async with session.post(url, headers=headers) as retry_resp:
                                if retry_resp.status == 204:
                                    print(f"[HatmasBot] Official shoutout sent (after refresh)")
                                else:
                                    body = await retry_resp.text()
                                    print(f"[HatmasBot] Shoutout retry failed: {retry_resp.status} {body}")
                    elif resp.status == 429:
                        print(f"[HatmasBot] Shoutout rate-limited (Twitch allows 1 per 2 min)")
                    else:
                        body = await resp.text()
                        print(f"[HatmasBot] Shoutout API error: {resp.status} {body}")
        except Exception as e:
            print(f"[HatmasBot] Official shoutout failed: {e}")

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
                bot_user = self.create_partialuser(int(self._bot_id))
                # TwitchIO v3: send_whisper(*, to_user, message) — both keyword-only
                target = chatter._user if hasattr(chatter, "_user") else chatter
                await bot_user.send_whisper(to_user=target, message=text)
                return
            except Exception as e:
                print(f"[Whisper Reply] Failed to whisper {chatter_name}: {e}")
                traceback.print_exc()
                # Fall through to chat reply

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
