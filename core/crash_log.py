"""
core/crash_log.py — persistent crash and restart logging.

Unhandled exceptions used to go only to stderr: if the bot died at
3am the traceback scrolled away with the console window and there
was nothing to diagnose in the morning. This module appends every
crash (and every supervisor restart event) to data/crash.log with a
timestamp, so downtime is always explained by a file on disk.

Used from two processes:
  * main.py calls record_crash() from its top-level except block.
  * tools/supervisor.py calls log_event() when it restarts the bot.

Deliberately stdlib-only and import-safe: importing this module must
never fail or trigger config side effects, because it runs on the
error path.
"""

from __future__ import annotations

import datetime
import os
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CRASH_LOG = BASE_DIR / "data" / "crash.log"

# Rotate once the log passes ~1 MB: crash.log -> crash.log.1 (the
# previous .1 is dropped). Two files bound disk use at ~2 MB while
# keeping weeks of history at typical crash volumes.
MAX_BYTES = 1_000_000


def _rotate_if_needed() -> None:
    try:
        if CRASH_LOG.exists() and CRASH_LOG.stat().st_size > MAX_BYTES:
            os.replace(CRASH_LOG, CRASH_LOG.with_suffix(".log.1"))
    except OSError:
        pass  # rotation is best-effort; never block the write below


def _append(text: str) -> None:
    try:
        CRASH_LOG.parent.mkdir(exist_ok=True)
        _rotate_if_needed()
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except OSError:
        pass  # a failing disk must not mask the original crash


def _stamp() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def log_event(message: str, source: str = "supervisor") -> None:
    """Append a one-line timestamped event (restarts, backoff waits)."""
    _append(f"[{_stamp()}] [{source}] {message}")


def record_crash(exc: BaseException | None = None, source: str = "main") -> None:
    """Append a timestamped traceback for the active (or given) exception."""
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    else:
        tb = traceback.format_exc()
    _append(f"[{_stamp()}] [{source}] CRASH\n{tb}{'-' * 60}")
