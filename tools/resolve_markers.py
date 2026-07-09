#!/usr/bin/env python3
"""
resolve_markers.py
==================
Drop the kill feed onto the DaVinci Resolve timeline as colored markers.

Since Vegas was retired (7/2) the .events.json files that
extract_events.py / process_recordings.py write had no consumer on the
editing side. This tool closes that gap: run it after resolve_import.py
has built the gameplay project and every kill / death / assist becomes
a marker you can jump between while editing (Edit page: Shift+Up/Down).

    Green  = kill      Red = death      Yellow = assist

Usage:
    python tools/resolve_markers.py <recording.mp4 | recording.events.json>
    python tools/resolve_markers.py            # file picker over recordings/
    ... [--offset SECONDS]   # if the edit prepends an intro before gameplay
    ... [--include kills,deaths,assists]       # default: all three

The markers land on the CURRENT timeline of the CURRENT project — the
state resolve_import.py leaves Resolve in. Marker frame ids are relative
to the timeline start, so a clip that begins at the head of the timeline
lines up 1:1 with the recording's timestamps; use --offset otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Shared connect recipe (sys.path bootstrap + launch-and-poll) lives in
# resolve_import.py — keep a single source of truth for the API dance.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from resolve_import import connect_resolve, fail, RECORDINGS_DIR  # noqa: E402

EVENT_STYLE = {
    # type -> (marker color, default label)
    "kill": ("Green", "Kill"),
    "death": ("Red", "Death"),
    "assist": ("Yellow", "Assist"),
    "game_start": ("Blue", "Game Start"),
    "game_end": ("Purple", "Game End"),
}


def pick_events_file(cli_arg: str | None) -> Path:
    if cli_arg:
        p = Path(cli_arg)
        if p.suffix.lower() == ".mp4":
            p = p.parent / (p.stem + ".events.json")
        if not p.is_file():
            fail(f"events file not found: {p}")
        return p

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    initial = RECORDINGS_DIR if RECORDINGS_DIR.is_dir() else Path.home()
    name = filedialog.askopenfilename(
        parent=root,
        title="Add kill markers from which recording?",
        initialdir=str(initial),
        filetypes=[("Recordings / events", "*.mp4 *.events.json"),
                   ("All files", "*.*")],
    )
    root.destroy()
    if not name:
        print("No file selected — nothing to do.")
        sys.exit(0)
    return pick_events_file(name)


def add_markers(timeline, events, fps: float, offset: float,
                include: set[str]) -> tuple[int, int]:
    added = skipped = 0
    counts: dict[str, int] = {}
    # Expand events the scan's overlap-merge absorbed (kill+death
    # trades collapse into one kill-anchored clip event; the "merged"
    # key preserves the constituents) so the timeline still gets a
    # marker for each real moment.
    expanded = []
    for ev in events:
        expanded.append(ev)
        for sub in ev.get("merged") or []:
            expanded.append({**sub, "pre_sec": 0.0, "post_sec": 0.0})
    for ev in sorted(expanded, key=lambda e: e.get("timestamp_sec", 0.0)):
        kind = ev.get("type", "")
        if kind not in include or kind not in EVENT_STYLE:
            continue
        color, label = EVENT_STYLE[kind]
        counts[kind] = counts.get(kind, 0) + 1
        note = (ev.get("note") or "").strip()
        if kind in ("game_start", "game_end"):
            god = (ev.get("god") or "").strip()
            name = f"{label} — {god}" if god else label
            note = god
        elif note:
            name = note.title()
        else:
            name = f"{label} #{counts[kind]}"
        frame = max(0, int(round((ev["timestamp_sec"] + offset) * fps)))
        ok = timeline.AddMarker(frame, color, name, note, 1)
        if not ok:
            # AddMarker refuses duplicates on the same frame (e.g. a kill
            # and an assist detected in the same second) — nudge forward
            # a frame at a time until it takes, up to one second.
            for nudge in range(1, int(fps) + 1):
                if timeline.AddMarker(frame + nudge, color, name, note, 1):
                    ok = True
                    break
        if ok:
            added += 1
        else:
            skipped += 1
            print(f"  [warn] marker refused at frame {frame}: {name}")
    return added, skipped


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Add kill/death/assist markers from an .events.json "
                    "to the current Resolve timeline.")
    ap.add_argument("source", nargs="?",
                    help="Recording .mp4 or .events.json (picker if omitted)")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="Seconds of timeline before the gameplay clip starts")
    ap.add_argument("--include", default="kills,deaths,assists,games",
                    help="Comma list: kills,deaths,assists,games "
                         "(default all; 'games' = the game_start / "
                         "game_end match-boundary markers)")
    args = ap.parse_args()

    include = {w.strip().lower().rstrip("s")
               for w in args.include.split(",") if w.strip()}
    if "game" in include:
        include.discard("game")
        include |= {"game_start", "game_end"}
    include &= {"kill", "death", "assist", "game_start", "game_end"}
    if not include:
        fail(f"--include matched nothing: {args.include!r}")

    events_path = pick_events_file(args.source)
    doc = json.loads(events_path.read_text(encoding="utf-8"))
    events = doc.get("events", [])
    if not events:
        print(f"{events_path.name} has no events — nothing to mark.")
        return

    resolve = connect_resolve()
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if not project:
        fail("no project open in Resolve — run resolve_import.py first")
    timeline = project.GetCurrentTimeline()
    if not timeline:
        fail(f"project '{project.GetName()}' has no current timeline — "
             f"run resolve_import.py first")

    fps = float(timeline.GetSetting("timelineFrameRate") or 60)
    added, skipped = add_markers(timeline, events, fps, args.offset, include)
    pm.SaveProject()
    print(f"Done: {added} markers on '{timeline.GetName()}' "
          f"({fps:g} fps){f', {skipped} refused' if skipped else ''}.")


if __name__ == "__main__":
    main()
