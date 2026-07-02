"""
Streaming Space Game Plugin (Phase 2)
=====================================
Lets Twitch chat spawn enemy ships into the browser game running at
http://localhost:8069/spacegame.

Commands (everyone):
    !red / !green / !yellow / !orange   spawn that enemy ship at the streamer
    !ships  (alias !spacegame)          how-to / what's open

Flow (mirrors the rest of the bot):
    chat command  ->  this plugin  ->  OverlayManager.broadcast("spacegame",
    "spawn", {type, user, id})  ->  the game's WebSocket  ->  ship warps in
    carrying the chatter's display name.

This is Phase 2 of StreamingSpaceGame_Plan.md: SIMPLE buttons + chat, no
currency yet (that's Phase 3). Spawns are free, gated only by a per-user
cooldown and a global rate limit so a raid can't flood the screen. The
game also enforces its own max-concurrent-ships cap.

The whole feature sits behind the "spacegame" toggle in DEFAULT_FEATURES
(default OFF, flip it live from the control panel's features card). Off =
every command here is silent, the commands are hidden from the /mod page
and Discord, and localhost:8069/spacegame 404s.
"""

import time
from collections import deque

# Valid ship types and the friendly aliases people will actually type.
SHIP_TYPES = ("red", "green", "yellow", "orange")
ALIASES = {
    "drifter": "red",
    "gunner": "green",
    "sniper": "green",
    "seeker": "yellow",
    "missile": "yellow",
    "kamikaze": "orange",
    "bomber": "orange",
}

# Per-user seconds between spawns (mods are exempt — handled by the bot).
USER_COOLDOWN = 4.0
# Global rate limit: at most this many spawns per window (anti-flood).
GLOBAL_MAX = 8
GLOBAL_WINDOW = 3.0


class SpaceGamePlugin:
    def __init__(self):
        self.bot = None
        self._spawn_id = 0
        self._recent = deque()  # timestamps of recent global spawns

    # === SETUP ===

    def setup(self, bot):
        self.bot = bot
        for t in SHIP_TYPES:
            # Bind the type via default arg so each command spawns its own ship.
            bot.register_command(
                t,
                lambda message, args, whisper=False, _t=t: self._cmd_spawn(message, _t),
                description=f"Spawn a {t} enemy ship in the space game",
                identity=True,
                plugin="spacegame",
                cooldown=USER_COOLDOWN,
            )
        for alias, t in ALIASES.items():
            bot.register_command(
                alias,
                lambda message, args, whisper=False, _t=t: self._cmd_spawn(message, _t),
                description=f"Spawn a {t} enemy ship (alias)",
                identity=True,
                plugin="spacegame",
                cooldown=USER_COOLDOWN,
            )
        bot.register_command(
            "spacegame", self.cmd_help,
            description="How to play the Streaming Space Game",
            plugin="spacegame",
        )
        bot.register_command(
            "ships", self.cmd_help,
            description="List the space-game ship commands",
            plugin="spacegame",
        )
        state = "ON" if self._feature_on() else "OFF (enable via control panel)"
        print(f"[SpaceGame] Ready — feature toggle is {state}; "
              "chat spawns ships with !red !green !yellow !orange")

    # === HELPERS ===

    def _feature_on(self):
        return bool(self.bot and self.bot.is_feature_enabled("spacegame"))

    def _overlay(self):
        return getattr(self.bot.web_server, "overlay", None) if self.bot else None

    def _game_is_live(self):
        ov = self._overlay()
        return bool(ov and ov.client_count("spacegame") > 0)

    def _global_rate_ok(self):
        now = time.monotonic()
        while self._recent and now - self._recent[0] > GLOBAL_WINDOW:
            self._recent.popleft()
        if len(self._recent) >= GLOBAL_MAX:
            return False
        self._recent.append(now)
        return True

    # === COMMANDS ===

    async def _cmd_spawn(self, message, ship_type):
        # Feature off = completely silent, as if the command didn't exist.
        if not self._feature_on():
            return
        ov = self._overlay()
        if ov is None:
            return

        # If nobody has the game open, let the asker know once (not spammy —
        # the per-user cooldown limits how often this can fire).
        if not self._game_is_live():
            await self.bot.send_reply(
                message,
                "The space game isn't on screen right now — ask the streamer "
                "to open it!",
            )
            return

        # Global anti-flood: silently drop if we're over the burst limit.
        if not self._global_rate_ok():
            return

        chatter = getattr(message, "chatter", None)
        user = ""
        if chatter:
            user = getattr(chatter, "display_name", None) or getattr(chatter, "name", "") or ""

        self._spawn_id += 1
        await ov.broadcast("spacegame", "spawn", {
            "type": ship_type,
            "user": user,
            "id": self._spawn_id,
        })

    async def cmd_help(self, message, args, whisper=False):
        if not self._feature_on():
            return
        live = "  (it's live now!)" if self._game_is_live() else ""
        await self.bot.send_reply(
            message,
            "Streaming Space Game — spawn ships at the streamer: "
            "!red (drifts down), !green (sniper), !yellow (homing missiles), "
            "!orange (kamikaze)." + live,
            whisper,
        )
