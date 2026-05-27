"""
plugins/smite/title.py
======================
Stream-title automation + the rotating {command} placeholder.

Two interlocking pieces:

  * `_command_rotation_loop` runs in a background task started by
    on_ready. Every TITLE_COMMAND_ROTATION_INTERVAL seconds it advances
    `_current_command` through TITLE_COMMAND_ROTATION and re-applies
    the current title with the new placeholder filled.

  * `_update_stream_title(god_name)` is the canonical title write — it
    expands TITLE_TEMPLATE_GOD or TITLE_TEMPLATE_LOBBY (depending on
    whether god_name is set) with placeholders {god}, {command},
    {record}, {song} and PATCHes /helix/channels. Auto-refreshes the
    token on 401 and retries once.

Plus runtime-mutable templates: `set_title_template()` updates the
in-memory globals AND persists to data/title_templates.json so the
dashboard's edits survive restarts. (Actual JSON load happens in
core/config.py at import time, before this module ever sees the
config — see config.py:_load_persisted_title_templates for the
race fix.)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.config import (
    TWITCH_OWNER_ID,
    TITLE_AUTO_UPDATE, TITLE_TEMPLATE_GOD, TITLE_TEMPLATE_LOBBY,
    TITLE_COMMAND_ROTATION, TITLE_COMMAND_ROTATION_INTERVAL,
    DATA_DIR,
)


class _TitleMixin:
    """
    Mixed into SmitePlugin. Reads/writes:
      self.session             aiohttp session
      self._token_manager      auto-refresh on 401
      self.current_god         for re-titling on rotation tick
      self.bot                 for is_feature_enabled + accessing other plugins
      self._command_index, self._current_command
      module globals           TITLE_TEMPLATE_GOD / TITLE_TEMPLATE_LOBBY (mutated by set_title_template)
    """

    async def _command_rotation_loop(self):
        """Background task that rotates the {command} placeholder at a fixed interval."""
        await asyncio.sleep(5)  # Short startup delay
        print(f"[Smite] Command rotation started — {len(TITLE_COMMAND_ROTATION)} commands, "
              f"rotating every {TITLE_COMMAND_ROTATION_INTERVAL}s")
        while True:
            try:
                await asyncio.sleep(TITLE_COMMAND_ROTATION_INTERVAL)
                if not TITLE_COMMAND_ROTATION:
                    continue
                # Advance to next command
                self._command_index = (self._command_index + 1) % len(TITLE_COMMAND_ROTATION)
                self._current_command = TITLE_COMMAND_ROTATION[self._command_index]
                print(f"[Smite] Command rotation → {self._current_command}")
                # Re-apply the current title with the new command
                god = self.current_god["name"] if self.current_god else None
                await self._update_stream_title(god)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Smite] Command rotation error: {e}")
                await asyncio.sleep(30)

    def _get_current_song(self):
        """Get the currently playing song from the SR plugin, or None."""
        if self.bot and "songrequest" in self.bot.plugins:
            sr = self.bot.plugins["songrequest"]
            if sr.current_song:
                title = sr.current_song.get("title", "")
                artist = sr.current_song.get("artist", "")
                if title and artist:
                    return f"{title} - {artist}"
                return title or None
        return None

    async def _update_stream_title(self, god_name=None):
        """Update the Twitch stream title based on current god or lobby state.
        Placeholders:
          {god}     — current god name (only in god template)
          {command} — rotating featured command
          {record}  — daily win/loss record (e.g. "3-1")
          {song}    — currently playing song request
        Respects the TITLE_AUTO_UPDATE toggle and the 'auto_title' feature flag."""
        # Re-import these every call to pick up runtime updates from
        # set_title_template — module-level imports cache the value
        # at import time.
        from core import config as _cfg
        if not _cfg.TITLE_AUTO_UPDATE:
            return
        if not self.bot.is_feature_enabled("auto_title"):
            return

        try:
            if god_name:
                title = _cfg.TITLE_TEMPLATE_GOD.replace("{god}", god_name)
            else:
                title = _cfg.TITLE_TEMPLATE_LOBBY

            # Replace {command} with the current rotating command
            if "{command}" in title and self._current_command:
                title = title.replace("{command}", self._current_command)

            # Replace {record} with today's win/loss
            if "{record}" in title:
                title = title.replace("{record}", self.get_record_string())

            # Replace {song} with the currently playing song
            if "{song}" in title:
                song = self._get_current_song()
                title = title.replace("{song}", song or "No song playing")

            # Try the request, auto-refresh on 401 and retry once
            for attempt in range(2):
                async with self.session.patch(
                    "https://api.twitch.tv/helix/channels",
                    headers=await self._broadcaster_headers(),
                    json={
                        "broadcaster_id": TWITCH_OWNER_ID,
                        "title": title,
                    }
                ) as resp:
                    if resp.status == 204:
                        print(f"[Smite] Stream title updated: {title}")
                        return
                    elif resp.status == 401 and attempt == 0 and self._token_manager:
                        print("[Smite] Title update got 401, refreshing token...")
                        refreshed = await self._token_manager.handle_401("broadcaster")
                        if refreshed:
                            continue  # Retry with new token
                    body = await resp.text()
                    print(f"[Smite] Title update failed: {resp.status} {body}")
                    return
        except Exception as e:
            print(f"[Smite] Title update error: {e}")

    async def set_title_template(self, god_template=None, lobby_template=None):
        """Update title templates at runtime (from dashboard).
        Persists to data/title_templates.json so templates survive restarts."""
        import core.config as _cfg
        if god_template is not None:
            _cfg.TITLE_TEMPLATE_GOD = god_template
            print(f"[Smite] God title template: {god_template}")
        if lobby_template is not None:
            _cfg.TITLE_TEMPLATE_LOBBY = lobby_template
            print(f"[Smite] Lobby title template: {lobby_template}")
        self._save_title_templates()

    def _save_title_templates(self):
        """Persist current title templates to disk."""
        import core.config as _cfg
        try:
            path = Path(DATA_DIR) / "title_templates.json"
            path.write_text(json.dumps({
                "god": _cfg.TITLE_TEMPLATE_GOD,
                "lobby": _cfg.TITLE_TEMPLATE_LOBBY,
            }, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[Smite] Failed to save title templates: {e}")

    def _load_title_templates(self):
        """
        Re-read data/title_templates.json into core.config at on_ready.

        Note: core/config.py also loads this file at import time (the
        race-fix added in P1's title-template work), so by the time
        this method runs the values are already correct. Keeping it
        anyway for the runtime-reload code path and as a safety net
        if the JSON file appears AFTER bot startup.
        """
        import core.config as _cfg
        path = Path(DATA_DIR) / "title_templates.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if "god" in data:
                    _cfg.TITLE_TEMPLATE_GOD = data["god"]
                if "lobby" in data:
                    _cfg.TITLE_TEMPLATE_LOBBY = data["lobby"]
                print(f"[Smite] Loaded saved title templates")
            except Exception as e:
                print(f"[Smite] Failed to load title templates: {e}")

    def add_rotation_command(self, command):
        """Add a command to the rotation list at runtime."""
        if command not in TITLE_COMMAND_ROTATION:
            TITLE_COMMAND_ROTATION.append(command)
            print(f"[Smite] Added '{command}' to command rotation ({len(TITLE_COMMAND_ROTATION)} total)")
            return True
        return False

    def remove_rotation_command(self, command):
        """Remove a command from the rotation list at runtime."""
        if command in TITLE_COMMAND_ROTATION:
            TITLE_COMMAND_ROTATION.remove(command)
            # Adjust index if needed
            if TITLE_COMMAND_ROTATION:
                self._command_index = self._command_index % len(TITLE_COMMAND_ROTATION)
                self._current_command = TITLE_COMMAND_ROTATION[self._command_index]
            else:
                self._command_index = 0
                self._current_command = ""
            print(f"[Smite] Removed '{command}' from command rotation ({len(TITLE_COMMAND_ROTATION)} remaining)")
            return True
        return False

    def get_rotation_commands(self):
        """Return the current command rotation list and active command."""
        return {
            "commands": list(TITLE_COMMAND_ROTATION),
            "current": self._current_command,
            "interval": TITLE_COMMAND_ROTATION_INTERVAL,
        }
