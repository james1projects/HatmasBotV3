"""
plugins/smite/state.py
======================
Persistence + daily session record.

The smite plugin caches a small state file at SMITE2_STATE_FILE
(default `data/smite_state.json`) covering:

  * last_match_result   — the most recent settled match (god, KDA, etc.)
                          so !lastmatch and the dashboard can show it
                          across bot restarts.
  * session_wins /
    session_losses /
    session_date        — daily W-L record, auto-resets at midnight
                          based on `session_date` mismatch.

`record_result(outcome)` is the public entry point — called from the
prediction resolver and the dashboard's manual W/L button. It
increments the appropriate counter, persists, and fires any
on_match_result callbacks so downstream plugins (economy settlement)
react to the win/loss.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from core.config import SMITE2_STATE_FILE


class _StateMixin:
    """
    Mixed into SmitePlugin. Reads/writes:
      self.last_match_result
      self._session_wins / _session_losses / _session_date
      self._on_match_result_callbacks
    All initialized in SmitePlugin.__init__.
    """

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
        from core.atomic_io import atomic_write_json
        atomic_write_json(SMITE2_STATE_FILE, state)

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

        # Fire match result callbacks (economy plugin settlement, etc.).
        # match_id is included so dedup-aware downstream code (the
        # economy's settle_match) can guard against double-processing
        # — same match_id could come from a backfill pass on a later
        # bot launch.
        if self._on_match_result_callbacks:
            result_data = {
                "match_id": (self.last_match_result.get("match_id")
                             if self.last_match_result else None),
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
