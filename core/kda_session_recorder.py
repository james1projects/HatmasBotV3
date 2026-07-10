"""
KDA Session Recorder — the detector's flight recorder.
======================================================
Persistent, per-session record of every DECISION the live KillDetector
makes (accepts, rejects, suppressed phantoms, rebaselines, resets),
plus the KDA-strip crop for each anomaly. Runs whenever detection runs
— live or not — so offstream ranked sessions (OBS + bot open, channel
offline) automatically become labeled diagnostic corpora.

Files land in data/kda_sessions/<YYYYMMDD_HHMMSS>/:
    session.jsonl          one line per decision + a 30s heartbeat
    <kind>_<t>_<n>.png     KDA-strip crop for anomalies (capped)

Read a session back with tools/kda_session_report.py.

Design constraints:
  * must NEVER break the detection loop — every public method swallows
    its own exceptions;
  * negligible cost — stable no-change reads are NOT logged (a 30s
    heartbeat proves liveness instead), crops are ~90x20px PNGs and
    capped per session.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

MAX_ANOMALY_FRAMES = 300      # per session — plenty, and bounded
HEARTBEAT_EVERY_S = 30.0

# Decision kinds that get their crop saved (the interesting ones).
FRAME_KINDS = {
    "suppressed_phantom", "reject_decrease", "reject_jump",
    "rebaseline", "event_kill", "event_death", "event_assist",
    "startup_validated", "zero_reset",
}


class KdaSessionRecorder:
    def __init__(self, base_dir: Path, logger: Optional[logging.Logger] = None):
        self._log = logger or logging.getLogger("KdaSessionRecorder")
        self._dir: Optional[Path] = None
        self._fh = None
        self._base = Path(base_dir)
        self._frames_saved = 0
        self._last_heartbeat = 0.0
        self._t0 = time.time()

    # -- lifecycle -------------------------------------------------------

    def _ensure_session(self) -> bool:
        if self._fh is not None:
            return True
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            self._dir = self._base / stamp
            self._dir.mkdir(parents=True, exist_ok=True)
            self._fh = (self._dir / "session.jsonl").open(
                "a", encoding="utf-8")
            self._t0 = time.time()
            return True
        except Exception as e:
            self._log.warning(f"session recorder disabled: {e}")
            self._fh = None
            self._dir = None
            return False

    def close(self) -> None:
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        self._fh = None

    # -- recording -------------------------------------------------------

    def record(
        self,
        kind: str,
        *,
        kda=None,
        prev=None,
        note: str = "",
        crop=None,           # PIL.Image of the KDA strip, if available
    ) -> None:
        """Log one detector decision. Never raises."""
        try:
            if not self._ensure_session():
                return
            t = round(time.time() - self._t0, 1)
            row = {"t": t, "kind": kind}
            if kda is not None:
                row["kda"] = list(kda)
            if prev is not None:
                row["prev"] = list(prev)
            if note:
                row["note"] = note
            frame_name = None
            if (crop is not None and kind in FRAME_KINDS
                    and self._frames_saved < MAX_ANOMALY_FRAMES):
                frame_name = f"{kind}_{t:.0f}s_{self._frames_saved}.png"
                try:
                    crop.save(str(self._dir / frame_name))
                    self._frames_saved += 1
                    row["frame"] = frame_name
                except Exception:
                    pass
            self._fh.write(json.dumps(row) + "\n")
            self._fh.flush()
        except Exception:
            pass

    def heartbeat(self, kda=None) -> None:
        """Cheap liveness proof — call once per read; writes ~2/min."""
        try:
            now = time.time()
            if now - self._last_heartbeat < HEARTBEAT_EVERY_S:
                return
            self._last_heartbeat = now
            self.record("heartbeat", kda=kda)
        except Exception:
            pass
