"""
Discord Bridge Plugin
======================
Phase 1 foundation + Phase 2 go-live announcements for HatmasBot's
Discord presence (see Discord_Integration_Plan.md).

What it does:
  - Connects to the Discord gateway using discord.py.
  - Observes every message the bot can see and dispatches to
    registered listeners (same add_*_listener pattern as killdetector).
  - Exposes send_message() for other plugins to post to Discord.
  - Go-live announcements via stream_status, max ONE per calendar day
    (persisted), so bot/stream restarts never double-announce.
  - Twitch mod commands !discordstatus / !discordtest for verification.

NOTE: this file must never be renamed to discord.py, it would shadow
the discord.py library import.
"""

import asyncio
import json
from datetime import datetime

try:
    import discord
except ImportError:
    discord = None

from core import config

READY_TIMEOUT = 15  # seconds send_message() waits for the gateway
ANNOUNCE_STATE_FILE = config.DATA_DIR / "discord_announce.json"


class _DiscordChatterAdapter:
    """Mimics ChatMessage.chatter for Discord interactions, same trick
    as _WhisperChatterAdapter in core/bot.py."""

    def __init__(self, user):
        self._user = user
        self.name = user.name
        self.display_name = getattr(user, "display_name", None) or user.name
        self.id = str(user.id)
        # Discord users are never Twitch mods/subs (until Phase 5 linking)
        self.moderator = False
        self.broadcaster = False
        self.subscriber = False
        self.badges = []


class _DiscordInteractionAdapter:
    """Wraps a slash-command Interaction to look like a ChatMessage.

    Handlers only touch .chatter and .respond() (via bot.send_reply),
    so this is the entire surface we need. The interaction is ALWAYS
    deferred before handlers run, so respond() maps to followup.send().
    """

    platform = "discord"

    def __init__(self, interaction, cmd_name, args):
        self._interaction = interaction
        self.chatter = _DiscordChatterAdapter(interaction.user)
        self.text = f"!{cmd_name} {args}".strip()

    async def respond(self, text):
        await self._interaction.followup.send(text)


class DiscordBridgePlugin:
    def __init__(self, debug=False):
        self.bot = None          # HatmasBot, set in setup()
        self.client = None       # discord.Client
        self.debug = debug

        self.enabled = bool(getattr(config, "DISCORD_ENABLED", False))
        self._token = getattr(config, "DISCORD_BOT_TOKEN", "") or ""
        self.guild_id = int(getattr(config, "DISCORD_GUILD_ID", 0) or 0)
        self.default_channel_id = int(
            getattr(config, "DISCORD_DEFAULT_CHANNEL_ID", 0) or 0
        )

        self._task = None                  # asyncio task running client.start()
        self._ready = asyncio.Event()
        self._message_listeners = []       # async callables (discord.Message)

        # -- Slash commands (Phase 4) --
        self.tree = None                   # discord.app_commands.CommandTree
        self._synced_signature = None      # hash of last-synced command set
        self._sync_debounce_task = None

        # -- Go-live announcements (Phase 2) --
        self.announce_enabled = bool(
            getattr(config, "DISCORD_ANNOUNCE_ENABLED", False)
        )
        self.announce_channel_id = int(
            getattr(config, "DISCORD_ANNOUNCE_CHANNEL_ID", 0) or 0
        ) or self.default_channel_id
        self.announce_role_id = int(
            getattr(config, "DISCORD_ANNOUNCE_ROLE_ID", 0) or 0
        )
        self._last_announce_date = self._load_announce_state()

    # === Plugin lifecycle ===

    def setup(self, bot):
        self.bot = bot
        bot.register_command("discordstatus", self.cmd_status,
                             mod_only=True, description="Discord connection state and latency", plugin="discord")
        bot.register_command("discordtest", self.cmd_test,
                             mod_only=True,
                             description="Send a test message to Discord (optional leading channel id targets that channel)",
                             plugin="discord")

        if not self.enabled:
            print("[Discord] Disabled (DISCORD_ENABLED is False), skipping connect")
            return
        if discord is None:
            print("[Discord] discord.py not installed, run: pip install -r requirements.txt")
            return
        if not self._token:
            print("[Discord] No DISCORD_BOT_TOKEN in config_local.py, skipping connect")
            return

        intents = discord.Intents.default()
        intents.message_content = True  # must ALSO be toggled in the dev portal

        self.client = discord.Client(intents=intents)
        self.tree = discord.app_commands.CommandTree(self.client)
        # re-sync slash commands when the /mod page flips a toggle
        bot.add_catalog_listener(self._schedule_tree_sync)

        # discord.py registers events by function NAME, but HatmasBot's
        # core also calls plugin.on_ready() as a Twitch-side lifecycle
        # hook, so the gateway handlers live on _on_discord_* methods
        # and these thin wrappers carry the names discord.py expects.
        @self.client.event
        async def on_ready():
            await self._on_discord_ready()

        @self.client.event
        async def on_disconnect():
            self._ready.clear()

        @self.client.event
        async def on_resumed():
            self._ready.set()

        @self.client.event
        async def on_message(message):
            await self._on_discord_message(message)

        # register_plugin() is called from async main(), so the loop is
        # already running, safe to spawn the gateway task here.
        self._task = asyncio.create_task(self._run(), name="discord-bridge")

    async def _run(self):
        try:
            await self.client.start(self._token)
        except discord.LoginFailure:
            print("[Discord] Login failed, bad token? Regenerate it in the dev portal.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Discord] Gateway error: {e}")

    async def cleanup(self):
        self._ready.clear()
        if self._sync_debounce_task is not None:
            self._sync_debounce_task.cancel()
        if self.client is not None and not self.client.is_closed():
            await self.client.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # === Discord events ===
    # NOTE: deliberately NOT named on_ready/on_message, those names
    # collide with HatmasBot's plugin lifecycle hooks (core/bot.py
    # calls plugin.on_ready() after the Twitch bot connects).

    async def _on_discord_ready(self):
        guild = self.client.get_guild(self.guild_id) if self.guild_id else None
        guild_desc = guild.name if guild else f"{len(self.client.guilds)} guild(s)"
        print(f"[Discord] Connected as {self.client.user} -> {guild_desc}")
        if self.guild_id and guild is None:
            print(f"[Discord] WARNING: not in guild {self.guild_id}, check the invite")
        if self.default_channel_id and self.client.get_channel(self.default_channel_id) is None:
            print(f"[Discord] WARNING: default channel {self.default_channel_id} not visible")
        self._ready.set()
        await self._sync_slash_commands()

    async def _on_discord_message(self, message):
        if message.author == self.client.user:
            return
        if self.debug:
            ch = getattr(message.channel, "name", "DM")
            print(f"[Discord] #{ch} <{message.author.display_name}> "
                  f"{message.content[:80]}")
        for listener in self._message_listeners:
            try:
                await listener(message)
            except Exception as e:
                print(f"[Discord] Message listener error: {e}")

    # === Public API (for other plugins) ===

    @property
    def is_ready(self):
        return self.client is not None and self._ready.is_set()

    def add_message_listener(self, coro):
        """Register an async callable(message: discord.Message)."""
        self._message_listeners.append(coro)

    async def send_message(self, text, channel_id=None, embed=None):
        """Send to a Discord channel. Defaults to DISCORD_DEFAULT_CHANNEL_ID.

        Returns the discord.Message on success, None on failure.
        """
        if self.client is None:
            print("[Discord] send_message called but bridge is not enabled")
            return None
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=READY_TIMEOUT)
        except asyncio.TimeoutError:
            print("[Discord] send_message timed out waiting for gateway")
            return None

        cid = int(channel_id or self.default_channel_id or 0)
        if not cid:
            print("[Discord] send_message: no channel_id and no default configured")
            return None
        channel = self.client.get_channel(cid)
        if channel is None:
            print(f"[Discord] send_message: channel {cid} not found/visible")
            return None
        try:
            return await channel.send(content=text, embed=embed)
        except Exception as e:
            print(f"[Discord] send_message failed: {e}")
            return None

    # === Slash commands (Phase 4) ===
    # Auto-generated from bot.get_command_catalog(), one slash command
    # per discord-enabled registry entry, guild-scoped (instant sync).
    # See Crossplatform_Commands_Plan.md.

    def _make_slash_callback(self, cmd_name):
        async def callback(interaction: discord.Interaction, args: str = ""):
            await self._run_slash(cmd_name, interaction, args or "")
        callback.__name__ = cmd_name
        return callback

    def _discord_command_set(self):
        """(name, description) pairs that should exist as slash commands."""
        out = []
        for entry in self.bot.get_command_catalog():
            if not entry["discord_enabled"]:
                continue
            name = entry["name"]
            # Discord slash name rules: 1-32 chars, lowercase a-z0-9-_
            if not (1 <= len(name) <= 32) or not name.replace("-", "").replace("_", "").isalnum():
                print(f"[Discord] Skipping /{name}, invalid slash name")
                continue
            desc = (entry["description"] or f"HatmasBot !{name}")[:100]
            out.append((name, desc))
        return sorted(out)

    async def _sync_slash_commands(self):
        if self.tree is None or self.bot is None:
            return
        if not self.guild_id:
            print("[Discord] No DISCORD_GUILD_ID, slash commands disabled")
            return
        commands = self._discord_command_set()
        signature = hash(tuple(commands))
        if signature == self._synced_signature:
            return  # nothing changed, don't burn a sync against rate limits

        guild = discord.Object(id=self.guild_id)
        self.tree.clear_commands(guild=guild)
        for name, desc in commands:
            cmd = discord.app_commands.Command(
                name=name,
                description=desc,
                callback=self._make_slash_callback(name),
            )
            self.tree.add_command(cmd, guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            self._synced_signature = signature
            print(f"[Discord] Synced {len(synced)} slash commands to guild")
        except Exception as e:
            print(f"[Discord] Slash sync failed: {e}")

    async def _schedule_tree_sync(self):
        """Catalog-changed listener, debounce 5s so a burst of mod-page
        toggles becomes one sync (Discord limits registration updates)."""
        if self._sync_debounce_task is not None:
            self._sync_debounce_task.cancel()

        async def _later():
            try:
                await asyncio.sleep(5)
                await self._sync_slash_commands()
            except asyncio.CancelledError:
                pass

        self._sync_debounce_task = asyncio.create_task(_later())

    async def _run_slash(self, cmd_name, interaction, args):
        cmd = self.bot._custom_commands.get(cmd_name)
        if cmd is None or not self.bot.is_command_enabled(cmd_name, "discord"):
            await interaction.response.send_message(
                "That command isn't available right now.", ephemeral=True)
            return

        # per-user cooldown from the registry (set on /mod, 0 = off)
        remaining = self.bot.check_cooldown(cmd_name, f"discord:{interaction.user.id}")
        if remaining > 0:
            await interaction.response.send_message(
                f"Command cooldown. Try again in {int(remaining) + 1}s.",
                ephemeral=True)
            return

        # Discord voids interactions not acknowledged within 3s and some
        # handlers hit tracker.gg/DB, so ALWAYS defer, reply via followup.
        await interaction.response.defer()
        adapter = _DiscordInteractionAdapter(interaction, cmd_name, args)
        self.bot.command_count += 1
        try:
            await cmd["handler"](adapter, args)
        except Exception as e:
            print(f"[Discord] /{cmd_name} failed: {e}")
            import traceback
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    "Command failed.")
            except Exception:
                pass

    # === Go-live announcement (Phase 2) ===
    #
    # Wired in main.py via stream_status.add_live_listener(). Hard rule:
    # MAXIMUM ONE announcement per calendar day (local time), persisted
    # to data/discord_announce.json so neither a bot restart nor a
    # stream restart re-announces. Any second announcement on the same
    # day is done manually by the broadcaster, never by the bot.

    def _load_announce_state(self):
        try:
            if ANNOUNCE_STATE_FILE.exists():
                with open(ANNOUNCE_STATE_FILE) as f:
                    return json.load(f).get("last_announce_date")
        except Exception as e:
            print(f"[Discord] Failed to load announce state: {e}")
        return None

    def _save_announce_state(self):
        try:
            with open(ANNOUNCE_STATE_FILE, "w") as f:
                json.dump({
                    "last_announce_date": self._last_announce_date,
                    "saved_at": datetime.now().isoformat(timespec="seconds"),
                }, f, indent=2)
        except Exception as e:
            print(f"[Discord] Failed to save announce state: {e}")

    async def on_stream_live(self, info):
        """stream_status live listener, announce, at most once per day."""
        if not self.announce_enabled:
            return
        if self.client is None:
            return

        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_announce_date == today:
            print(f"[Discord] Go-live suppressed, already announced today "
                  f"({today}). Manual announcements only from here.")
            return

        title = info.get("title") or "Live now!"
        game = info.get("game") or ""
        url = f"https://twitch.tv/{info.get('channel') or config.TWITCH_CHANNEL}"

        ping = f"<@&{self.announce_role_id}> " if self.announce_role_id else ""
        text = f"{ping}Hatmaster is LIVE: {title}"

        embed = None
        if discord is not None:
            embed = discord.Embed(
                title=title,
                url=url,
                description=f"Playing **{game}**" if game else None,
                color=0x9146FF,  # Twitch purple
            )
            thumb = info.get("thumbnail_url")
            if thumb:
                # Helix returns {width}x{height} placeholders; the ts
                # query param busts Discord's image cache.
                thumb = (thumb.replace("{width}", "1280")
                              .replace("{height}", "720"))
                embed.set_image(url=f"{thumb}?ts={int(datetime.now().timestamp())}")
            embed.set_footer(text="twitch.tv/" + (info.get("channel") or ""))

        sent = await self.send_message(
            text, channel_id=self.announce_channel_id, embed=embed
        )
        if sent:
            # Only mark the day used on a SUCCESSFUL send, a failed
            # send (gateway down, bad channel) may retry on the next
            # live transition.
            self._last_announce_date = today
            self._save_announce_state()
            print(f"[Discord] Go-live announced for {today}")
        else:
            print("[Discord] Go-live announcement failed, day not marked, "
                  "will retry on next live transition")

    # === Twitch commands (verification) ===

    async def cmd_status(self, message, args, whisper=False):
        """!discordstatus - Connection state, guild, latency. (mods)"""
        if not self.enabled:
            await self.bot.send_reply(message, "Discord bridge is disabled.", whisper)
            return
        if not self.is_ready:
            await self.bot.send_reply(message, "Discord: not connected.", whisper)
            return
        guild = self.client.get_guild(self.guild_id) if self.guild_id else None
        latency_ms = round(self.client.latency * 1000)
        await self.bot.send_reply(
            message,
            f"Discord: connected as {self.client.user.name} | "
            f"guild: {guild.name if guild else 'unknown'} | "
            f"latency: {latency_ms}ms",
            whisper,
        )

    @staticmethod
    def _parse_test_args(args):
        """Split !discordtest args into (channel_id|None, message_text).

        An optional leading channel reference -- a bare snowflake id or a
        <#id> mention -- targets that channel; everything else is the
        message. With no leading id, all of args is the message and the
        default channel is used."""
        args = (args or "").strip()
        if not args:
            return None, ""
        first, _, rest = args.partition(" ")
        token = first.strip()
        if token.startswith("<#") and token.endswith(">"):
            token = token[2:-1]
        token = token.lstrip("#")
        # Discord snowflakes are 17-19 digits; the length guard keeps a
        # short numeric word (e.g. "5 hats") from being read as a channel.
        if token.isdigit() and len(token) >= 15:
            return int(token), rest.strip()
        return None, args

    async def cmd_test(self, message, args, whisper=False):
        """!discordtest [channel_id] [text] - Send a test message. With a
        leading channel id (or <#mention>) it targets that channel, else
        the default channel. (mods)"""
        channel_id, text = self._parse_test_args(args)
        text = text or "Test message from HatmasBot (Twitch side)."
        sent = await self.send_message(text, channel_id=channel_id)
        if sent:
            where = f" channel {channel_id}" if channel_id else " default channel"
            await self.bot.send_reply(message, f"Sent to Discord ({where.strip()}).", whisper)
        else:
            await self.bot.send_reply(
                message, "Failed to send. Check the console.", whisper
            )
