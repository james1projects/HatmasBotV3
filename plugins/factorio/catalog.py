"""
Card catalog: maps Streamloots card names to mod actions, builds the
Lua command lines, and formats outbox events for Twitch chat.

The card map lives in config (FACTORIO_CARD_MAP) so renaming a card in
the Streamloots dashboard is a config_local.py edit, not a code change.
Map shape:  {"<card name>": {"action": "<key>", "cooldown": <seconds>}}

Action keys (mirroring the mod's remote interface):
    adopt_pet   -> spawn_pet(username, card message as pet name)
    grow_pet    -> upgrade_pet(username)
    pet_say     -> pet_say(username, card message)
    boss_attack -> spawn_boss(username, random direction, default distance)
"""

from typing import Dict, Optional

VALID_ACTIONS = {"adopt_pet", "grow_pet", "pet_say", "boss_attack"}

# Shown in the card manager UI (/factorio/cards) action dropdown.
ACTION_INFO = {
    "adopt_pet": "Spawn a pet biter that follows the streamer. "
                 "Card text field = pet name (falls back to owner's name).",
    "grow_pet": "Grow the viewer's pet one size "
                "(small -> medium -> big -> behemoth).",
    "pet_say": "Show the card's text message above the viewer's pet "
               "for 5 seconds.",
    "boss_attack": "Spawn a boss biter named after the viewer that "
                   "attacks the streamer from a random direction.",
}


def lua_quote(value: str) -> str:
    """Escape an untrusted string for inclusion in a double-quoted Lua
    string literal sent over RCON. Newlines collapse to spaces."""
    s = str(value or "")
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r", " ").replace("\n", " ")
    return s


def remote_call(func: str, *args) -> str:
    """Build a /silent-command line that calls the mod's remote
    interface and prints its return value back over RCON."""
    quoted = ", ".join(f'"{lua_quote(a)}"' for a in args)
    inner = f'remote.call("hatmas", "{func}"'
    if quoted:
        inner += f", {quoted}"
    inner += ")"
    return f"/silent-command rcon.print(tostring({inner} or 'ok'))"


def build_command(action: str, username: str,
                  message: Optional[str]) -> Optional[str]:
    """Translate a card action into the RCON command line."""
    username = (username or "viewer").strip()
    message = (message or "").strip()
    if action == "adopt_pet":
        pet_name = message or f"{username}'s biter"
        return remote_call("spawn_pet", username, pet_name, "small")
    if action == "grow_pet":
        return remote_call("upgrade_pet", username)
    if action == "pet_say":
        return remote_call("pet_say", username, message or "hello")
    if action == "boss_attack":
        return remote_call("spawn_boss", username)
    return None


# ── outbox event -> chat line ──────────────────────────────────────
# Plain bot tone: no emojis, no flair.

def format_event(ev: Dict) -> Optional[str]:
    etype = ev.get("event")
    if etype == "pet_spawned":
        return (f"Factorio: pet {ev.get('pet_name')} spawned for "
                f"{ev.get('owner')}.")
    if etype == "pet_upgraded":
        return (f"Factorio: {ev.get('owner')}'s pet grew to "
                f"{ev.get('size')}.")
    if etype == "pet_died":
        return (f"Factorio: pet {ev.get('pet_name')} ({ev.get('owner')}) "
                f"died after {ev.get('lifetime_seconds')}s. "
                f"Killed by: {ev.get('killed_by')}.")
    if etype == "boss_spawned":
        return (f"Factorio: {ev.get('viewer')} sent a boss biter from "
                f"the {ev.get('direction')}.")
    if etype == "boss_enraged":
        return f"Factorio: {ev.get('viewer')}'s boss is enraged."
    if etype == "boss_died":
        return (f"Factorio: {ev.get('viewer')}'s boss is down after "
                f"{ev.get('seconds_alive')}s. "
                f"Final blow: {ev.get('killed_by')}.")
    # pet_removed and unknown events: no chat announcement
    return None
