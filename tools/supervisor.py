"""
tools/supervisor.py — keep HatmasBot running.

Wraps `python main.py` in a restart loop so a crash at 3am does not
take hatmaster.tv down until someone notices. A crash (non-zero
exit) is logged to data/crash.log and the bot is relaunched with
exponential backoff; a graceful shutdown (exit 0 — console "quit",
Ctrl+C, or "stop") ends the supervisor too, so intentional stops
stay intentional.

Usage:
    python tools/supervisor.py        (or run_bot.bat)

Backoff: 5s doubling to a 300s cap, reset after the bot stays up
for 10 minutes. The child inherits this console, so typing "quit"
and reading live output work exactly as they do under a bare
`python main.py`.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.crash_log import log_event  # noqa: E402

BACKOFF_INITIAL = 5
BACKOFF_CAP = 300
STABLE_SECONDS = 600  # child alive this long -> backoff resets


def main() -> int:
    backoff = BACKOFF_INITIAL
    log_event("supervisor started")

    while True:
        started = time.monotonic()
        child = None
        try:
            child = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=str(REPO_ROOT),
            )
            code = child.wait()
        except KeyboardInterrupt:
            # Ctrl+C hits the whole console group: the bot receives it
            # too and runs its own graceful shutdown. Wait it out
            # (repeated Ctrl+C included), then treat this as an
            # intentional stop regardless of exit code.
            code = None
            while child is not None:
                try:
                    code = child.wait()
                    break
                except KeyboardInterrupt:
                    continue
            log_event(f"stopped by Ctrl+C (bot exit code {code})")
            return 0

        ran_for = time.monotonic() - started

        if code == 0:
            log_event("bot exited cleanly; supervisor done")
            return 0

        if ran_for >= STABLE_SECONDS:
            backoff = BACKOFF_INITIAL

        log_event(
            f"bot exited with code {code} after {int(ran_for)}s; "
            f"restarting in {backoff}s (see traceback above in this log)"
        )
        print(
            f"[Supervisor] Bot exited with code {code}. "
            f"Restarting in {backoff}s (Ctrl+C to stop)."
        )
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            log_event("stopped by Ctrl+C during backoff")
            return 0
        backoff = min(backoff * 2, BACKOFF_CAP)


if __name__ == "__main__":
    sys.exit(main())
