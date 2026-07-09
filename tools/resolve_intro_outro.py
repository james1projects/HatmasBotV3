#!/usr/bin/env python3
"""
resolve_intro_outro.py
======================
Place the intro stats strip and the outro card on the current DaVinci
Resolve timeline, anchored to the game_start / game_end events the VOD
detector records in .events.json (added 7/9).

For each game in the recording the intro strip appears at that game's
start for ~8 seconds and fades out; the outro card fades in after the
last game's end and holds ~15 seconds.  Multi-game recordings get one
god-correct intro per game by default; use --game N to build for a
single game (intro at game N's start, outro after game N's end) when
you're cutting one game out as a full gameplay upload.

How it works — Resolve cannot script still durations or fades (stills
land at the 5s user preference, SetProperty on fades returns False;
probed live on Resolve 21.0.2), so this tool renders the PNGs into
short QuickTime Animation (qtrle) clips with the alpha fade BAKED IN
via ffmpeg, then appends those to V2.  Video clips keep their exact
duration, so no manual trimming or fading is ever needed.

The .mov files land in <recording folder>/.overlays/ — they must
outlive this script because the Resolve project references them
(same convention as resolve_tiktok.py's .tiktok_bg folder).

Usage (after resolve_import.py has the project open):
    python tools/resolve_intro_outro.py <recording.mp4>
    python tools/resolve_intro_outro.py <recording> --game 2
    ... [--intro-secs 8] [--outro-secs 15] [--offset SECONDS]
        [--no-intro] [--no-outro]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resolve_import import connect_resolve, fail  # noqa: E402
from resolve_markers import pick_events_file  # noqa: E402
import build_intro  # noqa: E402
import build_outro  # noqa: E402
from build_thumbnail import resolve_god_card, resolve_god_icon, slugify  # noqa: E402

# Fallback locations for ffmpeg when the current shell hasn't picked up
# the PATH edit from the winget install (Gyan.FFmpeg, installed 7/9).
FFMPEG_FALLBACK_GLOBS = (
    "C:/Users/*/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe",
)


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import glob
    for pattern in FFMPEG_FALLBACK_GLOBS:
        hits = glob.glob(pattern)
        if hits:
            return hits[0]
    fail("ffmpeg not found — install with: winget install Gyan.FFmpeg")


def render_overlay_mov(png: Path, mov: Path, secs: float, fps: float,
                       fade: str, ffmpeg: str) -> None:
    """Wrap a PNG into a qtrle .mov of exact duration with a baked
    alpha fade.  fade='out' fades the last second; 'in' the first."""
    if fade == "out":
        vf = f"fade=t=out:st={secs - 1:.2f}:d=1:alpha=1,format=rgba"
    else:
        vf = "fade=t=in:st=0:d=1:alpha=1,format=rgba"
    cmd = [ffmpeg, "-y", "-loop", "1", "-i", str(png), "-t", f"{secs:.2f}",
           "-vf", vf, "-c:v", "qtrle", "-r", f"{fps:g}", str(mov)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not mov.exists():
        fail(f"ffmpeg failed for {mov.name}:\n{proc.stderr[-800:]}")


def load_games(events_path: Path) -> tuple[list[dict], list[dict]]:
    doc = json.loads(events_path.read_text(encoding="utf-8"))
    evs = doc.get("events", [])
    starts = [e for e in evs if e.get("type") == "game_start"]
    ends = [e for e in evs if e.get("type") == "game_end"]
    return starts, ends


def god_stats_or_fail(god: str) -> dict:
    con = sqlite3.connect(f"file:{build_outro.DB_PATH}?mode=ro", uri=True)
    try:
        stats = build_outro.fetch_god_stats(con, god)
    finally:
        con.close()
    if stats is None:
        fail(f"'{god}' not found in god_prices")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Overlay intro strip at game start(s) and outro card "
                    "at game end on the current Resolve timeline.")
    ap.add_argument("source", nargs="?",
                    help="Recording .mp4 or .events.json (picker if omitted)")
    ap.add_argument("--game", type=int, default=0,
                    help="1-based game number to build for (default: intro "
                         "for every game, outro after the last one)")
    ap.add_argument("--intro-secs", type=float, default=8.0)
    ap.add_argument("--outro-secs", type=float, default=15.0)
    ap.add_argument("--offset", type=float, default=0.0,
                    help="Seconds of timeline before the gameplay clip starts")
    ap.add_argument("--no-intro", action="store_true")
    ap.add_argument("--no-outro", action="store_true")
    args = ap.parse_args()

    events_path = pick_events_file(args.source)
    starts, ends = load_games(events_path)

    if args.game:
        if args.game > len(starts):
            fail(f"--game {args.game} but only {len(starts)} game_start "
                 f"event(s) in {events_path.name}")
        starts = [starts[args.game - 1]]
        # game N's end: first game_end after its start
        s_t = starts[0]["timestamp_sec"]
        ends = [e for e in ends if e["timestamp_sec"] >= s_t][:1]
    elif ends:
        ends = ends[-1:]  # default: outro only after the final game

    if not starts and not args.no_intro:
        print(f"[warn] no game_start in {events_path.name} (recording likely "
              f"begins mid-game) — skipping intro. Re-scan with the updated "
              f"detector if this file predates game markers.")
    if not ends and not args.no_outro:
        print("[warn] no game_end recorded — outro will anchor to the "
              "end of the gameplay clip instead.")

    ffmpeg = find_ffmpeg()
    overlays_dir = events_path.parent / ".overlays"
    overlays_dir.mkdir(exist_ok=True)
    stem = events_path.name.replace(".events.json", "")

    # ---- connect to Resolve first so we fail fast and know the fps ----
    resolve = connect_resolve()
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if not project:
        fail("no project open in Resolve — run resolve_import.py first")
    timeline = project.GetCurrentTimeline()
    if not timeline:
        fail("no current timeline — run resolve_import.py first")
    media_pool = project.GetMediaPool()
    fps = float(timeline.GetSetting("timelineFrameRate") or 60)

    v1 = timeline.GetItemListInTrack("video", 1)
    if not v1:
        fail("V1 is empty — nothing to overlay onto")
    v1_start, v1_end = v1[0].GetStart(), v1[-1].GetEnd()

    if timeline.GetTrackCount("video") < 2:
        if not timeline.AddTrack("video"):
            fail("could not add V2")

    def place(mov: Path, at_sec: float) -> bool:
        items = media_pool.ImportMedia([str(mov)])
        if not items:
            return False
        placed = media_pool.AppendToTimeline([{
            "mediaPoolItem": items[0],
            "recordFrame": v1_start + int(round((at_sec + args.offset) * fps)),
            "trackIndex": 2,
            "mediaType": 1,
        }])
        return bool(placed)

    # ---- intros: one per game_start, god-correct -----------------------
    if not args.no_intro:
        for i, ev in enumerate(starts, 1):
            god = (ev.get("god") or "").strip()
            if not god:
                print(f"[warn] game {i}: no god attributed — skipping intro")
                continue
            stats = god_stats_or_fail(god)
            png = overlays_dir / f"{stem}_intro{i}_{slugify(god)}.png"
            build_intro.render(stats, resolve_god_icon(stats["name"])).save(png)
            mov = png.with_suffix(".mov")
            render_overlay_mov(png, mov, args.intro_secs, fps, "out", ffmpeg)
            t = ev["timestamp_sec"]
            if place(mov, t):
                print(f"[ok] intro {i} ({god}) at {t:.0f}s "
                      f"for {args.intro_secs:g}s + fade")
            else:
                print(f"[warn] intro {i} placement refused at {t:.0f}s")

    # ---- outro: after the (selected) last game_end ----------------------
    if not args.no_outro:
        if ends:
            end_ev = ends[-1]
            god = (end_ev.get("god") or "").strip()
            # victory screen follows the last HUD read; give it a beat
            at = end_ev["timestamp_sec"] + 2.0
        else:
            god = ""
            at = (v1_end - v1_start) / fps - args.outro_secs
        if not god:
            god = next((s.get("god") for s in reversed(starts)
                        if s.get("god")), "")
        if not god:
            print("[warn] no god for the outro — skipping (pass --game or "
                  "re-scan the recording)")
        else:
            stats = god_stats_or_fail(god)
            card = resolve_god_card(stats["name"])
            if not card or not Path(card).exists():
                fail(f"no card art for {stats['name']} — run "
                     f"tools/download_god_cards.py --add \"{stats['name']}\"")
            png = overlays_dir / f"{stem}_outro_{slugify(god)}.png"
            build_outro.render(stats, card).convert("RGBA").save(png)
            mov = png.with_suffix(".mov")
            render_overlay_mov(png, mov, args.outro_secs, fps, "in", ffmpeg)
            if place(mov, at):
                print(f"[ok] outro ({god}) at {at:.0f}s "
                      f"for {args.outro_secs:g}s, fades in")
            else:
                print(f"[warn] outro placement refused at {at:.0f}s")

    pm.SaveProject()
    print("Done — overlays on V2, project saved.")


if __name__ == "__main__":
    main()
