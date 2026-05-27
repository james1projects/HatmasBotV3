"""
plugins/smite/match_state.py
============================
Live-match polling, god detection, and state transitions.

The poll loop runs in a background task started by `on_ready()` and
hits tracker.gg's live-match endpoint at adaptive intervals:

  IDLE       — Not in a match, poll every SMITE2_POLL_IDLE       (45s)
  SEARCHING  — Match detected but no god data yet, poll every
               SMITE2_POLL_SEARCHING (30s)
  FOUND      — God identified, poll every SMITE2_POLL_FOUND      (45s)
               (just for stat updates)

Two early-action helpers also live here:

  * `force_end_match()` — called by the kill detector when it sees
    non-gameplay screens; clears OBS portrait, fires match-end
    callbacks, and bounce-guards against tracker.gg re-entering the
    same match_id immediately afterward.
  * `set_god_from_portrait(name)` — called by the kill detector's
    portrait matcher 2-5 minutes BEFORE tracker.gg's API responds
    with god data; sets the OBS portrait + title immediately.

Plus the static segment-extraction helpers (`_find_my_segment`,
`_extract_god_info`, `_extract_all_players`, `_stat_val`,
`_stat_display`) used by the poll loop and history parsers.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from core.config import (
    SMITE2_PLATFORM_ID, SMITE2_GOD_IMAGE_BASE,
    SMITE2_POLL_IDLE, SMITE2_POLL_SEARCHING, SMITE2_POLL_FOUND,
)
from plugins.smite.history import _clean_god_name


class _MatchStateMixin:
    """
    Mixed into SmitePlugin. Reads/writes:
      self.is_in_match, self.current_god, self.current_match_id,
      self.match_players, self.match_start_time, self._poll_state,
      self._god_from_portrait, self._force_ended_match_id,
      self.prediction_id, self.last_match_result,
      self._on_match_start_callbacks, self._on_god_detected_callbacks,
      self._on_match_end_callbacks, self._on_stats_update_callbacks,
      self.bot
    All initialized in SmitePlugin.__init__.
    """

    # === EARLY MATCH-END DETECTION (from kill detector) ===

    async def force_end_match(self):
        """
        Called by the kill detector when it detects non-gameplay screens
        (lobby, god select, results) before tracker.gg's API drops the
        live match.  Clears the god portrait from OBS immediately so it
        doesn't linger for minutes while tracker.gg catches up.

        Also handles jungle practice / custom games where tracker.gg
        never detects a match (is_in_match stays False) but the portrait
        was still set via set_god_from_portrait.
        """
        if not self.is_in_match and not self._god_from_portrait:
            return  # Nothing to clean up

        print("[Smite] Force-ending match (kill detector saw non-gameplay)")

        ended_god = self.current_god

        # Remember which match we force-ended so tracker.gg doesn't re-enter it
        if self.current_match_id:
            self._force_ended_match_id = self.current_match_id

        self.is_in_match = False
        self.current_god = None
        self._god_from_portrait = False
        self.match_players = []
        self._poll_state = "IDLE"

        # Clear OBS portrait and overlay
        self._update_overlay_state()
        await self._clear_god_image()

        # Revert stream title to lobby default
        await self._update_stream_title(None)

        # Fire match end callbacks (voiceline clear, prediction prompt, etc.)
        await self._fire_event(self._on_match_end_callbacks, {
            "match_id": self.current_match_id,
            "god": ended_god,
            "source": "killdetector",
        })

        # Note: we intentionally skip the post-game data fetch and
        # win/loss notification here — the normal tracker.gg poll will
        # no-op when it sees is_in_match is already False.  The post-game
        # data fetch still happens from the _check_live_match path if
        # tracker.gg returned data between the KD trigger and the next poll.

    # === EARLY GOD DETECTION (from portrait matcher) ===

    async def set_god_from_portrait(self, god_name):
        """
        Called by the kill detector's portrait matcher when it identifies
        the god from the in-game portrait.  This fires 2-5 minutes before
        the tracker.gg API returns god data, and also works in modes
        tracker.gg doesn't cover (jungle practice, custom games).

        Sets the OBS god portrait image immediately.
        When tracker.gg eventually returns the same god, the SEARCHING→FOUND
        transition will skip the re-announcement but still populate the full
        match data (team, stats, players).  If tracker.gg returns a different
        god (misidentification), the portrait is corrected automatically.

        Note: no is_in_match guard — the kill detector's gameplay screen
        check already ensures we're in actual gameplay before calling this.
        """
        if self._god_from_portrait:
            return  # Already set from portrait this match

        self._god_from_portrait = True

        # Build a minimal god info dict (no stats yet — those come from tracker.gg)
        god_slug = god_name.lower().replace(" ", "-").replace("'", "")
        god_info = {
            "name": god_name,
            "slug": god_slug,
            "imageUrl": f"{SMITE2_GOD_IMAGE_BASE}/{god_slug}.jpg",
            "team": "unknown",
            "stats": {"kills": 0, "deaths": 0, "assists": 0,
                      "gold": 0, "gpm": 0, "damage": 0},
        }

        self.current_god = god_info
        print(f"[Smite] Early god detection from portrait: {god_name}")

        # Fire god detected callbacks (godrequest auto-complete, etc.)
        await self._fire_event(self._on_god_detected_callbacks, god_info)

        # Swap OBS god image source immediately
        await self._set_god_image(god_name, team=None)

        # Update stream title with god name
        await self._update_stream_title(god_name)

        # Update overlay state
        self._update_overlay_state()

    # === LIVE-DATA EXTRACTION ===

    def _find_my_segment(self, live_data):
        """Find our player segment in the live match data by Steam ID."""
        if not live_data or "data" not in live_data:
            return None

        segments = live_data["data"].get("segments", [])
        for seg in segments:
            attrs = seg.get("attributes", {})
            if attrs.get("platformUserIdentifier") == SMITE2_PLATFORM_ID:
                return seg
        return None

    def _extract_god_info(self, segment):
        """Extract god name, slug, image, team, and stats from a player segment."""
        if not segment:
            return None

        meta = segment.get("metadata", {})
        stats_raw = segment.get("stats", {})

        god_name = _clean_god_name(meta.get("godName") or meta.get("god"))
        if not god_name:
            return None

        god_slug = (meta.get("god") or god_name.lower().replace(" ", "-")
                     .replace("'", ""))

        return {
            "name": god_name,
            "slug": god_slug,
            "imageUrl": meta.get("godImageUrl",
                                  f"{SMITE2_GOD_IMAGE_BASE}/{god_slug}.jpg"),
            "team": meta.get("teamId", "unknown"),
            "partyId": meta.get("partyId"),
            "stats": {
                "kills": self._stat_val(stats_raw, "kills"),
                "deaths": self._stat_val(stats_raw, "deaths"),
                "assists": self._stat_val(stats_raw, "assists"),
                "gold": self._stat_val(stats_raw, "goldEarned"),
                "gpm": self._stat_val(stats_raw, "goldPerMinute"),
                "damage": self._stat_val(stats_raw, "damage"),
            },
            "rank": {
                "tier": self._stat_display(stats_raw, "skillRating"),
                "sr": self._stat_val(stats_raw, "skillRating"),
                "iconUrl": (stats_raw.get("skillRating", {})
                            .get("metadata", {}).get("iconUrl")),
            },
        }

    def _extract_all_players(self, live_data):
        """Extract all 10 player segments with their god info."""
        if not live_data or "data" not in live_data:
            return []

        players = []
        for seg in live_data["data"].get("segments", []):
            meta = seg.get("metadata", {})
            stats_raw = seg.get("stats", {})
            attrs = seg.get("attributes", {})

            players.append({
                "name": meta.get("platformUserHandle",
                                  attrs.get("platformUserIdentifier", "?")),
                "god": _clean_god_name(meta.get("godName")) or "Unknown",
                "godSlug": meta.get("god", ""),
                "team": meta.get("teamId", "unknown"),
                "partyId": meta.get("partyId"),
                "kills": self._stat_val(stats_raw, "kills"),
                "deaths": self._stat_val(stats_raw, "deaths"),
                "assists": self._stat_val(stats_raw, "assists"),
                "sr": self._stat_val(stats_raw, "skillRating"),
                "is_me": attrs.get("platformUserIdentifier") == SMITE2_PLATFORM_ID,
            })
        return players

    def _get_gamemode_stats(self, data, gamemode="conquest-ranked"):
        """Extract stats for a specific gamemode from profile data."""
        if not data or "data" not in data:
            return None
        for seg in data["data"].get("segments", []):
            if seg.get("type") == "gamemode":
                attrs = seg.get("attributes", {})
                if attrs.get("gamemode") == gamemode:
                    return seg.get("stats", {})
        return None

    def _get_god_stats(self, data, god_name):
        """Extract stats for a specific god from profile data."""
        if not data or "data" not in data:
            return None
        for seg in data["data"].get("segments", []):
            if seg.get("type") == "god":
                meta = seg.get("metadata", {})
                seg_name = _clean_god_name(meta.get("name", "")) or ""
                if seg_name.lower() == god_name.lower():
                    return seg
        return None

    @staticmethod
    def _stat_val(stats, key):
        """Get numeric value from a tracker.gg stat object."""
        stat = stats.get(key, {})
        return stat.get("value", 0)

    @staticmethod
    def _stat_display(stats, key):
        """Get display string from a tracker.gg stat object."""
        stat = stats.get(key, {})
        return stat.get("displayValue", "N/A")

    # === POLL LOOP ===

    async def _poll_loop(self):
        """
        Background poll loop with adaptive intervals:
          IDLE       — Not in match, poll every SMITE2_POLL_IDLE (45s)
          SEARCHING  — In match, no god data yet, poll every SMITE2_POLL_SEARCHING (30s)
          FOUND      — God identified, poll every SMITE2_POLL_FOUND (45s) for stat updates
        """
        print("[Smite] Poll loop started")

        while True:
            try:
                if not self.bot.is_feature_enabled("smite_tracking"):
                    await asyncio.sleep(10)
                    continue

                await self._check_live_match()

            except Exception as e:
                print(f"[Smite Poll] Error: {e}")

            # Adaptive sleep
            if self._poll_state == "SEARCHING":
                await asyncio.sleep(SMITE2_POLL_SEARCHING)
            elif self._poll_state == "FOUND":
                await asyncio.sleep(SMITE2_POLL_FOUND)
            else:
                await asyncio.sleep(SMITE2_POLL_IDLE)

    async def _check_live_match(self):
        """Core poll logic: detect match start/end and god identification."""
        live_data = await self._fetch_live_match()

        if live_data and "data" in live_data:
            # We're in a match
            match_attrs = live_data["data"].get("attributes", {})
            match_id = match_attrs.get("id")

            if not self.is_in_match:
                # Guard: don't re-enter a match that was just force-ended
                # by the kill detector.  Wait for tracker.gg to drop it.
                if match_id and match_id == self._force_ended_match_id:
                    return

                # === MATCH JUST STARTED ===
                self._force_ended_match_id = None  # Clear guard for new match
                self.is_in_match = True
                self.current_match_id = match_id
                self.match_start_time = time.time()
                self._poll_state = "SEARCHING"
                print(f"[Smite] Match detected! ID: {match_id}")

                # Fire match start event
                await self._fire_event(self._on_match_start_callbacks, {
                    "match_id": match_id,
                })

                # Auto-create prediction
                if self.bot.is_feature_enabled("predictions"):
                    await self._create_prediction()

                # Auto scene switch
                if (self.bot.is_feature_enabled("auto_scene_switch")
                        and "obs" in self.bot.plugins):
                    await self.bot.plugins["obs"].switch_to_game()

            # Try to extract god data from our segment
            my_segment = self._find_my_segment(live_data)
            god_info = self._extract_god_info(my_segment) if my_segment else None

            if god_info and god_info["name"]:
                if self._poll_state == "SEARCHING":
                    # === GOD JUST IDENTIFIED (from tracker.gg API) ===
                    self._poll_state = "FOUND"

                    # Save portrait god name BEFORE overwriting current_god
                    portrait_god = self.current_god.get("name", "") if self.current_god else ""

                    self.current_god = god_info
                    self.match_players = self._extract_all_players(live_data)

                    if self._god_from_portrait:
                        # Portrait matcher already set the image — check if tracker.gg agrees
                        if portrait_god.lower() != god_info["name"].lower():
                            # Tracker.gg disagrees with portrait — correct it
                            print(f"[Smite] Tracker.gg CORRECTED god: "
                                  f"{portrait_god} → {god_info['name']} "
                                  f"({god_info['team']} side)")

                            # Re-set OBS portrait to the correct god
                            await self._set_god_image(god_info["name"],
                                                      team=god_info.get("team"))

                            # Update stream title with corrected god name
                            await self._update_stream_title(god_info["name"])

                            # Fire god detected callbacks (voicelines, etc.)
                            await self._fire_event(
                                self._on_god_detected_callbacks, god_info)
                        else:
                            print(f"[Smite] Tracker.gg confirmed god: {god_info['name']} "
                                  f"({god_info['team']} side) — portrait already set")

                            # Update background with correct team color now that we know it
                            await self._set_god_background(team=god_info.get("team"))
                    else:
                        # Normal flow — portrait matcher didn't catch it
                        print(f"[Smite] God detected: {god_info['name']} "
                              f"({god_info['team']} side)")

                        # Fire god detected event
                        await self._fire_event(self._on_god_detected_callbacks, god_info)

                        # Swap OBS god image source to the matching funny image
                        # Pass team so the background matches Chaos/Order side
                        await self._set_god_image(god_info["name"], team=god_info.get("team"))

                        # Update stream title with god name
                        await self._update_stream_title(god_info["name"])

                    # Update webserver state for overlay (always, to get stats)
                    self._update_overlay_state()

                    # Authoritative match-confirmation signal. Fires
                    # exactly once per match (SEARCHING -> FOUND gates
                    # this whole block). Tracker.gg has verified a
                    # real match with a real match_id, so anything
                    # that touches money / shares / persisted state
                    # (i.e., the economy plugin) is safe to act on
                    # this. Portrait-only detections never reach here.
                    if self.current_match_id:
                        await self._fire_event(
                            self._on_match_confirmed_callbacks,
                            {
                                "match_id": self.current_match_id,
                                "god": god_info["name"],
                                "team": god_info.get("team"),
                            },
                        )

                elif self._poll_state == "FOUND":
                    # === UPDATE LIVE STATS ===
                    old_stats = self.current_god.get("stats", {}) if self.current_god else {}
                    self.current_god = god_info
                    self.match_players = self._extract_all_players(live_data)

                    # Update overlay with fresh stats
                    self._update_overlay_state()

                    # Fire stats update if KDA changed
                    new_stats = god_info.get("stats", {})
                    if (new_stats.get("kills") != old_stats.get("kills") or
                            new_stats.get("deaths") != old_stats.get("deaths") or
                            new_stats.get("assists") != old_stats.get("assists")):
                        await self._fire_event(self._on_stats_update_callbacks, god_info)

        else:
            # Not in a match
            if self.is_in_match:
                # === MATCH JUST ENDED ===
                print("[Smite] Match ended!")
                ended_god = self.current_god
                ended_match_id = self.current_match_id

                self.is_in_match = False
                self.current_god = None
                self._god_from_portrait = False  # Reset for next match
                self.match_players = []
                self._poll_state = "IDLE"

                # Clear overlay and OBS god image (fade out)
                self._update_overlay_state()
                await self._clear_god_image()

                # Revert stream title to lobby default
                await self._update_stream_title(None)

                # No "Game over!" chat announcement - removed per
                # broadcaster preference. The information still lives
                # in the match-end overlay + the dashboard. If you ever
                # want it back, restore the self.bot.send_chat(...) call
                # here using ended_god['name'] + ended_god['stats'].

                # Fetch post-game data
                if ended_match_id:
                    await asyncio.sleep(10)  # Give tracker.gg time to process
                    match_detail = await self._fetch_match_detail(ended_match_id)
                    if match_detail:
                        self.last_match_result = {
                            "match_id": ended_match_id,
                            "god": ended_god["name"] if ended_god else "Unknown",
                            "stats": ended_god["stats"] if ended_god else {},
                            "ended_at": datetime.now().isoformat(),
                        }
                        self._save_state()
                        print(f"[Smite] Post-game data saved for match {ended_match_id}")

                # Fire match end event
                await self._fire_event(self._on_match_end_callbacks, {
                    "match_id": ended_match_id,
                    "god": ended_god,
                })

                # Auto scene switch back
                if (self.bot.is_feature_enabled("auto_scene_switch")
                        and "obs" in self.bot.plugins):
                    await self.bot.plugins["obs"].switch_to_lobby()

                # Prompt prediction resolve
                if self.prediction_id:
                    await self.bot.send_chat(
                        "Match ended! Resolve the prediction in the control panel."
                    )
