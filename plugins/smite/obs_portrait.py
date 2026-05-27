"""
plugins/smite/obs_portrait.py
=============================
OBS god-portrait + team-color background management.

When tracker.gg / the kill detector identifies the current god, we
swap two OBS image sources:
  * OBS_SOURCE_GOD_IMAGE  — the god's "funny image" (.gif preferred,
                            .png fallback) from SMITE2_GOD_IMAGES_DIR
  * OBS_SOURCE_GOD_BG      — a team-side or role-keyed background
                            from SMITE2_GOD_BG_DIR

Both fade in together over TITLE_FADE_DURATION via the OBS
ColorCorrection FadeFilter pattern. `_clear_god_image()` does the
inverse on match end. `_startup_hide_god_image()` snaps both to
opacity 0 / hidden when the bot launches (covers the case where the
bot crashed mid-match — leftover portrait would otherwise linger
until the next match started).

Failure handling: `_set_god_image` calls `_try_set_god_image` which
returns False on any OBS-side failure. On False we attempt one
reconnect via `obs.reconnect()` and retry. After that, we log loud
and bail — better to surface the issue than silently leave a stale
portrait on screen.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from core.config import (
    SMITE2_GOD_IMAGES_DIR, SMITE2_GOD_BG_DIR,
    OBS_SOURCE_GOD_IMAGE, OBS_SOURCE_GOD_BG,
    OBS_GOD_IMAGE_SCENE, OBS_GOD_IMAGE_GROUP,
    TITLE_FADE_DURATION,
)


class _OBSPortraitMixin:
    """
    Mixed into SmitePlugin. Reads/writes:
      self.bot                         (for self.bot.plugins["obs"])
      self.current_god, self.match_players, self.current_match_id,
      self.match_start_time            — for _update_overlay_state
    Calls obs plugin methods (`ensure_color_correction_filter`,
    `set_source_filter_value`, `set_image_source`, `set_source_visible`,
    `reconnect`) and `bot.web_server.update_smite_state` for the
    overlay sync.
    """

    # Background file mapping: team/role → filename
    BG_MAP = {
        # Team-based (Chaos/Order)
        "chaos": "bg_chaos_red.png",
        "order": "bg_order_blue.png",
        # Role-based (Conquest)
        "carry": "bg_carry_gold.png",
        "support": "bg_support_emerald.png",
        "mid": "bg_mid_blue.png",
        "jungle": "bg_jungle_forest.png",
        "solo": "bg_solo_orange.png",
    }

    # Tracker.gg teamId values → team name mapping
    # Team 1 = Order, Team 2 = Chaos (standard Smite convention)
    TEAM_MAP = {
        "1": "order",
        "2": "chaos",
        1: "order",
        2: "chaos",
        "order": "order",
        "chaos": "chaos",
    }

    async def _set_god_image(self, god_name, team=None):
        """Swap the OBS image source to the god's funny image, if it exists.
        Also sets the appropriate background based on team side.
        Fades both sources in smoothly.

        Implements retry logic: if OBS operations fail, attempts one reconnect + retry cycle."""
        if "obs" not in self.bot.plugins:
            return

        if not SMITE2_GOD_IMAGES_DIR:
            return

        god_dir = Path(SMITE2_GOD_IMAGES_DIR)
        if not god_dir.exists():
            print(f"[Smite] God images directory not found: {god_dir}")
            return

        # Build candidate list: gif first, then png, across name formats
        slug = god_name.lower().replace(" ", "-").replace("'", "")
        names = [god_name, god_name.lower(), slug]
        extensions = [".gif", ".png"]

        image_path = None
        for ext in extensions:
            for name in names:
                candidate = god_dir / f"{name}{ext}"
                if candidate.exists():
                    image_path = candidate
                    break
            if image_path:
                break

        if not image_path:
            print(f"[Smite] No image found for {god_name} in {god_dir} — hiding source")
            await self._clear_god_image()
            return

        obs = self.bot.plugins["obs"]
        scene = OBS_GOD_IMAGE_SCENE or None
        group = OBS_GOD_IMAGE_GROUP or None

        # Attempt to set god image with retry logic
        success = await self._try_set_god_image(
            obs, image_path, scene, group, team
        )

        if not success:
            # Try one reconnect + retry cycle
            print("[Smite] God portrait update failed, attempting reconnect...")
            reconnect_success = await obs.reconnect()
            if reconnect_success:
                print("[Smite] Reconnect successful, retrying god portrait update...")
                success = await self._try_set_god_image(
                    obs, image_path, scene, group, team
                )

        if not success:
            print("[Smite] ERROR: God portrait update failed permanently after reconnect attempt. "
                  "Check OBS connection and scene/source configuration.")

    async def _try_set_god_image(self, obs, image_path, scene, group, team):
        """Inner method for god image update. Returns True if successful, False otherwise."""
        try:
            # Set opacity to 0 before making visible (so we can fade in)
            if not await obs.ensure_color_correction_filter(OBS_SOURCE_GOD_IMAGE):
                return False
            if not await obs.set_source_filter_value(OBS_SOURCE_GOD_IMAGE, "FadeFilter", {"opacity": 0}):
                return False
            if not await obs.ensure_color_correction_filter(OBS_SOURCE_GOD_BG):
                return False
            if not await obs.set_source_filter_value(OBS_SOURCE_GOD_BG, "FadeFilter", {"opacity": 0}):
                return False

            # Set the image file and make source visible
            if not await obs.set_image_source(OBS_SOURCE_GOD_IMAGE, str(image_path.resolve())):
                return False
            if not await obs.set_source_visible(OBS_SOURCE_GOD_IMAGE, True,
                                                scene=scene, group=group):
                return False
            print(f"[Smite] God image set: {image_path.name}")
        except Exception as e:
            print(f"[Smite] OBS god image error: {e}")
            return False

        # Set the background based on team side
        await self._set_god_background(team=team)

        # Fade both sources in together
        try:
            fade_duration = TITLE_FADE_DURATION
            steps = 20
            step_delay = fade_duration / steps
            for i in range(steps + 1):
                opacity = i / steps
                success_img = await obs.set_source_filter_value(
                    OBS_SOURCE_GOD_IMAGE, "FadeFilter", {"opacity": int(opacity * 100)})
                success_bg = await obs.set_source_filter_value(
                    OBS_SOURCE_GOD_BG, "FadeFilter", {"opacity": int(opacity * 100)})
                if not success_img or not success_bg:
                    print("[Smite] Fade operation failed, aborting fade sequence")
                    return False
                await asyncio.sleep(step_delay)
            print(f"[Smite] Fade in complete ({fade_duration}s)")
            return True
        except Exception as e:
            print(f"[Smite] Fade in error: {e}")
            # Fallback: ensure full opacity
            try:
                await obs.set_source_filter_value(OBS_SOURCE_GOD_IMAGE, "FadeFilter", {"opacity": 100})
                await obs.set_source_filter_value(OBS_SOURCE_GOD_BG, "FadeFilter", {"opacity": 1.0})
            except Exception:
                pass
            return False

    async def _set_god_background(self, team=None, role=None):
        """Set the god portrait background image based on role or team side.
        Priority: role (if conquest) > team side > default to chaos."""
        if "obs" not in self.bot.plugins:
            return
        if not SMITE2_GOD_BG_DIR:
            return

        bg_dir = Path(SMITE2_GOD_BG_DIR)
        if not bg_dir.exists():
            print(f"[Smite] Background directory not found: {bg_dir}")
            return

        # Determine which background to use
        # Priority: role > team > default (chaos)
        bg_key = None
        if role and role.lower() in self.BG_MAP:
            bg_key = role.lower()
        elif team:
            team_name = self.TEAM_MAP.get(team, "chaos")
            bg_key = team_name
        else:
            bg_key = "chaos"  # Default fallback

        bg_file = self.BG_MAP.get(bg_key, "bg_chaos_red.png")
        bg_path = bg_dir / bg_file

        if not bg_path.exists():
            print(f"[Smite] Background not found: {bg_path}")
            return

        obs = self.bot.plugins["obs"]
        scene = OBS_GOD_IMAGE_SCENE or None
        group = OBS_GOD_IMAGE_GROUP or None

        try:
            success = await obs.set_image_source(OBS_SOURCE_GOD_BG, str(bg_path.resolve()))
            if not success:
                print(f"[Smite] OBS background error: set_image_source failed")
                return
            success = await obs.set_source_visible(OBS_SOURCE_GOD_BG, True,
                                                    scene=scene, group=group)
            if not success:
                print(f"[Smite] OBS background error: set_source_visible failed")
                return
            print(f"[Smite] Background set: {bg_file} (key={bg_key})")
        except Exception as e:
            print(f"[Smite] OBS background error: {e}")

    async def _startup_hide_god_image(self):
        """Wait for OBS to connect, then instantly hide god portrait on startup."""
        # Wait up to 15 seconds for OBS plugin to connect
        for _ in range(30):
            if "obs" in self.bot.plugins:
                obs = self.bot.plugins["obs"]
                if getattr(obs, "client", None) is not None:
                    break
            await asyncio.sleep(0.5)
        else:
            print("[Smite] Startup hide skipped — OBS never connected")
            return

        obs = self.bot.plugins["obs"]
        scene = OBS_GOD_IMAGE_SCENE or None
        group = OBS_GOD_IMAGE_GROUP or None
        try:
            await obs.set_source_filter_value(
                OBS_SOURCE_GOD_IMAGE, "FadeFilter", {"opacity": 0})
            await obs.set_source_visible(OBS_SOURCE_GOD_IMAGE, False,
                                          scene=scene, group=group)
        except Exception as e:
            print(f"[Smite] Startup hide god image error: {e}")
        try:
            await obs.set_source_filter_value(
                OBS_SOURCE_GOD_BG, "FadeFilter", {"opacity": 0})
            await obs.set_source_visible(OBS_SOURCE_GOD_BG, False,
                                          scene=scene, group=group)
        except Exception as e:
            print(f"[Smite] Startup hide background error: {e}")
        print("[Smite] God portrait hidden on startup")

    async def _clear_god_image(self):
        """Fade out then hide the OBS god image and background sources."""
        if "obs" not in self.bot.plugins:
            return
        obs = self.bot.plugins["obs"]
        scene = OBS_GOD_IMAGE_SCENE or None
        group = OBS_GOD_IMAGE_GROUP or None

        # Fade both sources out together
        try:
            fade_duration = TITLE_FADE_DURATION
            steps = 20
            step_delay = fade_duration / steps
            for i in range(steps + 1):
                opacity = 1.0 - (i / steps)
                await obs.set_source_filter_value(
                    OBS_SOURCE_GOD_IMAGE, "FadeFilter", {"opacity": int(opacity * 100)})
                await obs.set_source_filter_value(
                    OBS_SOURCE_GOD_BG, "FadeFilter", {"opacity": int(opacity * 100)})
                await asyncio.sleep(step_delay)
            print(f"[Smite] Fade out complete ({fade_duration}s)")
        except Exception as e:
            print(f"[Smite] Fade out error: {e}")

        # Hide sources after fade completes
        try:
            await obs.set_source_visible(OBS_SOURCE_GOD_IMAGE, False,
                                          scene=scene, group=group)
            print("[Smite] God image hidden")
        except Exception as e:
            print(f"[Smite] OBS clear god image error: {e}")

        try:
            await obs.set_source_visible(OBS_SOURCE_GOD_BG, False,
                                          scene=scene, group=group)
            print("[Smite] Background hidden")
        except Exception as e:
            print(f"[Smite] OBS clear background error: {e}")

    def _update_overlay_state(self):
        """Push current god/match data to the webserver for the overlay."""
        if not self.bot.web_server:
            return

        if self.current_god:
            self.bot.web_server.update_smite_state({
                "in_match": True,
                "god": self.current_god,
                "players": self.match_players,
                "match_id": self.current_match_id,
                "match_duration": (
                    int(time.time() - self.match_start_time)
                    if self.match_start_time else 0
                ),
            })
        else:
            self.bot.web_server.update_smite_state({
                "in_match": False,
                "god": None,
                "players": [],
                "match_id": None,
                "match_duration": 0,
            })
