"""
Standalone RCON probe for the hatmas-events Factorio bridge.
Verifies the game is reachable WITHOUT starting the whole bot.

Usage: python tools/check_factorio_rcon.py

Checks, in order:
  1. FACTORIO_RCON_PASSWORD configured
  2. TCP connect + RCON auth to FACTORIO_RCON_HOST:PORT
  3. remote.call("hatmas", "ping")  -> proves the mod is loaded
  4. list_pets()                    -> proves remote calls round-trip

Exit 0 = bridge fully working, 1 = something failed (message says what).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import (
    FACTORIO_RCON_HOST, FACTORIO_RCON_PORT, FACTORIO_RCON_PASSWORD,
)
from plugins.factorio.rcon import RconClient, RconAuthError
from plugins.factorio.catalog import remote_call


async def main() -> int:
    if not FACTORIO_RCON_PASSWORD:
        print("FAIL  FACTORIO_RCON_PASSWORD is empty in core/config_local.py")
        return 1
    print(f"OK    password configured")
    client = RconClient(FACTORIO_RCON_HOST, FACTORIO_RCON_PORT,
                        FACTORIO_RCON_PASSWORD, timeout=5.0)
    try:
        await client.connect()
    except RconAuthError:
        print(f"FAIL  RCON auth rejected at {FACTORIO_RCON_HOST}:"
              f"{FACTORIO_RCON_PORT} - password mismatch between "
              f"config_local.py and the game's --rcon-password")
        return 1
    except Exception as e:
        print(f"FAIL  cannot connect to {FACTORIO_RCON_HOST}:"
              f"{FACTORIO_RCON_PORT} ({e})")
        print("      Is Factorio running? Did you launch via "
              "start_factorio.bat AND host a multiplayer game? "
              "(RCON is not active in single player or in the menu.)")
        return 1
    print(f"OK    connected + authenticated "
          f"({FACTORIO_RCON_HOST}:{FACTORIO_RCON_PORT})")
    try:
        pong = await client.command(remote_call("ping"))
        if "hatmas-events" in pong:
            print(f"OK    mod responding ({pong.strip()})")
        else:
            print(f"FAIL  unexpected ping response: {pong.strip()!r}")
            print("      Is the hatmas-events mod installed AND enabled "
                  "for this save? (Mods menu)")
            await client.close()
            return 1
        pets = await client.command(remote_call("list_pets"))
        print(f"OK    remote calls round-trip (list_pets -> "
              f"{pets.strip()!r})")
    finally:
        await client.close()
    print()
    print("Bridge fully working. The bot's card manager "
          "(http://localhost:8069/factorio/cards) will show "
          "RCON: connected.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
