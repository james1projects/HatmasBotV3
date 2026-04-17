"""
DeathCounterPlugin
==================
Tracks total deaths per day. Resets at midnight, mirrors the smite daily W-L
pattern. Powers the /overlay/deaths browser source.

Every time the KillDeathDetector fires on_death, main.py calls increment()
on this plugin. The count persists to data/death_count.json so bot restarts
mid-stream don't lose the tally.
"""

import json
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "death_count.json"


class DeathCounterPlugin:
    """Simple daily death tally with date-based auto-reset."""

    def __init__(self):
        self.bot = None
        self._count = 0
        self._date = datetime.now().strftime("%Y-%m-%d")
        self._load_state()

    # -----------------------------------------------------------------
    # Plugin lifecycle
    # -----------------------------------------------------------------

    def setup(self, bot):
        self.bot = bot

    async def on_ready(self):
        # Nothing to do on ready, state loaded in __init__
        pass

    async def cleanup(self):
        self._save_state()

    # -----------------------------------------------------------------
    # State persistence
    # -----------------------------------------------------------------

    def _load_state(self):
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            saved_date = data.get("date")
            today = datetime.now().strftime("%Y-%m-%d")
            if saved_date == today:
                self._count = int(data.get("count", 0))
                self._date = today
                print(f"[DeathCounter] Restored daily count: {self._count}")
            else:
                self._date = today
                self._count = 0
                print("[DeathCounter] New day, count reset to 0")
        except Exception as e:
            print(f"[DeathCounter] Failed to load state: {e}")

    def _save_state(self):
        try:
            STATE_FILE.write_text(
                json.dumps(
                    {"count": self._count, "date": self._date},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[DeathCounter] Failed to save state: {e}")

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def _check_day_reset(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._date != today:
            self._count = 0
            self._date = today
            self._save_state()
            print("[DeathCounter] New day detected, count reset to 0")

    def increment(self) -> int:
        """Add one death to today's total. Returns the new count."""
        self._check_day_reset()
        self._count += 1
        self._save_state()
        print(f"[DeathCounter] Deaths today: {self._count}")
        return self._count

    def get_count(self) -> int:
        """Return today's death count, resetting first if the day changed."""
        self._check_day_reset()
        return self._count

    def get_state(self) -> dict:
        """Return full state for API responses."""
        self._check_day_reset()
        return {"count": self._count, "date": self._date}
