"""
Smite 2 Plugin
===============
Live match detection via tracker.gg's internal API, god identification,
stat lookups, auto-predictions, and OBS god overlay integration.

Uses the undocumented tracker.gg API endpoints:
  - /profile/steam/{id}              — Full profile with god stats
  - /profile/steam/{id}/summary      — Lightweight rank/SR data
  - /matches/steam/{id}/live         — Live match with god + live KDA
  - /matches/{match_id}?authlevel=user — Post-game match detail

The live match endpoint is the key: it returns all 10 players with
their gods, teams, and live stats once a snapshot populates (~2-3 min
into the game).
"""

import asyncio
import aiohttp
import json
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from curl_cffi.requests import Session as CffiSession

from core.config import (
    SMITE2_PLATFORM, SMITE2_PLATFORM_ID, SMITE2_TRACKER_BASE,
    SMITE2_LIVE_URL, SMITE2_SUMMARY_URL, SMITE2_MATCH_URL,
    SMITE2_POLL_IDLE, SMITE2_POLL_SEARCHING, SMITE2_POLL_FOUND,
    SMITE2_CACHE_TTL, SMITE2_GOD_IMAGE_BASE, SMITE2_STATE_FILE,
    SMITE2_GOD_IMAGES_DIR, SMITE2_GOD_BG_DIR,
    OBS_SOURCE_GOD_IMAGE, OBS_SOURCE_GOD_BG,
    OBS_GOD_IMAGE_SCENE, OBS_GOD_IMAGE_GROUP,
    TWITCH_CHANNEL, TWITCH_CLIENT_ID, TWITCH_BOT_TOKEN,
    TWITCH_BROADCASTER_TOKEN, TWITCH_OWNER_ID,
    TITLE_AUTO_UPDATE, TITLE_TEMPLATE_GOD, TITLE_TEMPLATE_LOBBY,
    TITLE_FADE_DURATION, TITLE_COMMAND_ROTATION, TITLE_COMMAND_ROTATION_INTERVAL,
    DATA_DIR
)
from core.cache import Cache


class SmitePlugin:
    """
    Smite 2 tracker.gg integration.

    Poll states:
      IDLE       — Not in a match, poll every ~45s
      SEARCHING  — Match detected but no god data yet, poll every ~30s
      FOUND      — God data populated, poll every ~45s for stats updates
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://tracker.gg/smite2/",
        "Origin": "https://tracker.gg",
    }

    def __init__(self, token_manager=None):
        self.bot = None
        self.cache = Cache()
        self.session = None           # aiohttp session for Twitch API calls
        self._cffi_session = None     # curl_cffi session for tracker.gg (Cloudflare bypass)
        self._token_manager = token_manager  # Auto-refresh token manager
        self._cffi_executor = ThreadPoolExecutor(max_workers=2)

        # Match state
        self.is_in_match = False
        self.current_god = None          # {name, slug, imageUrl, team, stats}
        self.current_match_id = None
        self.match_players = []          # All 10 player segments
        self.match_start_time = None
        self._poll_state = "IDLE"        # IDLE | SEARCHING | FOUND

        # Prediction tracking
        self.prediction_id = None
        self._prediction_outcomes = {}   # {outcome_id: title}

        # Post-game
        self.last_match_result = None    # Stored after match ends

        # Background task
        self._poll_task = None

        # Command rotation for {command} title placeholder
        self._command_index = 0
        self._current_command = TITLE_COMMAND_ROTATION[0] if TITLE_COMMAND_ROTATION else ""
        self._command_rotation_task = None

        # Daily session record (auto-resets each day)
        self._session_wins = 0
        self._session_losses = 0
        self._session_date = None  # date string "YYYY-MM-DD"

        # Early god detection from portrait matcher (before tracker.gg responds)
        self._god_from_portrait = False  # True if portrait matcher already set the god

        # Guard against bounce-loop: if kill detector force-ended a match,
        # don't let tracker.gg re-enter the same match_id.
        self._force_ended_match_id = None

        # Event callbacks — other plugins can register here
        self._on_match_start_callbacks = []
        self._on_god_detected_callbacks = []
        self._on_match_end_callbacks = []
        self._on_match_result_callbacks = []  # Fired when win/loss is determined
        self._on_stats_update_callbacks = []

        # Persistence
        self._load_state()

    # === DATA PERSISTENCE ===

    def _load_state(self):
        if SMITE2_STATE_FILE.exists():
            try:
                with open(SMITE2_STATE_FILE) as f:
                    state = json.load(f)
                    self.last_match_result = state.get("last_match_result")
                    # Load daily record — auto-reset if it's a new day
                    today = datetime.now().strftime("%Y-%m-%d")
                    saved_date = state.get("session_date")
                    if saved_date == today:
                        self._session_wins = state.get("session_wins", 0)
                        self._session_losses = state.get("session_losses", 0)
                        self._session_date = today
                        print(f"[Smite] Restored daily record: {self._session_wins}-{self._session_losses}")
                    else:
                        self._session_date = today
                        print(f"[Smite] New day — daily record reset to 0-0")
            except Exception:
                pass

    def _save_state(self):
        state = {
            "last_match_result": self.last_match_result,
            "session_wins": self._session_wins,
            "session_losses": self._session_losses,
            "session_date": self._session_date or datetime.now().strftime("%Y-%m-%d"),
        }
        with open(SMITE2_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

    # === DAILY RECORD ===

    def _check_day_reset(self):
        """Reset record if it's a new day."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._session_date != today:
            self._session_wins = 0
            self._session_losses = 0
            self._session_date = today
            self._save_state()
            print(f"[Smite] New day detected — daily record reset to 0-0")

    def record_result(self, outcome):
        """Record a win or loss. Called from resolve_prediction or manually.
        outcome: 'win' or 'loss'"""
        self._check_day_reset()
        if outcome == "win":
            self._session_wins += 1
        elif outcome == "loss":
            self._session_losses += 1
        self._save_state()
        record = self.get_record_string()
        print(f"[Smite] Daily record updated: {record}")

        # Fire match result callbacks (economy plugin settlement, etc.)
        if self._on_match_result_callbacks:
            import asyncio
            result_data = {
                "outcome": outcome,
                "god": self.last_match_result.get("god") if self.last_match_result else None,
                "stats": self.last_match_result.get("stats") if self.last_match_result else {},
                "record": record,
            }
            asyncio.create_task(self._fire_event(self._on_match_result_callbacks, result_data))

        return record

    def get_record_string(self):
        """Get the current daily record as a string like '3-1'."""
        self._check_day_reset()
        return f"{self._session_wins}-{self._session_losses}"

    # === SETUP ===

    def setup(self, bot):
        self.bot = bot
        bot.register_command("god", self.cmd_god)
        bot.register_command("stats", self.cmd_stats)
        bot.register_command("rank", self.cmd_rank)
        bot.register_command("match", self.cmd_match)
        bot.register_command("winrate", self.cmd_winrate)
        bot.register_command("kda", self.cmd_kda)
        bot.register_command("damage", self.cmd_damage)
        bot.register_command("team", self.cmd_team)
        bot.register_command("lastmatch", self.cmd_lastmatch)
        bot.register_command("record", self.cmd_record)

    async def on_ready(self):
        # Load saved title templates (overrides config defaults if present)
        self._load_title_templates()

        # aiohttp for Twitch API (predictions, broadcaster ID)
        self.session = aiohttp.ClientSession()
        # curl_cffi for tracker.gg (bypasses Cloudflare TLS fingerprinting)
        self._cffi_session = CffiSession(
            impersonate="chrome124",
            headers=self.HEADERS,
        )
        if self.bot.is_feature_enabled("smite_tracking"):
            self._poll_task = asyncio.create_task(self._poll_loop())

        # Start command rotation task
        if TITLE_COMMAND_ROTATION and TITLE_AUTO_UPDATE:
            self._command_rotation_task = asyncio.create_task(self._command_rotation_loop())

        # Ensure god portrait is hidden on startup (clears leftovers from previous session)
        # Runs as background task because OBS plugin may not be connected yet
        asyncio.create_task(self._startup_hide_god_image())

    # === EVENT REGISTRATION ===

    def on_match_start(self, callback):
        """Register a callback for when a match is detected."""
        self._on_match_start_callbacks.append(callback)

    def on_god_detected(self, callback):
        """Register a callback for when the god is identified."""
        self._on_god_detected_callbacks.append(callback)

    def on_match_end(self, callback):
        """Register a callback for when a match ends."""
        self._on_match_end_callbacks.append(callback)

    def on_match_result(self, callback):
        """Register a callback for when win/loss is determined.
        callback receives: {'outcome': 'win'|'loss', 'god': god_info, 'record': '3-1'}"""
        self._on_match_result_callbacks.append(callback)

    async def _fire_event(self, callbacks, data=None):
        for cb in callbacks:
            try:
                await cb(data)
            except Exception as e:
                print(f"[Smite] Event callback error: {e}")

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

    # === API METHODS ===
    #
    # tracker.gg uses Cloudflare bot protection that checks TLS fingerprints.
    # We use curl_cffi (which impersonates Chrome's TLS handshake) via a
    # ThreadPoolExecutor so the sync HTTP calls don't block the event loop.

    def _cffi_get(self, url):
        """Synchronous GET via curl_cffi (runs in executor thread)."""
        resp = self._cffi_session.get(url)
        if resp.status_code == 200:
            return resp.json()
        return None

    async def _tracker_get(self, url):
        """Async wrapper: run curl_cffi GET in a thread."""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._cffi_executor, self._cffi_get, url
            )
        except Exception as e:
            print(f"[Smite] Tracker fetch error: {e}")
            return None

    async def _fetch_live_match(self):
        """
        GET /matches/steam/{id}/live
        Returns match data when in game, None when not.
        """
        url = f"{SMITE2_LIVE_URL}/{SMITE2_PLATFORM}/{SMITE2_PLATFORM_ID}/live"
        return await self._tracker_get(url)

    async def _fetch_profile(self, force_refresh=False):
        """
        GET /profile/steam/{id}[?forceCollect=true]
        Returns full profile with god stats, gamemodes, etc.
        """
        cache_key = "profile"
        if not force_refresh:
            cached = self.cache.get(cache_key, ttl=SMITE2_CACHE_TTL)
            if cached:
                return cached

        url = f"{SMITE2_TRACKER_BASE}/{SMITE2_PLATFORM}/{SMITE2_PLATFORM_ID}"
        if force_refresh:
            url += "?forceCollect=true"

        data = await self._tracker_get(url)
        if data:
            self.cache.set(cache_key, data)
        return data

    async def _fetch_summary(self):
        """
        GET /profile/steam/{id}/summary
        Lightweight rank/SR data.
        """
        cache_key = "summary"
        cached = self.cache.get(cache_key, ttl=SMITE2_CACHE_TTL)
        if cached:
            return cached

        url = f"{SMITE2_SUMMARY_URL}/{SMITE2_PLATFORM}/{SMITE2_PLATFORM_ID}/summary"
        data = await self._tracker_get(url)
        if data:
            self.cache.set(cache_key, data)
        return data

    async def _fetch_match_detail(self, match_id):
        """
        GET /matches/{match_id}?authlevel=user
        Full post-game match data.
        """
        url = f"{SMITE2_MATCH_URL}/{match_id}?authlevel=user"
        return await self._tracker_get(url)

    # === DATA EXTRACTION ===

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

        god_name = meta.get("godName") or meta.get("god")
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
                "god": meta.get("godName", "Unknown"),
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
                if meta.get("name", "").lower() == god_name.lower():
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

                # Notify chat
                if ended_god:
                    await self.bot.send_chat(
                        f"Game over! Was playing {ended_god['name']} "
                        f"({ended_god['stats']['kills']}/"
                        f"{ended_god['stats']['deaths']}/"
                        f"{ended_god['stats']['assists']})"
                    )

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

    # === OBS GOD IMAGE & BACKGROUND ===

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

    # === PREDICTIONS ===

    async def _twitch_headers(self):
        """Headers for Twitch Helix API calls using the bot token."""
        if self._token_manager:
            return await self._token_manager.get_bot_headers()
        return {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {TWITCH_BOT_TOKEN}",
            "Content-Type": "application/json",
        }

    async def _broadcaster_headers(self):
        """Headers for Twitch Helix API calls that require broadcaster auth.
        Used for channel title updates, predictions, etc.
        Falls back to bot token if no broadcaster token is configured."""
        if self._token_manager:
            return await self._token_manager.get_broadcaster_headers()
        token = TWITCH_BROADCASTER_TOKEN or TWITCH_BOT_TOKEN
        return {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # === STREAM TITLE ===

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
        if not TITLE_AUTO_UPDATE:
            return
        if not self.bot.is_feature_enabled("auto_title"):
            return

        try:
            if god_name:
                title = TITLE_TEMPLATE_GOD.replace("{god}", god_name)
            else:
                title = TITLE_TEMPLATE_LOBBY

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
        global TITLE_TEMPLATE_GOD, TITLE_TEMPLATE_LOBBY
        if god_template is not None:
            TITLE_TEMPLATE_GOD = god_template
            _cfg.TITLE_TEMPLATE_GOD = god_template
            print(f"[Smite] God title template: {god_template}")
        if lobby_template is not None:
            TITLE_TEMPLATE_LOBBY = lobby_template
            _cfg.TITLE_TEMPLATE_LOBBY = lobby_template
            print(f"[Smite] Lobby title template: {lobby_template}")
        self._save_title_templates()

    def _save_title_templates(self):
        """Persist current title templates to disk."""
        import json
        try:
            path = Path(DATA_DIR) / "title_templates.json"
            path.write_text(json.dumps({
                "god": TITLE_TEMPLATE_GOD,
                "lobby": TITLE_TEMPLATE_LOBBY,
            }, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[Smite] Failed to save title templates: {e}")

    def _load_title_templates(self):
        """Load saved title templates from disk (overrides config defaults)."""
        import json
        import core.config as _cfg
        global TITLE_TEMPLATE_GOD, TITLE_TEMPLATE_LOBBY
        path = Path(DATA_DIR) / "title_templates.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if "god" in data:
                    TITLE_TEMPLATE_GOD = data["god"]
                    _cfg.TITLE_TEMPLATE_GOD = data["god"]
                if "lobby" in data:
                    TITLE_TEMPLATE_LOBBY = data["lobby"]
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

    # === PREDICTIONS ===

    async def _create_prediction(self):
        """Create a Twitch prediction for the match."""
        try:
            payload = {
                "broadcaster_id": TWITCH_OWNER_ID,
                "title": "Will Hatmaster win this game?",
                "outcomes": [
                    {"title": "Win"},
                    {"title": "Loss"}
                ],
                "prediction_window": 120,
            }
            for attempt in range(2):
                async with self.session.post(
                    "https://api.twitch.tv/helix/predictions",
                    headers=await self._broadcaster_headers(),
                    json=payload
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pred = data["data"][0]
                        self.prediction_id = pred["id"]
                        self._prediction_outcomes = {
                            o["id"]: o["title"] for o in pred.get("outcomes", [])
                        }
                        print(f"[Smite] Prediction created: {self.prediction_id}")
                        await self.bot.send_chat(
                            "Place your bets! Will Hatmaster win? "
                            "Prediction closes in 2 minutes."
                        )
                        break
                    elif resp.status == 401 and attempt == 0 and self._token_manager:
                        print("[Smite] Prediction create got 401, refreshing token...")
                        if await self._token_manager.handle_401("broadcaster"):
                            continue
                    body = await resp.text()
                    print(f"[Smite] Prediction creation failed: {resp.status} {body}")
                    break
        except Exception as e:
            print(f"[Smite] Prediction error: {e}")

    async def resolve_prediction(self, outcome):
        """Resolve prediction. outcome = 'win' or 'loss'.
        Also records the result in the daily session record."""
        # Always record the result, even if prediction has expired
        record = self.record_result(outcome)

        if not self.prediction_id:
            print(f"[Smite] No active prediction — recorded {outcome} anyway ({record})")
            # Still update the title with the new record
            god = self.current_god["name"] if self.current_god else None
            await self._update_stream_title(god)
            return

        try:
            # Find the matching outcome ID
            winning_id = None
            target = "Win" if outcome == "win" else "Loss"
            for oid, title in self._prediction_outcomes.items():
                if title == target:
                    winning_id = oid
                    break

            if not winning_id:
                print(f"[Smite] Could not find outcome ID for '{outcome}'")
                return

            payload = {
                "broadcaster_id": TWITCH_OWNER_ID,
                "id": self.prediction_id,
                "status": "RESOLVED",
                "winning_outcome_id": winning_id,
            }
            for attempt in range(2):
                async with self.session.patch(
                    "https://api.twitch.tv/helix/predictions",
                    headers=await self._broadcaster_headers(),
                    json=payload
                ) as resp:
                    if resp.status == 200:
                        result_text = "Hatmaster won!" if outcome == "win" else "Hatmaster lost!"
                        await self.bot.send_chat(
                            f"Prediction resolved! {result_text} (Today: {record})"
                        )
                        print(f"[Smite] Prediction resolved: {outcome} ({record})")
                        self.prediction_id = None
                        self._prediction_outcomes = {}
                        # Update title with new record
                        god = self.current_god["name"] if self.current_god else None
                        await self._update_stream_title(god)
                        break
                    elif resp.status == 401 and attempt == 0 and self._token_manager:
                        print("[Smite] Prediction resolve got 401, refreshing token...")
                        if await self._token_manager.handle_401("broadcaster"):
                            continue
                    body = await resp.text()
                    print(f"[Smite] Resolve failed: {resp.status} {body}")
                    break
        except Exception as e:
            print(f"[Smite] Resolve error: {e}")

    # === CHAT COMMANDS ===

    async def cmd_god(self, message, args, whisper=False):
        """!god — Show current god (live) or look up god stats."""
        if not args and self.current_god:
            # Live god info
            g = self.current_god
            s = g["stats"]
            await self.bot.send_reply(
                message,
                f"Currently playing {g['name']} ({g['team'].title()} side) | "
                f"KDA: {s['kills']}/{s['deaths']}/{s['assists']} | "
                f"Gold: {s['gold']:,} ({s['gpm']} GPM)",
                whisper
            )
            return

        if not args and not self.current_god:
            await self.bot.send_reply(
                message,
                "Not in a match right now. Use !god <name> to look up a god.",
                whisper
            )
            return

        # Look up specific god stats from profile
        god_name = args.strip()
        data = await self._fetch_profile()
        if not data:
            await self.bot.send_reply(message, "Couldn't fetch stats right now.", whisper)
            return

        god = self._get_god_stats(data, god_name)
        if not god:
            await self.bot.send_reply(
                message, f"No stats found for {god_name}.", whisper
            )
            return

        stats = god.get("stats", {})
        name = god.get("metadata", {}).get("name", god_name)
        kills = self._stat_display(stats, "kills")
        deaths = self._stat_display(stats, "deaths")
        assists = self._stat_display(stats, "assists")
        matches = self._stat_display(stats, "matchesPlayed")
        winrate = self._stat_display(stats, "matchesWinPct")

        await self.bot.send_reply(
            message,
            f"{name}: {matches} games | {winrate} WR | K/D/A: {kills}/{deaths}/{assists}",
            whisper
        )

    async def cmd_stats(self, message, args, whisper=False):
        """!stats — Ranked conquest overview."""
        data = await self._fetch_profile()
        if not data:
            await self.bot.send_reply(message, "Couldn't fetch stats right now.", whisper)
            return

        stats = self._get_gamemode_stats(data)
        if not stats:
            await self.bot.send_reply(message, "No ranked conquest stats found.", whisper)
            return

        kills = self._stat_display(stats, "kills")
        deaths = self._stat_display(stats, "deaths")
        assists = self._stat_display(stats, "assists")
        winrate = self._stat_display(stats, "matchesWinPct")
        matches = self._stat_display(stats, "matchesPlayed")
        kda = self._stat_display(stats, "kdaRatio")

        await self.bot.send_reply(
            message,
            f"Ranked Conquest: {matches} games | {winrate} WR | "
            f"K/D/A: {kills}/{deaths}/{assists} | KDA: {kda}",
            whisper
        )

    async def cmd_rank(self, message, args, whisper=False):
        """!rank — Current SR and rank tier."""
        # Try live match first (most accurate SR)
        if self.current_god and self.current_god.get("rank"):
            r = self.current_god["rank"]
            await self.bot.send_reply(
                message, f"Rank: {r['tier']} (SR: {r['sr']})", whisper
            )
            return

        # Fall back to profile
        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch rank.", whisper)
            return

        matches = self._stat_display(stats, "matchesPlayed")
        wins = self._stat_display(stats, "matchesWon")
        losses = self._stat_display(stats, "matchesLost")

        await self.bot.send_reply(
            message, f"Ranked: {matches} games | {wins}W {losses}L", whisper
        )

    async def cmd_match(self, message, args, whisper=False):
        """!match — Check if currently in a match."""
        if self.is_in_match:
            if self.current_god:
                g = self.current_god
                s = g["stats"]
                duration = int(time.time() - self.match_start_time) if self.match_start_time else 0
                mins = duration // 60
                await self.bot.send_reply(
                    message,
                    f"In a match ({mins} min) playing {g['name']} | "
                    f"KDA: {s['kills']}/{s['deaths']}/{s['assists']}",
                    whisper
                )
            else:
                await self.bot.send_reply(
                    message, "In a match, still detecting god...", whisper
                )
        else:
            await self.bot.send_reply(message, "No active match right now.", whisper)

    async def cmd_winrate(self, message, args, whisper=False):
        """!winrate — Ranked win rate."""
        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch stats.", whisper)
            return
        wr = self._stat_display(stats, "matchesWinPct")
        await self.bot.send_reply(message, f"Win rate: {wr}", whisper)

    async def cmd_kda(self, message, args, whisper=False):
        """!kda — KDA ratio."""
        # Live KDA if in match
        if self.current_god:
            s = self.current_god["stats"]
            await self.bot.send_reply(
                message,
                f"Live KDA ({self.current_god['name']}): "
                f"{s['kills']}/{s['deaths']}/{s['assists']}",
                whisper
            )
            return

        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch stats.", whisper)
            return
        kda = self._stat_display(stats, "kdaRatio")
        kad = self._stat_display(stats, "kadRatio")
        await self.bot.send_reply(message, f"KDA: {kda} | KA/D: {kad}", whisper)

    async def cmd_damage(self, message, args, whisper=False):
        """!damage — Damage stats."""
        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch stats.", whisper)
            return
        total = self._stat_display(stats, "damage")
        per_match = self._stat_display(stats, "damagePerMatch")
        per_min = self._stat_display(stats, "damagePerMinute")
        matches = self._stat_display(stats, "matchesPlayed")
        await self.bot.send_reply(
            message,
            f"Total damage: {total} across {matches} games | "
            f"{per_match}/game | {per_min}/min",
            whisper
        )

    async def cmd_team(self, message, args, whisper=False):
        """!team — Show all players on your team in the current match."""
        if not self.is_in_match or not self.match_players:
            await self.bot.send_reply(message, "No match data available.", whisper)
            return

        my_team = "unknown"
        for p in self.match_players:
            if p.get("is_me"):
                my_team = p.get("team", "unknown")
                break

        teammates = [p for p in self.match_players if p["team"] == my_team]
        team_str = " | ".join(
            f"{p['god']} ({p['kills']}/{p['deaths']}/{p['assists']})"
            for p in teammates
        )
        await self.bot.send_reply(
            message, f"{my_team.title()} team: {team_str}", whisper
        )

    async def cmd_lastmatch(self, message, args, whisper=False):
        """!lastmatch — Show results from the last completed match."""
        if not self.last_match_result:
            await self.bot.send_reply(message, "No recent match data.", whisper)
            return

        r = self.last_match_result
        s = r.get("stats", {})
        await self.bot.send_reply(
            message,
            f"Last game: {r['god']} "
            f"{s.get('kills', 0)}/{s.get('deaths', 0)}/{s.get('assists', 0)}",
            whisper
        )

    async def cmd_record(self, message, args, whisper=False):
        """!record — Show today's win/loss record."""
        record = self.get_record_string()
        total = self._session_wins + self._session_losses
        if total == 0:
            await self.bot.send_reply(message, "No games played today yet!", whisper)
        else:
            pct = round(self._session_wins / total * 100) if total > 0 else 0
            await self.bot.send_reply(
                message,
                f"Today's record: {record} ({pct}% WR across {total} games)",
                whisper
            )

    # === CLEANUP ===

    async def cleanup(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self._command_rotation_task:
            self._command_rotation_task.cancel()
        if self.session:
            await self.session.close()
        if self._cffi_session:
            self._cffi_session.close()
        self._cffi_executor.shutdown(wait=False)
        self._save_state()
