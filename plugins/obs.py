"""
OBS Plugin
===========
Controls OBS via WebSocket for scene switching, overlay management,
and stream automation.
"""

import asyncio
import obsws_python as obs

from core.config import (
    OBS_WS_HOST, OBS_WS_PORT, OBS_WS_PASSWORD,
    OBS_SCENE_MAIN, OBS_SCENE_LOBBY, OBS_SCENE_INGAME, OBS_SCENE_SNAP,
    OBS_SOURCE_NOW_PLAYING, OBS_SOURCE_SNAP
)


class OBSPlugin:
    def __init__(self):
        self.bot = None
        self.client = None
        self.previous_scene = None

    def setup(self, bot):
        self.bot = bot
        bot.register_command("scene", self.cmd_scene, mod_only=True)
        bot.register_command("overlay", self.cmd_overlay, mod_only=True)

    async def on_ready(self):
        await self._connect()

    async def _connect(self):
        try:
            self.client = obs.ReqClient(
                host=OBS_WS_HOST,
                port=OBS_WS_PORT,
                password=OBS_WS_PASSWORD,
            )
            print("[OBS] Connected to OBS WebSocket")
            return True
        except Exception as e:
            print(f"[OBS] Connection failed: {e}")
            print("[OBS] Make sure OBS is running with WebSocket enabled")
            self.client = None
            return False

    def _ensure_connected(self):
        if not self.client:
            raise ConnectionError("OBS not connected")

    async def reconnect(self):
        """Attempt to reconnect to OBS. Returns True if successful, False otherwise."""
        print("[OBS] Attempting to reconnect...")
        return await self._connect()

    # === SCENE CONTROL ===

    async def switch_scene(self, scene_name):
        self._ensure_connected()
        try:
            self.client.set_current_program_scene(scene_name)
            print(f"[OBS] Switched to scene: {scene_name}")
            return True
        except Exception as e:
            print(f"[OBS] Scene switch error: {e}")
            return False

    async def switch_to_game(self):
        self.previous_scene = self._get_current_scene()
        await self.switch_scene(OBS_SCENE_INGAME)

    async def switch_to_lobby(self):
        await self.switch_scene(OBS_SCENE_LOBBY)

    async def trigger_snap_scene(self):
        self.previous_scene = self._get_current_scene()
        await self.switch_scene(OBS_SCENE_SNAP)

    async def return_from_snap(self):
        if self.previous_scene:
            await self.switch_scene(self.previous_scene)
            self.previous_scene = None
        else:
            await self.switch_scene(OBS_SCENE_MAIN)

    def _get_current_scene(self):
        try:
            resp = self.client.get_current_program_scene()
            return resp.scene_name
        except Exception:
            return OBS_SCENE_MAIN

    # === SOURCE CONTROL ===

    async def set_source_visible(self, source_name, visible, scene=None, group=None):
        self._ensure_connected()
        try:
            if not scene:
                scene = self._get_current_scene()
            if group:
                # Source is nested inside a group — look up the group first,
                # then find the source within the group's scene items
                group_id = self.client.get_scene_item_id(scene, group).scene_item_id
                item_id = self.client.get_scene_item_id(group, source_name).scene_item_id
                self.client.set_scene_item_enabled(group, item_id, visible)
            else:
                scene_item_id = self.client.get_scene_item_id(scene, source_name).scene_item_id
                self.client.set_scene_item_enabled(scene, scene_item_id, visible)
            print(f"[OBS] Source '{source_name}' visible={visible}")
            return True
        except Exception as e:
            print(f"[OBS] Source control error: {e}")
            return False

    async def update_text_source(self, source_name, text):
        self._ensure_connected()
        try:
            self.client.set_input_settings(
                source_name,
                {"text": text},
                overlay=True
            )
            return True
        except Exception as e:
            print(f"[OBS] Text update error: {e}")
            return False

    async def refresh_browser_source(self, source_name):
        self._ensure_connected()
        try:
            self.client.press_input_properties_button(source_name, "refreshnocache")
            return True
        except Exception as e:
            print(f"[OBS] Browser refresh error: {e}")
            return False

    async def set_image_source(self, source_name, file_path):
        """Change the file path of an Image source. Returns True on success, False on failure."""
        self._ensure_connected()
        try:
            self.client.set_input_settings(
                source_name,
                {"file": file_path},
                overlay=True
            )
            print(f"[OBS] Image source '{source_name}' set to: {file_path}")
            return True
        except Exception as e:
            print(f"[OBS] Image source error: {e}")
            return False

    # === FILTER CONTROL ===

    async def set_source_filter_value(self, source_name, filter_name, settings):
        """Update settings on a source filter. Returns True on success, False on failure."""
        self._ensure_connected()
        try:
            self.client.set_source_filter_settings(
                source_name, filter_name, settings, overlay=True
            )
            return True
        except Exception as e:
            print(f"[OBS] Filter update error ({source_name}/{filter_name}): {e}")
            return False

    # OBS filter kind string varies by version — try these in order
    COLOR_FILTER_KINDS = ["color_filter", "color_filter_v4", "color_correction_filter"]

    async def ensure_color_correction_filter(self, source_name, filter_name="FadeFilter"):
        """Ensure a Color Correction filter exists on the source for fade effects.
        Creates it if it doesn't exist, with opacity set to 0 (invisible).
        Tries multiple filter kind strings for OBS version compatibility."""
        self._ensure_connected()
        try:
            # Check if filter already exists
            filters = self.client.get_source_filter_list(source_name)
            for f in filters.filters:
                if f.get("filterName") == filter_name:
                    return True  # Already exists

            # Try each filter kind until one works
            for kind in self.COLOR_FILTER_KINDS:
                try:
                    self.client.create_source_filter(
                        source_name,
                        filter_name,
                        kind,
                        {"opacity": 0},
                    )
                    print(f"[OBS] Created FadeFilter on '{source_name}' (kind={kind})")
                    return True
                except Exception:
                    continue

            print(f"[OBS] Could not create fade filter on '{source_name}' — no compatible filter kind found")
            return False
        except Exception as e:
            print(f"[OBS] Could not create fade filter on '{source_name}': {e}")
            return False

    async def fade_source(self, source_name, fade_in=True, duration=1.0, steps=20,
                          filter_name="FadeFilter"):
        """Fade a source in or out using a Color Correction opacity filter.

        Args:
            source_name: OBS source name
            fade_in: True = fade 0→1 (appear), False = fade 1→0 (disappear)
            duration: Total fade time in seconds
            steps: Number of opacity steps (higher = smoother)
            filter_name: Name of the Color Correction filter to use
        """
        if not await self.ensure_color_correction_filter(source_name, filter_name):
            return False

        step_delay = duration / steps
        for i in range(steps + 1):
            if fade_in:
                opacity = i / steps
            else:
                opacity = 1.0 - (i / steps)
            success = await self.set_source_filter_value(
                source_name, filter_name, {"opacity": int(opacity * 100)}
            )
            if not success:
                return False
            await asyncio.sleep(step_delay)

        print(f"[OBS] Fade {'in' if fade_in else 'out'} complete: {source_name}")
        return True

    async def set_browser_source_url(self, source_name, url):
        self._ensure_connected()
        try:
            self.client.set_input_settings(
                source_name,
                {"url": url},
                overlay=True
            )
            return True
        except Exception as e:
            print(f"[OBS] Browser URL update error: {e}")
            return False

    # === OVERLAY CONTROL ===

    async def show_now_playing(self):
        await self.set_source_visible(OBS_SOURCE_NOW_PLAYING, True)

    async def hide_now_playing(self):
        await self.set_source_visible(OBS_SOURCE_NOW_PLAYING, False)

    async def show_now_playing_timed(self, duration=10):
        await self.show_now_playing()
        await asyncio.sleep(duration)
        await self.hide_now_playing()

    # === STREAM LIFECYCLE ===

    async def go_live_sequence(self):
        """Automated go-live sequence."""
        print("[OBS] Starting go-live sequence...")

        # Switch to main scene
        await self.switch_scene(OBS_SCENE_MAIN)

        # Start Spotify playlist if song request plugin exists
        if "songrequest" in self.bot.plugins:
            sr = self.bot.plugins["songrequest"]
            await sr._resume_spotify()

        print("[OBS] Go-live sequence complete")

    async def stop_stream_sequence(self):
        """Automated stream shutdown sequence."""
        print("[OBS] Starting shutdown sequence...")

        # Pause Spotify
        if "songrequest" in self.bot.plugins:
            sr = self.bot.plugins["songrequest"]
            await sr._pause_spotify()

        # Hide overlays
        await self.hide_now_playing()

        print("[OBS] Shutdown sequence complete")

    # === COMMANDS ===

    async def cmd_scene(self, message, args, whisper=False):
        if not args:
            current = self._get_current_scene()
            await self.bot.send_reply(message, f"Current scene: {current}", whisper)
            return
        await self.switch_scene(args.strip())
        await self.bot.send_reply(message, f"Switched to: {args.strip()}", whisper)

    async def cmd_overlay(self, message, args, whisper=False):
        parts = args.strip().lower().split()
        if not parts:
            await self.bot.send_reply(message, "Usage: !overlay <on|off|auto>", whisper)
            return

        mode = parts[0]
        if mode == "on":
            await self.show_now_playing()
            await self.bot.send_reply(message, "Now Playing overlay: always on", whisper)
        elif mode == "off":
            await self.hide_now_playing()
            await self.bot.send_reply(message, "Now Playing overlay: hidden", whisper)
        elif mode == "auto":
            await self.bot.send_reply(message, "Now Playing overlay: auto mode", whisper)

    async def cleanup(self):
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
