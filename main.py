"""
HatmasBot
==========
Main entry point. Initializes all systems and starts the bot.

Usage: python main.py
"""

import asyncio
import sys

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
            await kd.start_detection()

        async def _kd_match_end(data):
            await kd.stop_detection()
            stats = kd.get_match_stats()
            if stats["kills"] > 0 or stats["deaths"] > 0:
                print(f"[KillDetector] Match stats: {stats['kills']}K / {stats['deaths']}D")

        smite_plugin.on_match_start(_kd_match_start)
        smite_plugin.on_match_end(_kd_match_end)

    # Start web server
    await web.start()

    print("[HatmasBot] Starting...")
    print()

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

        # Start the bot (this blocks)
        await bot.start()
    except KeyboardInterrupt:
        print("\n[HatmasBot] Shutting down...")
    except Exception as e:
        print(f"\n[HatmasBot] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        for name, plugin in bot.plugins.items():
            if hasattr(plugin, "cleanup"):
                await plugin.cleanup()
        await token_mgr.close()
        await web.stop()
        print("[HatmasBot] Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
