"""
plugins/smite/plugin.py
=======================
The actual SmitePlugin class — only thing main.py imports from this
package.

Owns:
  * `__init__` (instance state)
  * `setup`    (chat-command registration)
  * `on_ready` (one-time async startup: aiohttp + curl_cffi sessions,
               poll-loop start, command-rotation start, startup OBS hide)
  * `cleanup`  (cancel tasks, close sessions, persist state)
  * Event-callback registration (on_match_start, on_god_detected,
    on_match_end, on_match_result) and the `_fire_event` dispatcher.

All other behavior comes from the per-concern mixins in this package:
state / tracker_client / history / match_state / obs_portrait /
title / predictions / twitch_api / commands.
"""

from __future__ import annotations

import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor

from curl_cffi.requests import Session as CffiSession

from core.cache import Cache
from core.config import TITLE_COMMAND_ROTATION

from .commands import _CommandsMixin
from .history import _HistoryMixin
from .match_state import _MatchStateMixin
from .obs_portrait import _OBSPortraitMixin
from .predictions import _PredictionsMixin
from .state import _StateMixin
from .title import _TitleMixin
from .tracker_client import _TrackerClientMixin
from .twitch_api import _TwitchAPIMixin


class SmitePlugin(
    # Foundational mixins first — state persistence, HTTP clients,
    # match-data extraction, headers. Everything else builds on these.
    _StateMixin,
    _TwitchAPIMixin,
    _TrackerClientMixin,
    _MatchStateMixin,
    _HistoryMixin,
    # OBS portrait management — relies on match state for current_god,
    # writes to the OBS plugin via self.bot.plugins["obs"].
    _OBSPortraitMixin,
    # Title automation + predictions — call into _TwitchAPIMixin and
    # _StateMixin (record_result fires the on_match_result callbacks).
    _TitleMixin,
    _PredictionsMixin,
    # Operator-facing surface: chat commands. Sit on top of everything.
    _CommandsMixin,
):
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
        # _on_match_confirmed_callbacks fires ONLY from the tracker.gg
        # poll loop's SEARCHING -> FOUND transition, only when there's
        # a real match_id. Anything that touches money / shares /
        # persisted state (the economy plugin) subscribes here, NOT
        # to _on_god_detected_callbacks (which also fires for portrait-
        # only detections in jungle practice / custom games).
        # Payload shape: {match_id: str, god: str, team: str}
        self._on_match_confirmed_callbacks = []
        self._on_match_end_callbacks = []
        self._on_match_result_callbacks = []  # Fired when win/loss is determined
        self._on_stats_update_callbacks = []

        # Persistence — _StateMixin._load_state must be available before
        # this point, which works because mixins are inherited.
        self._load_state()

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
        # Re-load saved title templates from disk (overrides config defaults
        # if present — also runs at config import time, this is a safety net).
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
        from core.config import TITLE_AUTO_UPDATE
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

    def on_match_confirmed(self, callback):
        """
        Register a callback for the authoritative match-start signal.

        Fires only from the tracker.gg poll loop's SEARCHING -> FOUND
        transition, only when there's a real match_id. Use this for
        anything that should ONLY run on a confirmed real match -
        e.g., the economy plugin's match-start dividend and live-tick
        priming. Portrait-matcher detections in jungle practice /
        custom games / lobby false-positives never fire this callback.

        Payload: {\"match_id\": str, \"god\": str, \"team\": str}
        """
        self._on_match_confirmed_callbacks.append(callback)

    def on_match_end(self, callback):
        """Register a callback for when a match ends."""
        self._on_match_end_callbacks.append(callback)

    def on_match_result(self, callback):
        """Register a callback for when win/loss is determined.
        Fires when resolve_prediction() or record_result() is called.
        Used by economy plugin for match settlement (price changes)."""
        self._on_match_result_callbacks.append(callback)

    async def _fire_event(self, callbacks, data=None):
        for cb in callbacks:
            try:
                await cb(data)
            except Exception as e:
                print(f"[Smite Event] {e}")

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
