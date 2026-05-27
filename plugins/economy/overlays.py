"""
plugins/economy/overlays.py
===========================
Bridges the economy plugin to the OBS browser-source overlays + the
voiceline overlay.

Outbound traffic falls into two categories:

  1. **OverlayManager events** — `_emit_overlay_event(name, data)`
     pushes a structured event onto the central overlay manager's bus.
     The manager applies show/hide/keep_alive_on rules from
     `core/overlay_rules.json` and broadcasts to whichever browser
     sources have a matching subscription. Used for ticker updates,
     match-live ticks, dividend payouts, the leaderboard, the trade
     feed, and the portfolio request.

  2. **Voiceline triggers** — `_trigger_voiceline(trigger_key, god)`
     plays a VGS voice line through the voicelines overlay (audio +
     optional MP4). Currently gated behind ECONOMY_VOICELINES_ENABLED
     because god voiceline file naming is too inconsistent for the
     search heuristic to be reliable; flip that flag in config and
     fix the per-god file mapping when you're ready to enable it.

`_emit_trade_event` and `_emit_leaderboard` are convenience wrappers
that build a payload from current state and call _emit_overlay_event.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Dict, Optional

from core.config import DATA_DIR

try:
    from core.config import ECONOMY_VOICELINES_ENABLED
except ImportError:
    # Older config_local.py overlays may not define this constant yet.
    ECONOMY_VOICELINES_ENABLED = False


# Where pre-downloaded god voice line .ogg files live on disk.
# Populated by tools/download_voicelines.py.
VOICELINE_DIR = DATA_DIR / "smite_voicelines"


# Maps economy events to SMITE VGS search patterns.
# God voiceline files use inconsistent naming (Ymir_Emote_R, Bellona_VER,
# Danzaburou_vox_vgs_emote_r), so we search case-insensitively with
# multiple possible suffixes per trigger.
VGS_TRIGGERS = {
    "dividend":   ["emote_r",  "ver"],      # "You Rock!"
    "win":        ["emote_a",  "vea"],      # "Awesome!"
    "loss":       ["other_v_t", "vvgt"],    # "That's too bad"
    "big_spike":  ["emote_w",  "vew"],      # "Woohoo!"
    "big_crash":  ["help",     "vhh"],      # "Help!"
}


class _OverlaysMixin:
    """
    Mixed into EconomyPlugin. Reads:
      self.bot                — for self.bot.web_server.overlay
      self._match_god         — voiceline god fallback
      self._db                — for _emit_leaderboard's portfolio query
    """

    def _emit_overlay_event(self, event_name: str, data: Dict):
        """Emit an event to the overlay manager."""
        if not self.bot or not self.bot.web_server:
            return
        overlay_mgr = getattr(self.bot.web_server, "overlay", None)
        if overlay_mgr:
            asyncio.create_task(overlay_mgr.emit(event_name, data))

    def _trigger_voiceline(self, trigger_key: str, god_name: Optional[str] = None):
        """
        Play a VGS voice line through the voiceline overlay.

        trigger_key: one of 'dividend', 'win', 'loss', 'big_spike', 'big_crash'
        god_name:    god display name (e.g. 'Ymir'). Falls back to _match_god.

        Gated behind ECONOMY_VOICELINES_ENABLED in core/config.py. The
        feature is currently disabled because god voiceline file naming
        is wildly inconsistent across gods and the search heuristic
        below misfires more than it lands. When a proper god→file
        mapping is built, flip the config flag and let this run.
        """
        if not ECONOMY_VOICELINES_ENABLED:
            return
        suffixes = VGS_TRIGGERS.get(trigger_key)
        if not suffixes:
            return

        god = god_name or self._match_god
        if not god:
            return

        god_slug = god.lower().replace(" ", "_").replace("'", "")
        vgs_dir = VOICELINE_DIR / god_slug / "vgs"
        if not vgs_dir.exists():
            print(f"[Economy] No VGS dir for {god_slug}")
            return

        # God voicelines have wildly inconsistent naming across gods:
        #   Ymir_Emote_R.ogg, AthenaV2_vox_vgs_emote_r.ogg,
        #   Agni_VGS_Emote_R.ogg, Bellona_VER.ogg
        # Search case-insensitively for any file ending in _{suffix}.ogg
        matches = []
        all_files = list(vgs_dir.iterdir())
        for suffix in suffixes:
            for f in all_files:
                # Case-insensitive match: filename ends with _{suffix}.ogg
                name_lower = f.name.lower()
                if name_lower.endswith(f"_{suffix.lower()}.ogg") or name_lower.endswith(f"_{suffix.lower()}_1.ogg"):
                    matches.append(f)
            if matches:
                break  # Use first suffix that has results

        if not matches:
            print(f"[Economy] No VGS file for {trigger_key} ({suffixes}) for {god_slug}")
            return

        chosen = random.choice(matches)
        audio_url = f"/api/voiceline_audio/{god_slug}/vgs/{chosen.name}"

        god_display = god_slug.replace("_", " ").title()
        event = {
            "type": f"economy_{trigger_key}",
            "god": god_display,
            "user": "HatmasBot",
            "audio_url": audio_url,
            "video_url": None,
            "timestamp": time.time(),
        }

        web_server = getattr(self.bot, "web_server", None)
        if web_server and hasattr(web_server, "trigger_voiceline_event"):
            web_server.trigger_voiceline_event(event)
            print(f"[Economy] VGS triggered: {trigger_key} → {chosen.name}")

    def _emit_trade_event(self, trade_type: str, username: str, god_name: str,
                          shares: float, price: float, total: float, fee: float):
        """Emit a trade event to the trade feed overlay."""
        self._emit_overlay_event("trade_executed", {
            "type": trade_type,
            "username": username,
            "god": god_name,
            "shares": round(shares, 2),
            "price": round(price),
            "total": round(total),
            "fee": round(fee),
            "timestamp": datetime.now().isoformat(),
        })

    async def _emit_leaderboard(self):
        """Emit leaderboard data to the overlay."""
        leaderboard = []
        async with self._db.execute("""
            SELECT p.username, SUM(p.shares * gp.price) as portfolio_value
            FROM portfolios p
            JOIN god_prices gp ON p.god_name = gp.god_name
            WHERE p.shares > 0.001
            GROUP BY p.username
            ORDER BY portfolio_value DESC
            LIMIT 10
        """) as cursor:
            rank = 1
            async for row in cursor:
                username, value = row
                leaderboard.append({
                    "rank": rank,
                    "username": username,
                    "portfolio_value": round(value),
                })
                rank += 1

        self._emit_overlay_event("leaderboard_update", {"leaderboard": leaderboard})
