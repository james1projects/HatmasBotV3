"""
tools/redblue.py
================

Top-level orchestrator for the Red/Blue Button trend video. Wraps
`tools/redblue_tally.py` + `tools/redblue_thumbnail.py` into a single
command so you don't have to run the two tools by hand.

Subcommands:
    cycle  <video_id>    Scan comments, render, upload — once.
    watch  <video_id>    Cycle every --interval seconds (default 900).
    status <video_id>    Peek at current tally and last-upload state
                         WITHOUT scanning or uploading.

Each cycle is itself idempotent — if `redblue_thumbnail update` sees
that the counts haven't changed since the last successful upload, the
upload step is skipped. Safe to drop on a cron / scheduled task.

Examples:
    python tools/redblue.py cycle  cUD9DbGPQvw
    python tools/redblue.py watch  cUD9DbGPQvw --interval 600
    python tools/redblue.py status cUD9DbGPQvw
"""

import argparse
import sys
import time
import traceback
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


# ============================================================
# IMPORTS FROM SIBLING TOOLS
# ============================================================
#
# Importing the existing CLI handlers directly (rather than shelling
# out to subprocesses) keeps the orchestrator fast, ensures both tools
# share the same Python interpreter / dependency state, and lets us
# reuse helpers like _load_state without duplication.

from tools.redblue_tally import (         # noqa: E402
    cmd_scan as _tally_scan,
    open_db as _open_db,
    current_tally as _current_tally,
)
from tools.redblue_thumbnail import (     # noqa: E402
    cmd_update as _thumb_update,
    _load_state as _thumb_load_state,
)


# ============================================================
# COMMANDS
# ============================================================

def cmd_cycle(args):
    """Scan -> render -> upload, all in one call."""
    print(f"[i] === redblue cycle for {args.video_id} ===")
    print("[i] Step 1/2: scanning YouTube comments...")
    _tally_scan(argparse.Namespace(video_id=args.video_id))

    print("\n[i] Step 2/2: rendering + uploading thumbnail...")
    _thumb_update(argparse.Namespace(
        video_id=args.video_id,
        counts=None,
        force=args.force,
    ))
    print("\n[+] Cycle complete.")


def cmd_watch(args):
    """Run `cycle` on a loop. Per-cycle exceptions are logged but don't crash the loop."""
    interval = max(60, args.interval)
    print(f"[i] Watching {args.video_id} — full cycle every {interval}s.")
    print("[i] Ctrl+C to stop.\n")

    cycle_n = 0
    try:
        while True:
            cycle_n += 1
            print(f"=== cycle #{cycle_n} ===")
            try:
                cmd_cycle(argparse.Namespace(
                    video_id=args.video_id,
                    force=False,
                ))
            except Exception as e:
                print(f"[!] cycle #{cycle_n} failed: {e}", file=sys.stderr)
                if args.verbose:
                    traceback.print_exc()
            print(f"\n[i] sleeping {interval}s...\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[i] Watch loop stopped.")


def cmd_status(args):
    """Read-only summary — no API calls, no upload."""
    conn = _open_db()
    red, blue = _current_tally(conn, args.video_id)
    conn.close()

    state = _thumb_load_state()
    last = state.get("videos", {}).get(args.video_id, {})

    total = red + blue
    print(f"Video: {args.video_id}")
    print(f"  Current tally   : RED {red}  |  BLUE {blue}  |  TOTAL {total}")

    if last:
        last_r = last.get("last_uploaded_red")
        last_b = last.get("last_uploaded_blue")
        print(f"  Last uploaded   : RED {last_r}  |  BLUE {last_b}")
        print(f"  Last upload at  : {last.get('last_uploaded_at', '?')}")
        if last_r == red and last_b == blue:
            print(f"  Sync            : up to date")
        else:
            d_red = (red - last_r) if isinstance(last_r, int) else "?"
            d_blue = (blue - last_b) if isinstance(last_b, int) else "?"
            print(f"  Sync            : pending — RED Δ{d_red}, BLUE Δ{d_blue}")
            print(f"                    run `python tools/redblue.py cycle {args.video_id}` to push.")
    else:
        print(f"  Last uploaded   : never")
        print(f"  Sync            : pending — run `python tools/redblue.py cycle {args.video_id}` to push.")


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Red/Blue full-loop orchestrator (scan + render + upload).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("cycle", help="Scan + render + upload, once.")
    sp.add_argument("video_id")
    sp.add_argument(
        "--force", action="store_true",
        help="Upload thumbnail even if counts haven't changed.",
    )
    sp.set_defaults(func=cmd_cycle)

    sp = sub.add_parser("watch", help="Run cycle every --interval seconds.")
    sp.add_argument("video_id")
    sp.add_argument(
        "--interval", type=int, default=900,
        help="Seconds between cycles (default 900 = 15 min, min 60).",
    )
    sp.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print full tracebacks on per-cycle errors.",
    )
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("status", help="Show tally + last upload state.")
    sp.add_argument("video_id")
    sp.set_defaults(func=cmd_status)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
