"""
Test stream title auto-update.

Usage:
  python test_title.py                    — Show current stream title
  python test_title.py "Sylvanus"         — Set title using god template with Sylvanus
  python test_title.py lobby              — Set title to lobby template
  python test_title.py custom "My Title"  — Set an exact custom title
"""

import sys
import asyncio
import aiohttp

sys.path.insert(0, ".")
from core.config import (
    TWITCH_CLIENT_ID, TWITCH_BOT_TOKEN, TWITCH_BROADCASTER_TOKEN,
    TWITCH_OWNER_ID, TITLE_TEMPLATE_GOD, TITLE_TEMPLATE_LOBBY,
)


def twitch_headers():
    token = TWITCH_BROADCASTER_TOKEN or TWITCH_BOT_TOKEN
    return {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def get_current_title():
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.twitch.tv/helix/channels?broadcaster_id={TWITCH_OWNER_ID}",
            headers=twitch_headers(),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                channel = data["data"][0]
                return channel.get("title", "(no title)")
            else:
                body = await resp.text()
                print(f"ERROR: {resp.status} {body}")
                return None


async def set_title(title):
    async with aiohttp.ClientSession() as session:
        async with session.patch(
            "https://api.twitch.tv/helix/channels",
            headers=twitch_headers(),
            json={
                "broadcaster_id": TWITCH_OWNER_ID,
                "title": title,
            }
        ) as resp:
            if resp.status == 204:
                print(f"  Title updated successfully!")
                return True
            else:
                body = await resp.text()
                print(f"  ERROR: {resp.status} {body}")
                return False


async def main():
    print(f"Config:")
    print(f"  TWITCH_OWNER_ID:      {TWITCH_OWNER_ID}")
    print(f"  TITLE_TEMPLATE_GOD:   {TITLE_TEMPLATE_GOD}")
    print(f"  TITLE_TEMPLATE_LOBBY: {TITLE_TEMPLATE_LOBBY}")

    # Show current title
    current = await get_current_title()
    if current:
        print(f"\n  Current title: {current}")

    if len(sys.argv) < 2:
        print(f"\nUsage:")
        print(f'  python test_title.py "Sylvanus"         — God template')
        print(f'  python test_title.py lobby               — Lobby template')
        print(f'  python test_title.py custom "My Title"   — Exact title')
        return

    action = sys.argv[1]

    if action.lower() == "lobby":
        title = TITLE_TEMPLATE_LOBBY
        print(f"\n  Setting lobby title: {title}")
        await set_title(title)

    elif action.lower() == "custom" and len(sys.argv) >= 3:
        title = " ".join(sys.argv[2:])
        print(f"\n  Setting custom title: {title}")
        await set_title(title)

    else:
        # Treat as god name
        god_name = " ".join(sys.argv[1:])
        title = TITLE_TEMPLATE_GOD.replace("{god}", god_name)
        print(f"\n  Setting god title: {title}")
        await set_title(title)

    # Show updated title
    updated = await get_current_title()
    if updated:
        print(f"  Verified title: {updated}")


if __name__ == "__main__":
    asyncio.run(main())
