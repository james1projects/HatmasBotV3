"""
HatmasBot
==========
Main entry point. Initializes all systems and starts the bot.

Usage: python main.py

Shutdown:  Type "quit" or "exit" in the console, or press Ctrl+C.
           All plugins are cleaned up and logs are flushed before exit.
"""

import asyncio
import signal
import sys
import threading

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from core.bot import HatmasBot
from core.webserver import WebServer
from core.public_webserver import PublicWebServer
from core.token_manager import TokenManager
from core import db as shared_db
from core.config import (
    TWITCH_BOT_TOKEN, TWITCH_BOT_REFRESH_TOKEN,
    TWITCH_BROADCASTER_TOKEN, TWITCH_BROADCASTER_REFRESH_TOKEN,
    TWITCH_BOT_ID, TWITCH_OWNER_ID
)
from plugins.basic import BasicPlugin
from plugins.smite import SmitePlugin
from plugins.songrequest import SongRequestPlugin
# from plugins.snap import SnapPlugin
from plugins.obs import OBSPlugin
from plugins.claude_chat import ClaudeChatPlugin
from plugins.godrequest import GodRequestPlugin
from plugins.gamble import GamblePlugin
from plugins.killdetector import KillDeathDetector
from plugins.voicelines import VoiceLinePlugin
from plugins.deathcounter import DeathCounterPlugin
from plugins.economy import EconomyPlugin
from plugins.youtube_rewards import YouTubeRewardsPlugin
from plugins.stream_status import StreamStatusPlugin
from plugins.youtube_live_badge import YouTubeLiveBadgePlugin
from plugins.backup_manager import BackupManagerPlugin
from plugins.god_pool import GodPoolPlugin
from plugins.priority_request import PriorityRequestPlugin
from plugins.streamloots import StreamlootsPlugin
from plugins.factorio import FactorioPlugin
from plugins.discord_bridge import DiscordBridgePlugin
from plugins.custom_commands import CustomCommandsPlugin


async def main():
    print("=" * 50)
    print("  HatmasBot v2.5")
    print("  Built by Hatmaster & Claude")
    print("  May 2026")
    print("=" * 50)
    print()

    # Initialize token manager (auto-refreshes OAuth tokens)
    token_mgr = TokenManager()

    # Initialize web server
    web = WebServer()

    # Initialize bot with IDs and token manager
    bot = HatmasBot(
        web_server=web,
        bot_id=TWITCH_BOT_ID,
        owner_id=TWITCH_OWNER_ID,
        token_manager=token_mgr,
    )
    web.bot = bot

    # Register plugins (uncomment as you set them up)
    bot.register_plugin("basic", BasicPlugin())
    bot.register_plugin("smite", SmitePlugin(token_manager=token_mgr))
    bot.register_plugin("songrequest", SongRequestPlugin())
    # bot.register_plugin("snap", SnapPlugin())
    bot.register_plugin("obs", OBSPlugin())
    bot.register_plugin("godrequest", GodRequestPlugin())
    bot.register_plugin("claude", ClaudeChatPlugin())
    bot.register_plugin("gamble", GamblePlugin())

    # Death counter — registered before killdetector so the on_death
    # callback can reference it.
    death_counter = DeathCounterPlugin()
    bot.register_plugin("deathcounter", death_counter)

    # Kill/death detector — uses listener registration (add_*_listener).
    # Multiple plugins can subscribe to the same event without
    # monkey-patching each other's callbacks.
    kd = KillDeathDetector(debug=False)

    # Webserver/death-counter listeners — overlay events + daily death tally
    async def _overlay_on_kill(kill_type, count=1):
        web.trigger_kill_event("kill", kill_type)

    async def _overlay_on_multikill(kill_type):
        web.trigger_kill_event("kill", kill_type)

    async def _overlay_on_death(count=1):
        web.trigger_kill_event("death")
        death_counter.increment()

    kd.add_kill_listener(_overlay_on_kill)
    kd.add_multikill_listener(_overlay_on_multikill)
    kd.add_death_listener(_overlay_on_death)
    bot.register_plugin("killdetector", kd)

    # Voice line plugin — channel point redemptions for god voice lines
    vl = VoiceLinePlugin(token_manager=token_mgr)
    bot.register_plugin("voicelines", vl)

    # Hook kill detector into smite match lifecycle
    smite_plugin = bot.plugins.get("smite")
    if smite_plugin:
        async def _kd_match_start(data):
            kd.reset_match_stats()

        async def _kd_match_end(data):
            stats = kd.get_match_stats()
            if stats["kills"] > 0 or stats["deaths"] > 0:
                print(f"[KillDetector] Match stats: {stats['kills']}K / {stats['deaths']}D")

        smite_plugin.on_match_start(_kd_match_start)
        smite_plugin.on_match_end(_kd_match_end)

        # Hook god portrait detection — the kill detector identifies the god
        # from the in-game portrait ~2-5 minutes before tracker.gg API responds.
        async def _on_god_identified(god_name):
            await smite_plugin.set_god_from_portrait(god_name)
            vl.set_current_god(god_name)

        kd.add_god_identified_listener(_on_god_identified)

        # Hook kill detector gameplay-end detection — clears god portrait
        # immediately instead of waiting for tracker.gg API to catch up.
        async def _kd_gameplay_ended():
            await smite_plugin.force_end_match()

        kd.add_gameplay_ended_listener(_kd_gameplay_ended)

        # Also hook into god detection from tracker.gg (backup path)
        async def _on_god_detected_vl(god_info):
            if god_info and god_info.get("name"):
                vl.set_current_god(god_info["name"])

        smite_plugin.on_god_detected(_on_god_detected_vl)

        # Clear voice line god on match end
        async def _vl_match_end(data):
            vl.set_current_god(None)

        smite_plugin.on_match_end(_vl_match_end)

    # ── God Stock Market Economy ──
    economy = EconomyPlugin(token_manager=token_mgr)
    bot.register_plugin("economy", economy)

    if smite_plugin:
        # Visual god-detection signal (portrait matcher OR tracker.gg).
        # Arms the economy's cosmetic state so the live match overlay
        # appears and KDA ticks animate even in jungle practice /
        # custom games. Does NOT pay dividends or settle anything -
        # that's gated separately on tracker.gg confirmation.
        smite_plugin.on_god_detected(economy.on_god_detected_visual)

        # Authoritative match-start signal (tracker.gg-verified, real
        # match_id). The economy fires its 5% start dividend and
        # promotes visual state to authoritative from here. Portrait-only
        # detections never reach this callback.
        smite_plugin.on_match_confirmed(economy.on_match_confirmed)

        # Stop cosmetic ticking on match end
        smite_plugin.on_match_end(economy.on_match_end)

        # Broadcaster resolved the Twitch prediction → economy kicks an
        # immediate backfill cycle to settle the match using tracker.gg's
        # canonical KDA. If tracker.gg already has the listing, settlement
        # fires now; otherwise the 5-min scheduled backfill catches it.
        smite_plugin.on_match_result(economy.on_match_result)

    # Hook economy into kill detector for COSMETIC price ticks. The
    # ticking handlers in plugins/economy/ticking.py never mutate the
    # persisted price — they only animate the overlays. They DO fire
    # in jungle practice (gated on _match_god, set by visual detection),
    # but every tick stays in memory only. The DB never moves until
    # tracker.gg-verified settlement.
    kd.add_kill_listener(economy.on_kill)
    kd.add_death_listener(economy.on_death)
    kd.add_assist_listener(economy.on_assist)

    # Register economy API routes on webserver
    economy.register_api_routes(web.app)

    # ── YouTube Rewards (commenter portfolios) ──
    # Periodic scanner that awards shares to YouTube commenters on the
    # most recent uploads. No-op until YOUTUBE_API_KEY and
    # YOUTUBE_CHANNEL_ID are filled in config_local.py. Registered after
    # economy so the YouTube plugin can rely on the youtube_* tables
    # being created (economy._init_db owns the schema).
    bot.register_plugin("youtube_rewards", YouTubeRewardsPlugin())

    # ── Twitch live-status poller ──
    # Polls /helix/streams every 60s; emits stream_live / stream_offline
    # events that the public webserver caches and serves via
    # /api/stream-status. Drives the Twitch embed on hatmaster.tv —
    # the player iframe shows up only while the stream is actually live.
    stream_status = StreamStatusPlugin(token_manager=token_mgr,
                                        web_server=web)
    bot.register_plugin("stream_status", stream_status)

    # ── YouTube LIVE thumbnail badge automation ──
    # Listens for stream_live / stream_offline events from StreamStatusPlugin.
    # On live: shells out to tools/youtube_live_badge.py apply, which slaps
    # a "LIVE" badge on the last 8 YouTube thumbnails. On offline: revert.
    # Cached originals at data/youtube_thumbnails/<id>.png. State at
    # data/live_badge_state.json. Toggle via dashboard feature: youtube_live_badge.
    bot.register_plugin("youtube_live_badge", YouTubeLiveBadgePlugin())

    # ── Daily economy.db backup ──
    # Periodic gzipped snapshots of economy.db to data/backups/.
    # Auto-rotates so storage stays bounded. Configurable via
    # BACKUP_INTERVAL_HOURS / BACKUP_RETENTION_DAYS in config.
    bot.register_plugin("backup_manager", BackupManagerPlugin())

    # ── Viewer-driven god voting ──
    # Adds !nominate / !pool / !spin / !poolclear chat commands and
    # exposes the current pool via /api/community for the website.
    bot.register_plugin("god_pool", GodPoolPlugin())

    # ── Streamloots event hub ──
    # SSE listener on the Streamloots alert overlay stream (the same
    # unofficial surface MixItUp/Firebot use). Other plugins subscribe
    # via streamloots.add_redemption_listener(cb) (also _purchase_ /
    # _gift_) and receive normalized event dicts — see the docstring
    # in plugins/streamloots.py. No-op until STREAMLOOTS_ALERT_ID is
    # set in config_local.py. Future consumers (e.g. the Factorio
    # plugin) attach their listeners here in main.py.
    streamloots = StreamlootsPlugin()
    bot.register_plugin("streamloots", streamloots)

    # ── Factorio integration ──
    # Bot half of the hatmas-events Factorio mod: RCON commands into
    # the game, outbox tailer out of it (chat announcements for pet
    # deaths, boss kills, ...). Subscribes to Streamloots redemptions
    # below; FACTORIO_CARD_MAP in config maps card names to mod
    # actions. No-op until FACTORIO_RCON_PASSWORD is set in
    # config_local.py. Mod-only test commands: !factorio !fpet !fgrow
    # !fsay !fboss.
    factorio = FactorioPlugin()
    bot.register_plugin("factorio", factorio)
    streamloots.add_redemption_listener(factorio.on_streamloots_redemption)
    # Card manager page + API: /factorio/cards on the dashboard
    # webserver (port 8069). Mappings persist to
    # data/factorio_cards.json; FACTORIO_CARD_MAP only seeds it once.
    factorio.register_api_routes(web.app)

    # ── Discord bridge ──
    # Phase 1 foundation (see Discord_Integration_Plan.md): connects to
    # the Discord gateway, observes messages, exposes send_message()
    # for future consumers. No-op until DISCORD_ENABLED +
    # DISCORD_BOT_TOKEN are set in config_local.py. Mod-only test
    # commands: !discordstatus !discordtest.
    discord_bridge = DiscordBridgePlugin()
    bot.register_plugin("discord", discord_bridge)

    # Phase 2: go-live announcement. The dedupe (max ONE per calendar
    # day, persisted to data/discord_announce.json) lives inside the
    # plugin, so bot restarts and stream restarts never double-announce.
    # Gated by DISCORD_ANNOUNCE_ENABLED in config_local.py.
    stream_status.add_live_listener(discord_bridge.on_stream_live)

    # Mod-created text commands (managed from /mod). Registered LAST
    # so built-in command names always win collisions.
    custom_commands = CustomCommandsPlugin()
    bot.register_plugin("custom_commands", custom_commands)

    # ── Priority god request (Stripe) ──
    # Hosts the $5-skip-the-line flow on hatmaster.tv/community.
    # Registered AFTER godrequest so PriorityRequestPlugin can look
    # it up in bot.plugins. The plugin instance is passed to
    # PublicWebServer below so the new /api/priority-request/create
    # and /api/stripe-webhook routes can reach it.
    priority_request = PriorityRequestPlugin()
    bot.register_plugin("priority_request", priority_request)

    # ── Open the shared aiosqlite connection ──
    # Plugin setup() calls (above) queued their schema callbacks via
    # core.db.register_schema(). init_db() now opens the connection
    # to data/economy.db, sets PRAGMAs (WAL, foreign_keys), and runs
    # those callbacks in registration order. After this returns,
    # every consumer (economy, god_pool, youtube_rewards, public
    # webserver, dashboard webserver) shares one aiosqlite.Connection
    # — no more per-plugin connection management.
    #
    # Returns None if aiosqlite isn't installed; in that case
    # economy/god_pool/etc. on_ready will print "DB unavailable" and
    # gracefully degrade.
    await shared_db.init_db()

    # Start kill detector immediately — it scans always and uses
    # _is_gameplay_screen() to filter, so it works in jungle practice
    # and real matches alike without waiting for the tracker.gg API.
    await kd.start_detection()

    # Start web server
    await web.start()

    # ── Public read-only web server (port 8070) ──
    # Serves the YouTube portfolio page at hatmaster.tv via a Cloudflare
    # Tunnel. Bound to 127.0.0.1 only — Cloudflare reaches it through
    # cloudflared running locally; nothing on this port is exposed
    # directly to the internet. Subscribes to web.overlay so live
    # price ticks reach portfolio page WebSocket clients.
    public_web = PublicWebServer(overlay_manager=web.overlay,
                                  stream_status=stream_status,
                                  priority_request=priority_request,
                                  economy=economy,
                                  bot=bot)  # /mod command matrix
    await public_web.start()

    print("[HatmasBot] Starting...")
    print()

    # --- Graceful shutdown machinery ---
    _shutdown_event = asyncio.Event()

    async def _shutdown():
        """Run full cleanup: plugins → token manager → web server →
        shared DB → logs.

        Order matters: every plugin.cleanup() must run BEFORE we close
        the shared aiosqlite connection. Plugins clear their self._db
        references during cleanup but don't close the connection
        themselves (that's our job here). Likewise the public webserver
        stops accepting requests before we close its DB handle.
        """
        print("\n[HatmasBot] Shutting down...")
        for name, plugin in bot.plugins.items():
            if hasattr(plugin, "cleanup"):
                try:
                    await plugin.cleanup()
                    print(f"  - {name} cleaned up")
                except Exception as e:
                    print(f"  - {name} cleanup error: {e}")
        await token_mgr.close()
        await public_web.stop()
        await web.stop()
        # Close the shared DB last — every plugin and the webserver
        # have now released their references to it.
        await shared_db.close_db()

        # Flush all log handlers so nothing is truncated
        import logging
        for handler in logging.getLogger("KillDetector").handlers:
            handler.flush()
        logging.shutdown()

        print("[HatmasBot] Goodbye.")

    # Console input listener — runs in a background thread so it
    # doesn't block the asyncio loop.  Typing "quit" or "exit"
    # triggers the same graceful shutdown as Ctrl+C.
    def _console_listener():
        while not _shutdown_event.is_set():
            try:
                line = input()
            except EOFError:
                break
            cmd = line.strip().lower()
            if cmd in ("quit", "exit", "stop", "close"):
                print("[HatmasBot] Shutdown requested from console")
                _shutdown_event.set()
                break

    console_thread = threading.Thread(target=_console_listener, daemon=True)
    console_thread.start()

    # Handle Ctrl+C via the event loop (works reliably on Windows)
    def _signal_handler():
        _shutdown_event.set()

    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _signal_handler)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler — fall back to
        # threading-based signal handler
        signal.signal(signal.SIGINT, lambda s, f: _shutdown_event.set())

    try:
        # Start token manager (validates and refreshes tokens on startup)
        await token_mgr.start()

        # Add the bot's OAuth token before starting
        # (uses the potentially-refreshed token from config module)
        from core import config as _cfg
        await bot.add_token(_cfg.TWITCH_BOT_TOKEN, _cfg.TWITCH_BOT_REFRESH_TOKEN)
        print("[HatmasBot] Bot token added")

        # Add the broadcaster's OAuth token for channel-level EventSub
        # (subscribe, resub, gift sub events require broadcaster auth)
        await bot.add_token(_cfg.TWITCH_BROADCASTER_TOKEN, _cfg.TWITCH_BROADCASTER_REFRESH_TOKEN)
        print("[HatmasBot] Broadcaster token added")

        print()
        print("Type 'quit' or 'exit' to shut down gracefully.")
        print()

        # Run the bot and the shutdown watcher concurrently.
        # When _shutdown_event fires (from console, Ctrl+C, or signal),
        # the watcher task completes and we cancel the bot.
        bot_task = asyncio.create_task(bot.start())
        shutdown_watcher = asyncio.create_task(_shutdown_event.wait())

        done, pending = await asyncio.wait(
            [bot_task, shutdown_watcher],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel whichever is still running
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    except Exception as e:
        print(f"\n[HatmasBot] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await _shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Last-resort catch for Ctrl+C during asyncio.run() teardown
        print("\n[HatmasBot] Force quit.")
