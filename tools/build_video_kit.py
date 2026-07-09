"""
Video Kit Builder
=================
One command that produces everything needed to edit and publish a full
gameplay recording, from the artifacts the pipeline already makes:

  1. intro_<god>.png   — transparent top-strip stats overlay (build_intro)
  2. outro_<god>.png   — 1920x1080 end-screen card        (build_outro)
  3. <stem>.chapters.txt — YouTube chapter list built from the sibling
     .events.json kill feed, ready to paste into the video description.

Point it at a recording that process_recordings.py has already sorted:

    python tools/build_video_kit.py "recordings/Atlas/Atlas-12.mp4"

The god is inferred from (in order) --god, the events.json top-level
gods_seen list, then the recording's parent folder name. Outputs land
next to the recording so the DaVinci project and the upload checklist
are one folder.

Chapter rules follow YouTube's constraints: first chapter is forced to
0:00, chapters closer than 10s to the previous one are merged, and
kills within 12s of each other collapse into one "Double Kill" /
"Triple Kill" / ... chapter. --offset SECONDS shifts every timestamp
(use it when the edit prepends an intro before the gameplay).

    python tools/build_video_kit.py <video> [--god NAME] [--offset 8]
        [--include-deaths] [--skip-images]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

MULTI_KILL_WINDOW_S = 12.0   # kills this close merge into one chapter
MIN_CHAPTER_GAP_S = 10.0     # YouTube rejects chapters closer than this


def fmt_ts(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


MULTI_NAMES = {2: "Double Kill", 3: "Triple Kill", 4: "Quadra Kill",
               5: "Penta Kill"}


def build_chapters(events, offset=0.0, include_deaths=False):
    """Turn an events list (timestamp_sec/type/note dicts) into
    [(seconds, title)] YouTube chapters. Kills within
    MULTI_KILL_WINDOW_S collapse into multi-kill chapters."""
    kills = sorted((e for e in events if e.get("type") == "kill"),
                   key=lambda e: e["timestamp_sec"])
    deaths = [e for e in events if e.get("type") == "death"] \
        if include_deaths else []

    groups = []
    for e in kills:
        if groups and (e["timestamp_sec"] - groups[-1][-1]["timestamp_sec"]
                       <= MULTI_KILL_WINDOW_S):
            groups[-1].append(e)
        else:
            groups.append([e])

    chapters = []
    # Match boundaries (when the detector recorded them) become their
    # own chapters — with the god's name when recordings span games.
    starts = [e for e in events if e.get("type") == "game_start"]
    for e in starts:
        god = (e.get("god") or "").strip()
        title = (f"Match Start — {god}"
                 if god and len(starts) > 1 else "Match Start")
        chapters.append((e["timestamp_sec"] + offset, title))
    kill_no = 0
    for g in groups:
        kill_no += len(g)
        note = next((e["note"] for e in g if e.get("note")), "")
        if len(g) > 1:
            title = MULTI_NAMES.get(len(g), f"{len(g)}-Kill Frenzy")
        elif note:
            title = note.title()
        else:
            title = f"Kill #{kill_no}"
        chapters.append((g[0]["timestamp_sec"] + offset, title))

    for e in deaths:
        chapters.append((e["timestamp_sec"] + offset, "It Gets Worse"))

    chapters.sort(key=lambda c: c[0])

    # Enforce YouTube's minimum spacing by dropping the later chapter of
    # any too-close pair (the earlier one keeps the moment findable).
    spaced = []
    for t, title in chapters:
        if spaced and t - spaced[-1][0] < MIN_CHAPTER_GAP_S:
            continue
        spaced.append((t, title))

    # First chapter must be 0:00 for YouTube to pick the list up at all.
    if not spaced or spaced[0][0] > 0:
        # If a real detector-recorded Match Start chapter exists later
        # in the list, the forced 0:00 lead is pre-game content.
        has_real_start = any(t.startswith("Match Start") for _, t in spaced)
        lead = "Intro" if (offset > 0 or has_real_start) else "Match Start"
        # guard the 10s rule against a kill in the opening seconds
        spaced = [(0.0, lead)] + [c for c in spaced
                                  if c[0] >= MIN_CHAPTER_GAP_S]
    return spaced


def infer_god(args_god, events_doc, video_path):
    if args_god:
        return args_god
    # Most precise source first: the per-game god attribution on the
    # game_start / game_end markers (added 7/9).  Only trust it when
    # every marked game agrees — a multi-god recording needs --god.
    marker_gods = {
        e["god"] for e in events_doc.get("events", [])
        if e.get("type") in ("game_start", "game_end") and e.get("god")
    }
    if len(marker_gods) == 1:
        return marker_gods.pop()
    gods = events_doc.get("gods_seen") or []
    if len(gods) == 1:
        return gods[0]
    folder = video_path.parent.name
    if folder.lower() not in ("recordings", "mixed", "unknown", ""):
        return folder
    return None


def main():
    ap = argparse.ArgumentParser(description="Build intro/outro/chapters "
                                             "for a sorted recording.")
    ap.add_argument("video", type=Path, help="Recording .mp4 (with sibling "
                                             ".events.json)")
    ap.add_argument("--god", help="Override god inference")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="Seconds the edit prepends before gameplay")
    ap.add_argument("--include-deaths", action="store_true",
                    help="Add death chapters too (self-deprecating mode)")
    ap.add_argument("--skip-images", action="store_true",
                    help="Chapters only; skip intro/outro renders")
    args = ap.parse_args()

    video = args.video.resolve()
    events_path = video.parent / (video.stem + ".events.json")
    if not events_path.exists():
        sys.exit(f"No {events_path.name} next to the recording — run "
                 f"tools/process_recordings.py (or extract_events.py) first.")

    doc = json.loads(events_path.read_text(encoding="utf-8"))
    events = doc.get("events", [])

    god = infer_god(args.god, doc, video)
    if god is None:
        print("[warn] couldn't infer a single god (mixed/unknown recording); "
              "pass --god to get intro/outro renders")

    # ---- chapters ----------------------------------------------------
    chapters = build_chapters(events, offset=args.offset,
                              include_deaths=args.include_deaths)
    out_txt = video.parent / f"{video.stem}.chapters.txt"
    lines = [f"{fmt_ts(t)} {title}" for t, title in chapters]
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    kills = sum(1 for e in events if e.get("type") == "kill")
    print(f"[ok] {out_txt.name}: {len(chapters)} chapters from "
          f"{kills} kills")
    for ln in lines:
        print(f"       {ln}")

    # ---- intro / outro ------------------------------------------------
    if god and not args.skip_images:
        import build_intro
        import build_outro
        import sqlite3
        con = sqlite3.connect(f"file:{build_outro.DB_PATH}?mode=ro", uri=True)
        try:
            stats = build_outro.fetch_god_stats(con, god)
        finally:
            con.close()
        if stats is None:
            sys.exit(f"'{god}' not in god_prices — check the name.")

        from build_thumbnail import resolve_god_card, resolve_god_icon

        intro_png = video.parent / f"intro_{video.stem}.png"
        build_intro.render(stats, resolve_god_icon(stats["name"])) \
            .save(intro_png)
        print(f"[ok] {intro_png.name}")

        card = resolve_god_card(stats["name"])
        if card and Path(card).exists():
            outro_png = video.parent / f"outro_{video.stem}.png"
            build_outro.render(stats, card).save(outro_png)
            print(f"[ok] {outro_png.name}")
        else:
            print(f"[warn] no card art for {stats['name']}; skipped outro "
                  f"(tools/download_god_cards.py --add \"{stats['name']}\")")


if __name__ == "__main__":
    main()
