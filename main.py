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
from core.config import (
    TWITCH_BOT_TOKEN, TWITCH_BOT_REFRESH_TOKEN,
    TWITCH_BOT_ID, TWITCH_OWNER_ID
)
from plugins.basic import BasicPlugin
# from plugins.smite import SmitePlugin
from plugins.songrequest import SongRequestPlugin
# from plugins.snap import SnapPlugin
# from plugins.obs import OBSPlugin
# from plugins.claude_chat import ClaudeChatPlugin


async def main():
    print("=" * 50)
    print("  HatmasBot v2.0")
    print("  Built by Hatmaster & Claude")
    print("  April 2026")
    print("=" * 50)
    print()

    # Initialize web server
    web = WebServer()

    # Initialize bot with IDs
    bot = HatmasBot(
        web_server=web,
        bot_id=TWITCH_BOT_ID,
        owner_id=TWITCH_OWNER_ID,
    )
    web.bot = bot

    # Register plugins (uncomment as you set them up)
    bot.register_plugin("basic", BasicPlugin())
    # bot.register_plugin("smite", SmitePlugin())
    bot.register_plugin("songrequest", SongRequestPlugin())
    # bot.register_plugin("snap", SnapPlugin())
    # bot.register_plugin("obs", OBSPlugin())
    # bot.register_plugin("claude", ClaudeChatPlugin())

    # Start web server
    await web.start()

    print("[HatmasBot] Starting...")
    print()

    try:
        # Add the bot's OAuth token before starting
        await bot.add_token(TWITCH_BOT_TOKEN, TWITCH_BOT_REFRESH_TOKEN)
        print("[HatmasBot] Token added")

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
        await web.stop()
        print("[HatmasBot] Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
