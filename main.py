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
from core.token_manager import TokenManager
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


async def main():
    print("=" * 50)
    print("  HatmasBot v2.0")
    print("  Built by Hatmaster & Claude")
    print("  April 2026")
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

    # Kill/death detector — hooks into smite match state
    kd = KillDeathDetector(debug=True)

    async def on_kill(kill_type):
        web.trigger_kill_event("kill", kill_type)

    async def on_multikill(kill_type):
        web.trigger_kill_event("kill", kill_type)

    async def on_death():
        web.trigger_kill_event("death")

    kd.on_kill = on_kill
    kd.on_multikill = on_multikill
    kd.on_death = on_death
    bot.register_plugin("killdetector", kd)

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

        kd.on_god_identified = _on_god_identified

    # Start kill detector immediately — it scans always and uses
    # _is_gameplay_screen() to filter, so it works in jungle practice
    # and real matches alike without waiting for the tracker.gg API.
    await kd.start_detection()

    # Start web server
    await web.start()

    print("[HatmasBot] Starting...")
    print()

    # --- Graceful shutdown machinery ---
    _shutdown_event = asyncio.Event()

    async def _shutdown():
        """Run full cleanup: plugins → token manager → web server → logs."""
        print("\n[HatmasBot] Shutting down...")
        for name, plugin in bot.plugins.items():
            if hasattr(plugin, "cleanup"):
                try:
                    await plugin.cleanup()
                    print(f"  - {name} cleaned up")
                except Exception as e:
                    print(f"  - {name} cleanup error: {e}")
        await token_mgr.close()
        await web.stop()

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
