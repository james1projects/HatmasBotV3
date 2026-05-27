#!/usr/bin/env python3
"""
process_recordings.py
=====================
One-button workflow for end-of-stream recording cleanup.

Walks a recordings folder (default ``HatmasBot/recordings``), runs the
KDA + portrait scanner over every ``.mp4`` that doesn't already have a
sibling ``.events.json``, and then sorts each file into a per-god
subfolder based on which god(s) appeared in the recording:

  * Exactly one god identified:
        recordings/{God Name}/{God Name}-N.mp4
        recordings/{God Name}/{God Name}-N.events.json
  * Two or more gods identified (multi-match recording):
        recordings/mixed/mixed-N.mp4
        recordings/mixed/mixed-N.events.json
  * Zero gods identified (short clip, demo, menu only):
        recordings/unknown/unknown-N.mp4
        recordings/unknown/unknown-N.events.json

``N`` is the lowest positive integer not already used by another file
of the same stem in the target folder, so gaps left by deletions get
filled in.  Subfolders are created on demand.

Files already living inside a subfolder of ``recordings/`` are
considered already-processed and ignored.  ``recordings/`` itself is
the queue — anything in the root is treated as new.

The ``.events.json`` written next to the moved ``.mp4`` carries:
  * the renamed absolute path in ``source_video`` (so HighlightBuilder
    in Sony Vegas opens the right file),
  * a top-level ``gods_seen`` list (same one that drove the routing
    decision), and
  * the usual ``events`` array.

Defaults assume James's setup (NVIDIA HEVC, kills + deaths in the
output, OBS recordings at 1920x1080 60fps).  The defaults are picked so
the common case is just::

    python tools/process_recordings.py

with no flags — perfect for a Stream Deck button.

Usage:
    python tools/process_recordings.py [--source <folder>] [options]

See --help for the full option list.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

# Make sure the repo root is on sys.path regardless of where the script
# is invoked from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.kda_reader import KdaReader
from tools.vod_detector import (
    VodDetector,
    VodDetectorError,
    VodDetectorOptions,
)
from tools.extract_events import (
    _parse_include,
    find_mp4s,
    render_events_json,
)


# --- Defaults --------------------------------------------------------------

DEFAULT_SOURCE = _REPO_ROOT / "recordings"
DEFAULT_DATA_DIR = _REPO_ROOT / "data"
DEFAULT_TESSERACT_WIN = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Subfolder names for the two non-single-god cases.  Kept short so the
# resulting filenames are easy to scan visually in Vegas's file picker.
MIXED_FOLDER = "mixed"
UNKNOWN_FOLDER = "unknown"


# --- Routing logic ---------------------------------------------------------

def find_mp4s_recursive(
    root: Path,
    skip_folder_names: set | None = None,
) -> list[Path]:
    """Like extract_events.find_mp4s but recurses into subfolders.

    Used by ``--reprocess-all`` so a single command re-scans every
    recording in ``recordings/`` regardless of which per-god subfolder
    it currently lives in.

    ``skip_folder_names`` is a set of subfolder names (lowercased) we
    refuse to descend into — useful for ``processed/``, ``replays/``,
    and audit folders that start with an underscore.
    """
    if not root.exists():
        raise FileNotFoundError(f"Folder does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    skip = {s.lower() for s in (skip_folder_names or set())}

    mp4s: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() != ".mp4":
            continue
        # Skip if ANY parent folder name (between root and the file)
        # is excluded, or starts with "_" (audit / scratch folders).
        rel_parts = p.relative_to(root).parts[:-1]
        if any(part.lower() in skip or part.startswith("_")
               for part in rel_parts):
            continue
        mp4s.append(p)
    # Sort by parent-folder name then filename so a viewer scanning
    # the log can predict the order: root files first (alphabetical),
    # then each per-god subfolder in alpha order.
    mp4s.sort(key=lambda p: (
        "" if p.parent == root else p.parent.name.lower(),
        p.name.lower(),
    ))
    return mp4s


def categorize(gods_seen: list[str]) -> tuple[str, str]:
    """Pick the destination subfolder name and filename stem.

    Returns ``(folder, stem)``:

      * ``["Ymir"]``        → ``("Ymir", "Ymir")``
      * ``["Ymir", "Loki"]``→ ``("mixed", "mixed")``
      * ``[]``              → ``("unknown", "unknown")``

    The folder name and stem are intentionally identical so a viewer
    sees ``recordings/Ymir/Ymir-3.mp4`` instead of mixing conventions.
    Smite god display names contain spaces ("Hou Yi", "The Morrigan"),
    which Windows handles fine in folder names and filenames.
    """
    if not gods_seen:
        return UNKNOWN_FOLDER, UNKNOWN_FOLDER
    if len(gods_seen) == 1:
        name = gods_seen[0]
        return name, name
    return MIXED_FOLDER, MIXED_FOLDER


def next_index(folder: Path, stem: str) -> int:
    """Lowest positive integer ``N`` such that ``{stem}-N.mp4`` is free.

    Fills gaps: if ``Ymir-1.mp4`` and ``Ymir-3.mp4`` exist, returns 2.
    Case-insensitive match on the stem so ``ymir-2`` and ``Ymir-2``
    can't both claim slot 2 on Windows (which is case-insensitive
    anyway).
    """
    used: set[int] = set()
    if folder.exists():
        # Anchor pattern at start/end of stem to avoid matching e.g.
        # "Ymir-and-friends-2".  ``re.IGNORECASE`` keeps Windows
        # casing differences from creating phantom collisions.
        pat = re.compile(
            rf"^{re.escape(stem)}-(\d+)$",
            flags=re.IGNORECASE,
        )
        for p in folder.iterdir():
            if not p.is_file() or p.suffix.lower() != ".mp4":
                continue
            m = pat.match(p.stem)
            if m:
                used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return n


# --- Move + write ----------------------------------------------------------

def move_and_emit(
    video: Path,
    events: list[dict],
    gods_seen: list[str],
    source_root: Path,
    *,
    dry_run: bool,
) -> tuple[Path, Path]:
    """Move ``video`` into the right subfolder and write its events JSON.

    Order of operations is:
        1. Decide target paths.
        2. ``mkdir`` the target folder.
        3. Move the .mp4 (rename / cross-volume copy via ``shutil.move``).
        4. Write the .events.json next to it with the new absolute
           path baked into ``source_video``.

    The .mp4 moves before the JSON gets written so the JSON we emit
    can never reference a path that doesn't exist on disk — if the
    move fails the JSON is never written and the caller gets the
    original exception.

    Returns ``(new_video_path, new_json_path)``.  In ``dry_run`` mode
    nothing is touched and the function just returns the planned
    paths.
    """
    subfolder, stem = categorize(gods_seen)
    target_dir = source_root / subfolder
    n = next_index(target_dir, stem)
    new_stem = f"{stem}-{n}"
    new_video = target_dir / f"{new_stem}{video.suffix}"
    new_json = target_dir / f"{new_stem}.events.json"

    if dry_run:
        return new_video, new_json

    target_dir.mkdir(parents=True, exist_ok=True)

    shutil.move(str(video), str(new_video))

    payload = render_events_json(
        str(new_video.resolve()),
        events,
        gods_seen=gods_seen,
    )
    new_json.write_text(payload, encoding="utf-8")
    return new_video, new_json


# --- Per-video driver ------------------------------------------------------

def reprocess_one(
    video: Path,
    idx: int,
    total: int,
    detector: VodDetector,
    source_root: Path,
    *,
    dry_run: bool,
) -> dict:
    """Re-process a video already living in a per-god subfolder.

    Reuses process_one for the heavy lifting (scan + dashboard pushes
    + error handling), then overrides the move step:
      - If the newly-detected god matches the file's CURRENT parent
        folder, leave the file in place and just rewrite the events
        JSON next to it. Avoids churning filenames on every reprocess
        run.
      - If the newly-detected god is DIFFERENT, fall through to the
        normal move flow which picks the next available <God>-N slot
        in the new folder. The original file moves and gets renamed
        accordingly; its old events.json is left behind for cleanup
        (rare enough that we don't bother auto-deleting).

    Returns the same shape as process_one so the summary path doesn't
    care which function ran.
    """
    prefix = f"[{idx}/{total}] {video.relative_to(source_root)}"

    global _dashboard_current_file
    _dashboard_current_file = video.name

    sys.stdout.write(f"{prefix} -> rescanning...")
    sys.stdout.flush()

    def _on_progress(scan_t: float, duration: float) -> None:
        pct = 100.0 * scan_t / duration if duration > 0 else 0.0
        line = (
            f"{prefix} -> rescanning... {scan_t:.0f}s / {duration:.0f}s "
            f"({pct:.0f}%)"
        )
        sys.stdout.write("\r" + line)
        sys.stdout.flush()
        global _dashboard_last_update_at
        if _dashboard_enabled:
            now = time.time()
            if now - _dashboard_last_update_at >= DASHBOARD_UPDATE_THROTTLE:
                _dashboard_last_update_at = now
                _dashboard_post("/api/vod_processor/update", {
                    "current_file_idx": idx,
                    "current_file_name": str(video.relative_to(source_root)),
                    "current_scan_t": scan_t,
                    "current_duration": duration,
                })

    detector.opts.progress_callback = _on_progress
    try:
        events = detector.detect(video)
        gods_seen = list(detector.gods_seen or [])
    except VodDetectorError as e:
        sys.stdout.write("\n")
        print(f"  ERROR: {e}")
        _dashboard_post("/api/vod_processor/file_done", {
            "name": str(video.relative_to(source_root)),
            "status": "error", "error": str(e),
        })
        return {
            "status": "error", "video": video, "new_video": None,
            "kills": 0, "deaths": 0, "assists": 0,
            "gods_seen": [], "error": str(e),
        }
    except Exception as e:
        sys.stdout.write("\n")
        print(f"  ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        _dashboard_post("/api/vod_processor/file_done", {
            "name": str(video.relative_to(source_root)),
            "status": "error", "error": f"{type(e).__name__}: {e}",
        })
        return {
            "status": "error", "video": video, "new_video": None,
            "kills": 0, "deaths": 0, "assists": 0,
            "gods_seen": [], "error": f"{type(e).__name__}: {e}",
        }
    finally:
        detector.opts.progress_callback = None

    sys.stdout.write("\r" + " " * 100 + "\r")
    sys.stdout.flush()

    new_subfolder, _ = categorize(gods_seen)
    current_parent = video.parent
    current_parent_name = (current_parent.name
                           if current_parent != source_root
                           else "")

    k = sum(1 for e in events if e["type"] == "kill")
    d = sum(1 for e in events if e["type"] == "death")
    a = sum(1 for e in events if e["type"] == "assist")

    # Case 1: file already in the right folder. Just rewrite events JSON.
    same_folder = (current_parent_name.lower() == new_subfolder.lower())
    if same_folder:
        json_path = video.with_suffix(".events.json")
        verb = "would rewrite" if dry_run else "rewrote"
        if not dry_run:
            payload = render_events_json(
                str(video.resolve()), events, gods_seen=gods_seen,
            )
            json_path.write_text(payload, encoding="utf-8")
        god_label = ", ".join(gods_seen) if gods_seen else "no god"
        counts = [f"{k}k"]
        if d: counts.append(f"{d}d")
        if a: counts.append(f"{a}a")
        print(
            f"{prefix} -> {god_label}  ({'/'.join(counts)})  "
            f"{verb} events.json in place"
        )
        _dashboard_post("/api/vod_processor/file_done", {
            "name": str(video.relative_to(source_root)),
            "status": "rewrote",
            "god": gods_seen[0] if len(gods_seen) == 1
                   else ("mixed" if len(gods_seen) > 1 else None),
            "kills": k, "deaths": d, "assists": a,
            "output_path": str(json_path.relative_to(source_root)),
        })
        return {
            "status": "rewrote",
            "video": video, "new_video": video,
            "kills": k, "deaths": d, "assists": a,
            "gods_seen": gods_seen, "error": None,
        }

    # Case 2: god changed (or file landed in root from somewhere). Move.
    try:
        new_video, _new_json = move_and_emit(
            video, events, gods_seen, source_root, dry_run=dry_run,
        )
    except Exception as e:
        print(f"{prefix} -> ERROR moving file: {type(e).__name__}: {e}")
        _dashboard_post("/api/vod_processor/file_done", {
            "name": str(video.relative_to(source_root)),
            "status": "error",
            "error": f"move failed: {type(e).__name__}: {e}",
        })
        return {
            "status": "error", "video": video, "new_video": None,
            "kills": 0, "deaths": 0, "assists": 0,
            "gods_seen": gods_seen,
            "error": f"{type(e).__name__}: {e}",
        }

    # Also clean up the old .events.json next to where the file used to
    # live (the new one lives in the new folder via move_and_emit).
    old_json = video.with_suffix(".events.json")
    if not dry_run and old_json.exists():
        try:
            old_json.unlink()
        except OSError:
            pass

    god_label = ", ".join(gods_seen) if gods_seen else "no god"
    counts = [f"{k}k"]
    if d: counts.append(f"{d}d")
    if a: counts.append(f"{a}a")
    verb = "would move" if dry_run else "re-sorted to"
    print(
        f"{prefix} -> {god_label}  ({'/'.join(counts)})  "
        f"{verb} {new_video.relative_to(source_root)} "
        f"(was in {current_parent_name or 'root'})"
    )
    _dashboard_post("/api/vod_processor/file_done", {
        "name": str(video.relative_to(source_root)),
        "status": "re-sorted",
        "god": gods_seen[0] if len(gods_seen) == 1
               else ("mixed" if len(gods_seen) > 1 else None),
        "kills": k, "deaths": d, "assists": a,
        "output_path": str(new_video.relative_to(source_root)),
    })
    return {
        "status": "re-sorted",
        "video": video, "new_video": new_video,
        "kills": k, "deaths": d, "assists": a,
        "gods_seen": gods_seen, "error": None,
    }


def process_one(
    video: Path,
    idx: int,
    total: int,
    detector: VodDetector,
    source_root: Path,
    *,
    dry_run: bool,
) -> dict:
    """Scan + sort a single recording.  Never raises.

    Returns a result dict::
        {"status": "moved" | "error",
         "video": Path,
         "new_video": Optional[Path],
         "kills": int, "deaths": int, "assists": int,
         "gods_seen": list[str],
         "error": Optional[str]}
    """
    prefix = f"[{idx}/{total}] {video.name}"

    # Tell the dashboard log handler which file the next batch of
    # VodDetector warnings belong to.
    global _dashboard_current_file
    _dashboard_current_file = video.name

    # Per-video progress callback prints over a single line via \r.
    sys.stdout.write(f"{prefix} -> scanning...")
    sys.stdout.flush()

    def _on_progress(scan_t: float, duration: float) -> None:
        pct = 100.0 * scan_t / duration if duration > 0 else 0.0
        line = (
            f"{prefix} -> scanning... {scan_t:.0f}s / {duration:.0f}s "
            f"({pct:.0f}%)"
        )
        sys.stdout.write("\r" + line)
        sys.stdout.flush()
        # Also push to the /detector dashboard, throttled to avoid
        # flooding the bot's webserver during a fast scan.
        global _dashboard_last_update_at
        if _dashboard_enabled:
            now = time.time()
            if now - _dashboard_last_update_at >= DASHBOARD_UPDATE_THROTTLE:
                _dashboard_last_update_at = now
                _dashboard_post("/api/vod_processor/update", {
                    "current_file_idx": idx,
                    "current_file_name": video.name,
                    "current_scan_t": scan_t,
                    "current_duration": duration,
                })

    detector.opts.progress_callback = _on_progress
    try:
        events = detector.detect(video)
        gods_seen = list(detector.gods_seen or [])
    except VodDetectorError as e:
        sys.stdout.write("\n")
        print(f"  ERROR: {e}")
        _dashboard_post("/api/vod_processor/file_done", {
            "name": video.name,
            "status": "error",
            "error": str(e),
        })
        return {
            "status": "error",
            "video": video,
            "new_video": None,
            "kills": 0, "deaths": 0, "assists": 0,
            "gods_seen": [],
            "error": str(e),
        }
    except Exception as e:
        sys.stdout.write("\n")
        print(f"  ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        _dashboard_post("/api/vod_processor/file_done", {
            "name": video.name,
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        })
        return {
            "status": "error",
            "video": video,
            "new_video": None,
            "kills": 0, "deaths": 0, "assists": 0,
            "gods_seen": [],
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        detector.opts.progress_callback = None

    # Clear the progress line so the result line prints on a fresh row.
    sys.stdout.write("\r" + " " * 100 + "\r")
    sys.stdout.flush()

    try:
        new_video, _new_json = move_and_emit(
            video, events, gods_seen, source_root, dry_run=dry_run,
        )
    except Exception as e:
        print(f"{prefix} -> ERROR moving file: {type(e).__name__}: {e}")
        return {
            "status": "error",
            "video": video,
            "new_video": None,
            "kills": 0, "deaths": 0, "assists": 0,
            "gods_seen": gods_seen,
            "error": f"{type(e).__name__}: {e}",
        }

    k = sum(1 for e in events if e["type"] == "kill")
    d = sum(1 for e in events if e["type"] == "death")
    a = sum(1 for e in events if e["type"] == "assist")

    god_label = ", ".join(gods_seen) if gods_seen else "no god identified"
    counts = [f"{k}k"]
    if d:
        counts.append(f"{d}d")
    if a:
        counts.append(f"{a}a")
    rel = new_video.relative_to(source_root) if not dry_run else \
          new_video.relative_to(source_root)
    verb = "would move" if dry_run else "moved to"
    print(
        f"{prefix} -> {god_label}  ({'/'.join(counts)})  "
        f"{verb} {rel}"
    )

    _dashboard_post("/api/vod_processor/file_done", {
        "name": video.name,
        "status": "moved",
        "god": (gods_seen[0] if len(gods_seen) == 1
                else ("mixed" if len(gods_seen) > 1 else None)),
        "kills": k, "deaths": d, "assists": a,
        "output_path": str(new_video.relative_to(source_root))
                       if new_video else None,
    })

    return {
        "status": "moved",
        "video": video,
        "new_video": new_video,
        "kills": k, "deaths": d, "assists": a,
        "gods_seen": gods_seen,
        "error": None,
    }


# --- CLI -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="process_recordings.py",
        description=(
            "Scan unprocessed Smite 2 recordings, write their event JSONs, "
            "and sort each .mp4 into a per-god subfolder.  Defaults are "
            "tuned for James's setup (NVIDIA HEVC, kills + deaths in "
            "output) — for the common case, run with no flags."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=(
            f"Folder of new recordings to process.  Files in subfolders "
            f"are considered already sorted and ignored.  "
            f"Default: {DEFAULT_SOURCE}"
        ),
    )
    p.add_argument(
        "--include",
        default="deaths",
        help=(
            "Comma-separated extra event types to include in the .events.json. "
            "Options: deaths, assists, all.  Default: kills + deaths."
        ),
    )
    p.add_argument(
        "--hwaccel",
        default="cuda",
        help=(
            "ffmpeg -hwaccel value.  'cuda' for NVIDIA (recommended), "
            "'d3d11va' / 'dxva2' on other Windows GPUs, 'auto' to let "
            "ffmpeg pick, 'none' to force software decode.  Default: cuda."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and decide routing but don't move files or write JSONs.",
    )
    p.add_argument(
        "--enroll-templates",
        action="store_true",
        help=(
            "Save confirmed digit crops back into data/digit_templates/ "
            "during the scan.  Off by default."
        ),
    )
    p.add_argument(
        "--coarse",
        type=float,
        default=5.0,
        help="Coarse scan interval in seconds (default: 5.0).",
    )
    p.add_argument(
        "--precision",
        type=float,
        default=0.2,
        help="Binary-search refinement precision in seconds (default: 0.2).",
    )
    p.add_argument(
        "--no-refine",
        action="store_true",
        help=(
            "Skip the binary-search refinement step (faster scans, "
            "wider clip windows).  See extract_events.py for details."
        ),
    )
    p.add_argument(
        "--no-merge-overlaps",
        action="store_true",
        help="Keep events with overlapping pre/post windows separate.",
    )
    p.add_argument(
        "--no-lobby-skip",
        action="store_true",
        help="Disable the pre-match lobby sparse-sampling optimization.",
    )
    p.add_argument(
        "--no-ffmpeg-crop",
        action="store_true",
        help="Disable the ffmpeg-side HUD-strip crop (debugging only).",
    )
    p.add_argument(
        "--no-seek-scan",
        action="store_true",
        help=(
            "Disable the seek-based coarse scan and fall back to the "
            "legacy streaming-pipeline backend. Seek-based is the "
            "default — it's 3-10x faster on 4K AV1 sources because "
            "ffmpeg only decodes from the nearest keyframe per sample "
            "instead of decoding every frame to compute the fps filter "
            "timestamps. Use this flag for A/B comparison or if a "
            "particular recording fails to seek cleanly."
        ),
    )
    p.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="Path to ffmpeg binary (default: resolve on PATH).",
    )
    p.add_argument(
        "--ffprobe",
        default="ffprobe",
        help="Path to ffprobe binary (default: resolve on PATH).",
    )
    p.add_argument(
        "--tesseract",
        default=None,
        help=(
            f"Path to tesseract.exe (default: {DEFAULT_TESSERACT_WIN} "
            f"on Windows, none elsewhere)."
        ),
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=(
            "HatmasBot data directory (holds digit_templates/ and "
            f"god_icons/).  Default: {DEFAULT_DATA_DIR}"
        ),
    )
    p.add_argument(
        "--debug-misreads",
        nargs="?",
        const="data/vod_debug",
        default=None,
        metavar="DIR",
        help=(
            "Save the offending frame + raw KDA crop on every KDA-level "
            "misread (partial decrease, max-jump rejection).  Output "
            "lands in <DIR>/<video_basename>/.  Pass a path to override; "
            "bare flag uses 'data/vod_debug'.  Off by default."
        ),
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print per-sample detection progress.",
    )
    p.add_argument(
        "--no-dashboard",
        action="store_true",
        help=(
            "Skip the /detector dashboard bridge — don't probe the bot, "
            "don't open the browser, don't POST progress updates. Use "
            "when batching headlessly (no bot running, no browser)."
        ),
    )
    p.add_argument(
        "--no-open-browser",
        action="store_true",
        help=(
            "Don't auto-open the dashboard in a browser tab; just print "
            "the URL. Useful if you already have it open."
        ),
    )
    p.add_argument(
        "--reprocess-all",
        action="store_true",
        help=(
            "Walk every .mp4 in --source recursively (including ones "
            "already sorted into per-god subfolders) and re-run the "
            "detector on each. Files that still detect as their current "
            "folder's god get their .events.json rewritten in place. "
            "Files where the detection now disagrees get re-sorted into "
            "the correct folder. Use after detector improvements (e.g. "
            "new icon-filter thresholds, baseline validation) to upgrade "
            "previously-mislabeled recordings. Skips processed/, "
            "replays/, and any folder name starting with _."
        ),
    )
    return p


def _resolve_hwaccel(value: Optional[str]) -> Optional[str]:
    """Map the ``--hwaccel`` CLI string to the value vod_detector wants.

    ``"none"`` / ``"off"`` / ``""`` → ``None`` (software decode).
    Anything else → passed through verbatim.
    """
    if not value:
        return None
    v = value.strip()
    if not v or v.lower() in ("none", "off", "no"):
        return None
    return v


# ─── DASHBOARD BRIDGE ──────────────────────────────────────────────────────
# Push live progress to the bot's /detector page while a batch runs.
# Hits localhost:8069 (the bot's webserver). When the bot isn't running
# all calls fail silently and the processor keeps printing to console
# as it always has — the dashboard is purely additive observability.

DASHBOARD_BASE_URL = "http://localhost:8069"
DASHBOARD_PAGE_URL = f"{DASHBOARD_BASE_URL}/detector"
DASHBOARD_POST_TIMEOUT = 1.0  # seconds — keep the processor non-blocking
DASHBOARD_UPDATE_THROTTLE = 0.5  # seconds between progress updates

_dashboard_enabled = False
_dashboard_last_update_at = 0.0
# Updated by process_one() each iteration so the VodDetector log
# handler can tag every event with the file it came from.
_dashboard_current_file = None


def _dashboard_post(path: str, body: dict) -> bool:
    """POST a JSON body to the bot's dashboard endpoint.
    Returns True on success, False on any failure (silently)."""
    if not _dashboard_enabled:
        return False
    url = f"{DASHBOARD_BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=DASHBOARD_POST_TIMEOUT):
            return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _dashboard_probe() -> bool:
    """Return True if the bot's webserver is reachable on localhost:8069.
    Used at processor startup to decide whether to enable dashboard
    pushing for this run."""
    try:
        with urllib.request.urlopen(
            f"{DASHBOARD_BASE_URL}/api/state", timeout=1.0
        ) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _push_kda_failure_to_dashboard(payload: dict) -> None:
    """VodDetector failure_event_callback target.

    Forwards the failure dict to the bot's webserver so the /detector
    page can render the raw KDA crop + binarised view alongside the
    reason the read failed.  Tagged with the file currently being
    scanned so the dashboard can show "which video did this come from"
    in batch runs.  No-op when dashboard pushing is disabled.
    """
    if not _dashboard_enabled:
        return
    body = dict(payload)
    body["file"] = _dashboard_current_file
    _dashboard_post("/api/vod_processor/kda_failure", body)


class _DashboardLogHandler(logging.Handler):
    """Pipe VodDetector log records to the /detector dashboard.

    Attached to the "VodDetector" logger so every per-frame diagnostic
    (partial-decrease rejections, jump-too-large rejections, accepted
    events, god match warnings, etc.) shows up in the dashboard's
    rolling event panel in real time — same data the console scroll
    shows, but persistent and readable on a second monitor.

    Logs at WARNING+ by default so we don't flood the dashboard with
    routine per-sample debug spam. Bump via processor's --verbose if
    info-level visibility is wanted (not currently wired through).
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record):
        if not _dashboard_enabled:
            return
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        # Strip leading whitespace from the VodDetector log format
        # ("  t=X — partial KDA decrease...") so the dashboard table
        # doesn't render extra indent.
        msg = msg.lstrip()
        _dashboard_post("/api/vod_processor/event", {
            "ts": record.created,
            "level": record.levelname.lower(),
            "msg": msg,
            "file": _dashboard_current_file,
        })


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # --- Validate source folder ---------------------------------------
    if not args.source.exists():
        print(
            f"error: source folder does not exist: {args.source}\n"
            f"       create it and drop your recordings inside, or pass "
            f"--source to point at the right folder.",
            file=sys.stderr,
        )
        return 2
    if not args.source.is_dir():
        print(f"error: not a directory: {args.source}", file=sys.stderr)
        return 2

    # --- Parse --include ----------------------------------------------
    try:
        include_deaths, include_assists = _parse_include(args.include)
    except argparse.ArgumentTypeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # --- Logging ------------------------------------------------------
    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(name)s: %(message)s",
    )

    # --- Find recordings to scan -------------------------------------
    # Default path: only files in args.source root (per-god subfolders
    # are considered already-processed and skipped).
    # --reprocess-all path: walk all subfolders too, including ones
    # that already contain sorted videos. Used to upgrade earlier
    # mislabeled scans after detector improvements.
    try:
        if args.reprocess_all:
            mp4s = find_mp4s_recursive(
                args.source,
                skip_folder_names={"processed", "replays"},
            )
        else:
            mp4s = find_mp4s(args.source)
    except (FileNotFoundError, NotADirectoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not mp4s:
        if args.reprocess_all:
            print(f"No recordings found anywhere in {args.source}.")
        else:
            print(f"No new recordings to process in {args.source}.")
        return 0

    # --- Resolve Tesseract -------------------------------------------
    tesseract_path = args.tesseract
    if tesseract_path is None and Path(DEFAULT_TESSERACT_WIN).exists():
        tesseract_path = DEFAULT_TESSERACT_WIN

    # --- Build reader + detector ------------------------------------
    reader = KdaReader(
        data_dir=args.data_dir,
        tesseract_path=tesseract_path,
        debug=False,
    )
    if not reader.is_ready:
        print(
            "error: KDA reader is not ready — neither Tesseract nor a "
            f"digit template library is available.  Install Tesseract or "
            f"populate {args.data_dir / 'digit_templates'}.",
            file=sys.stderr,
        )
        return 2

    opts = VodDetectorOptions(
        coarse_interval=args.coarse,
        refine_precision=args.precision,
        include_deaths=include_deaths,
        include_assists=include_assists,
        enroll_templates=args.enroll_templates,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        verbose=args.verbose,
        no_refine=args.no_refine,
        ffmpeg_crop=not args.no_ffmpeg_crop,
        lobby_skip=not args.no_lobby_skip,
        use_seek_scan=not args.no_seek_scan,
        hwaccel=_resolve_hwaccel(args.hwaccel),
        merge_overlaps=not args.no_merge_overlaps,
        enable_god_detection=True,
        misread_debug_dir=(
            Path(args.debug_misreads) if args.debug_misreads else None
        ),
        # Surface KDA reader failures (crop + binarisation) to the
        # /detector dashboard so live diagnosis is possible without
        # having to scrape disk-side debug folders. No-op when the
        # bot isn't running (dashboard probe failed at startup).
        failure_event_callback=_push_kda_failure_to_dashboard,
    )

    detector = VodDetector(reader, opts)

    # --- Header -------------------------------------------------------
    include_label = ["kills"]
    if include_deaths:
        include_label.append("deaths")
    if include_assists:
        include_label.append("assists")
    if args.reprocess_all:
        print(
            f"Re-processing {len(mp4s)} recording(s) (recursive) in "
            f"{args.source}"
        )
        print(
            "Files matching their current god folder will get their "
            ".events.json rewritten in place. Files where detection "
            "now disagrees will be moved to the correct folder."
        )
    else:
        print(
            f"Processing {len(mp4s)} new recording(s) in {args.source}"
        )
    print(f"Emitting event types: {', '.join(include_label)}")
    if args.dry_run:
        print("(dry run — no files will be moved or written)")
    print()

    # --- Dashboard bridge --------------------------------------------
    # Probe the bot. If reachable, enable the dashboard pushes and
    # open the browser tab. Otherwise carry on with console-only
    # output — the dashboard is purely additive.
    global _dashboard_enabled
    if not args.no_dashboard and _dashboard_probe():
        _dashboard_enabled = True
        _dashboard_post("/api/vod_processor/start", {
            "total_files": len(mp4s),
            "args": {
                "source": str(args.source),
                "include": args.include,
                "hwaccel": args.hwaccel,
                "dry_run": bool(args.dry_run),
            },
        })
        # Pipe VodDetector log messages (warnings + above) to the
        # dashboard's rolling event panel. Attach to the named logger
        # so the same handler catches every detector instance.
        vod_logger = logging.getLogger("VodDetector")
        vod_logger.addHandler(_DashboardLogHandler())
        # Make sure the logger actually emits warnings — defensive.
        if (vod_logger.level == logging.NOTSET
                or vod_logger.level > logging.WARNING):
            vod_logger.setLevel(logging.WARNING)

        print(f"Dashboard: {DASHBOARD_PAGE_URL}")
        if not args.no_open_browser:
            try:
                webbrowser.open(DASHBOARD_PAGE_URL)
            except Exception:
                pass
        print()
    elif args.no_dashboard:
        pass  # explicitly disabled
    else:
        print(
            f"(dashboard skipped: bot not reachable at "
            f"{DASHBOARD_BASE_URL} — start `python main.py` in another "
            f"window to enable the live progress view)"
        )
        print()

    # --- Per-video loop ----------------------------------------------
    # Pick the per-video function based on the run mode. process_one
    # is the original "scan a brand-new file in root and sort it"
    # path; reprocess_one is the "re-scan and maybe re-sort" path
    # used by --reprocess-all.
    _per_video = reprocess_one if args.reprocess_all else process_one
    t_start = time.time()
    results = [
        _per_video(
            video, i, len(mp4s), detector, args.source,
            dry_run=args.dry_run,
        )
        for i, video in enumerate(mp4s, 1)
    ]
    elapsed = time.time() - t_start

    # --- Dashboard: mark batch complete -----------------------------
    if _dashboard_enabled:
        _dashboard_post("/api/vod_processor/stop", {"state": "done"})

    # --- Summary ------------------------------------------------------
    moved = sum(1 for r in results if r["status"] == "moved")
    errors = sum(1 for r in results if r["status"] == "error")
    total_k = sum(r["kills"] for r in results)
    total_d = sum(r["deaths"] for r in results)
    total_a = sum(r["assists"] for r in results)

    # Group by destination folder so the user sees what landed where.
    by_folder: dict[str, int] = {}
    for r in results:
        if r["status"] != "moved" or r["new_video"] is None:
            continue
        folder = r["new_video"].parent.name
        by_folder[folder] = by_folder.get(folder, 0) + 1

    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    print()
    bits = [f"{moved} moved"]
    if errors:
        bits.append(f"{errors} error(s)")
    print(f"Done in {mins}m {secs}s: {', '.join(bits)}.")
    if by_folder:
        breakdown = ", ".join(
            f"{n} -> {folder}/" for folder, n in sorted(by_folder.items())
        )
        print(f"Routed: {breakdown}")
    if moved:
        detail = [f"{total_k} kills"]
        if include_deaths:
            detail.append(f"{total_d} deaths")
        if include_assists:
            detail.append(f"{total_a} assists")
        print(f"Totals: {', '.join(detail)}.")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
