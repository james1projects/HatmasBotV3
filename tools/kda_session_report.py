"""
KDA Session Report
==================
Human-readable summary of a detector session recorded by the flight
recorder (core/kda_session_recorder.py). Run after an offstream
practice session (OBS + bot open, not live) to see exactly what the
detector saw and decided — every event, every rejected read, every
suppressed phantom (with its frame crop saved next to the journal).

Usage:
    python tools/kda_session_report.py               # latest session
    python tools/kda_session_report.py --list        # list sessions
    python tools/kda_session_report.py <session dir or name>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = REPO_ROOT / "data" / "kda_sessions"


def fmt_t(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def load(session: Path) -> list[dict]:
    j = session / "session.jsonl"
    if not j.exists():
        sys.exit(f"no session.jsonl in {session}")
    return [json.loads(l) for l in
            j.read_text(encoding="utf-8").splitlines() if l.strip()]


def report(session: Path) -> None:
    rows = load(session)
    print(f"Session {session.name} — {len(rows)} journal entries\n")

    counts = Counter(r["kind"] for r in rows)
    hb = counts.pop("heartbeat", 0)
    for kind in ("startup_validated", "event_kill", "event_death",
                 "event_assist", "suppressed_phantom", "reject_decrease",
                 "reject_jump", "rebaseline", "zero_reset"):
        if counts.get(kind):
            print(f"  {kind:20s} {counts[kind]}")
    print(f"  {'heartbeats':20s} {hb} (~{hb * 0.5:.0f} min of reads)\n")

    interesting = [r for r in rows if r["kind"] != "heartbeat"]
    if not interesting:
        print("nothing but heartbeats — quiet session.")
        return

    print("timeline:")
    for r in interesting:
        kda = "/".join(map(str, r["kda"])) if r.get("kda") else "-"
        prev = "/".join(map(str, r["prev"])) if r.get("prev") else ""
        arrow = f"{prev} → {kda}" if prev else kda
        frame = f"   [{r['frame']}]" if r.get("frame") else ""
        note = f"   ({r['note']})" if r.get("note") else ""
        print(f"  {fmt_t(r['t']):>8}  {r['kind']:20s} {arrow}{note}{frame}")

    # Verdict block — what a diagnosis session is FOR.
    phantoms = counts.get("suppressed_phantom", 0)
    rebases = counts.get("rebaseline", 0)
    rejects = counts.get("reject_decrease", 0) + counts.get("reject_jump", 0)
    print("\nverdict:")
    if phantoms:
        print(f"  {phantoms} phantom read(s) were caught by the confirm "
              f"gate before firing — these would have been live misfires "
              f"before 7/10. Crops saved for analysis.")
    if rebases:
        print(f"  {rebases} rebaseline(s): a phantom got through the gate "
              f"and was corrected ~2.5s later. If this is >0 regularly, "
              f"raise DELTA_CONFIRM_READS to 3.")
    if rejects and not phantoms and not rebases:
        print(f"  {rejects} rejected read(s), all absorbed cleanly.")
    if not (phantoms or rebases or rejects):
        print("  clean session — every read consistent.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?",
                    help="Session dir name (default: latest)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if not SESSIONS_DIR.exists():
        sys.exit("no data/kda_sessions/ yet — run the bot with detection "
                 "on (a session appears at the first decision).")

    sessions = sorted(d for d in SESSIONS_DIR.iterdir() if d.is_dir())
    if args.list:
        for s in sessions:
            n = sum(1 for _ in (s / "session.jsonl").open()) \
                if (s / "session.jsonl").exists() else 0
            print(f"{s.name}  ({n} entries)")
        return
    if not sessions:
        sys.exit("no sessions recorded yet.")

    if args.session:
        target = Path(args.session)
        if not target.is_dir():
            target = SESSIONS_DIR / args.session
    else:
        target = sessions[-1]
    report(target)


if __name__ == "__main__":
    main()
