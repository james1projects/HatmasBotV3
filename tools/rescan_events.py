#!/usr/bin/env python3
"""
rescan_events.py
================
Rescan a folder of already-sorted recordings and update their sibling
``<basename>.events.json`` files **in place** — no renames, no moves,
no folder reshuffling.  Designed for the "I started editing a montage
in Sony Vegas, then realised the detector had misreads, and I want to
update the events without breaking my project's clip references" flow.

Pairs with the rescan-append mode in ``HighlightBuilder.cs``: this
tool writes a sidecar ``_rescan_diff.json`` listing every clip whose
detected event COUNT changed (added, removed, or both), and the Vegas
script can read that sidecar to re-import only the fixed clips at the
end of an in-progress timeline.

Differences from ``extract_events.py``:
  * Always overwrites — you don't pass ``--overwrite``; rescan IS the
    overwrite intent.  Existing files always get re-scanned.
  * Always writes the new ``.events.json`` even when the new scan
    produces zero events.  ``extract_events.py`` historically skipped
    the write on empty results, which means a misread that the fix
    has corrected to "actually no event" would silently persist on
    disk.  Rescan mode is the new source of truth, so we write
    ``"events": []`` when that's what the scanner produces.
  * Captures the OLD event count before scanning each clip, and prints
    a console summary + writes ``_rescan_diff.json`` at the end so the
    operator (and downstream tooling) can see exactly which clips got
    different numbers of events than before.
  * Never touches filenames or folder structure.

Diff criterion is intentionally simple: a clip is "changed" when its
total event count (kill + death + assist) differs from the count in
the prior ``.events.json``.  Timestamp wobble on the same number of
events isn't flagged — that's noise from refinement, not a real
correctness change.

Usage:
    python tools/rescan_events.py recordings\\Atlas --hwaccel cuda
    python tools/rescan_events.py recordings\\Atlas --include deaths --workers 4
    python tools/rescan_events.py recordings\\Atlas --dry-run
    python tools/rescan_events.py recordings\\Atlas --diff-out my_diff.json

The scan is non-recursive.  Point it at one per-god folder at a time —
that mirrors how you'd typically import clips into Vegas anyway, and
keeps the diff sidecar scoped to one montage's worth of clips.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Make sure the repo root is on sys.path regardless of where the script
# is invoked from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.kda_reader import KdaReader
from tools.vod_detector import VodDetector, VodDetectorOptions, VodDetectorError

# Reuse the helpers from extract_events.py so behaviour stays in lockstep.
# ``find_mp4s`` walks one folder non-recursively (exactly what we want),
# ``render_events_json`` produces the HighlightBuilder-compatible JSON
# layout, ``events_json_path`` translates <video>.mp4 → <video>.events.json.
from tools.extract_events import (
    find_mp4s,
    render_events_json,
    events_json_path,
    _parse_include,
    DEFAULT_DATA_DIR,
    DEFAULT_TESSERACT_WIN,
)


# --- Constants -------------------------------------------------------------

# Default name for the sidecar diff file written into the scanned folder.
# Underscore prefix so it sorts to the top of directory listings and is
# obviously not a regular .events.json file.  Override via --diff-out.
DEFAULT_DIFF_FILENAME = "_rescan_diff.json"


# --- Argument parsing ------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Rescan a folder of recordings and update their .events.json "
            "files in place. Reports which clips' event count changed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "folder",
        type=Path,
        help="Folder of .mp4 recordings to rescan (non-recursive).",
    )
    p.add_argument(
        "--include",
        default="deaths",
        help=(
            "Comma-separated extra event types to include. Options: "
            "deaths, assists, all. Default: 'deaths' to match the "
            "process_recordings.py defaults (kills + deaths)."
        ),
    )
    p.add_argument(
        "--diff-out",
        type=Path,
        default=None,
        help=(
            "Where to write the rescan diff sidecar JSON.  Default: "
            f"<folder>/{DEFAULT_DIFF_FILENAME}.  Use this if you want "
            "the sidecar somewhere other than the scanned folder."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show which clips would be scanned and what the prior "
            "event counts are, without writing any files.  Useful for "
            "confirming you've pointed at the right folder."
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
            "Skip the binary-search refinement step.  Events emit at "
            "the coarse-scan timestamp with widened pre/post windows."
        ),
    )
    p.add_argument(
        "--no-ffmpeg-crop",
        action="store_true",
        help="Disable the ffmpeg-side HUD-strip crop (debugging only).",
    )
    p.add_argument(
        "--no-lobby-skip",
        action="store_true",
        help="Disable the pre-match lobby sparse-sampling optimization.",
    )
    p.add_argument(
        "--no-merge-overlaps",
        action="store_true",
        help=(
            "Keep events with overlapping pre/post clip windows as "
            "separate entries.  By default a kill+death trade collapses "
            "into one wider event."
        ),
    )
    p.add_argument(
        "--hwaccel",
        default=None,
        help=(
            "ffmpeg -hwaccel value for GPU video decode.  'cuda' on "
            "NVIDIA is the biggest single speedup.  Default: off."
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
            f"Path to tesseract.exe (default: {DEFAULT_TESSERACT_WIN} on "
            f"Windows, None elsewhere)."
        ),
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"HatmasBot data directory.  Default: {DEFAULT_DATA_DIR}",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of videos to scan concurrently (default: 1). "
            "3-4 is the sweet spot on a typical gaming rig."
        ),
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print per-sample detection progress.",
    )
    return p


# --- Prior event count snapshot --------------------------------------------

def _count_prior_events(json_path: Path) -> Optional[dict]:
    """Read an existing ``.events.json`` and return a small count summary.

    Returns ``None`` if the file doesn't exist (first-time scan for this
    clip).  Returns a dict with per-type counts otherwise.  Tolerates
    malformed JSON by treating it as "exists but unparseable" — we still
    want to overwrite it but should flag that to the operator.
    """
    if not json_path.exists():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "total": -1,
            "kills": -1,
            "deaths": -1,
            "assists": -1,
            "parse_error": str(e),
        }
    events = data.get("events") or []
    k = sum(1 for e in events if e.get("type") == "kill")
    d = sum(1 for e in events if e.get("type") == "death")
    a = sum(1 for e in events if e.get("type") == "assist")
    return {
        "total": len(events),
        "kills": k,
        "deaths": d,
        "assists": a,
        "parse_error": None,
    }


def _new_event_counts(events: list[dict]) -> dict:
    """Same shape as ``_count_prior_events`` but for fresh detector output."""
    k = sum(1 for e in events if e.get("type") == "kill")
    d = sum(1 for e in events if e.get("type") == "death")
    a = sum(1 for e in events if e.get("type") == "assist")
    return {
        "total": k + d + a,
        "kills": k,
        "deaths": d,
        "assists": a,
        "parse_error": None,
    }


def _classify_change(prior: Optional[dict], new: dict) -> str:
    """Map (prior, new) counts to a change category.

    Categories:
      ``new``        — there was no prior .events.json at all (first scan
                       for this clip).  Always treated as a change.
      ``unchanged``  — total event count is identical to prior.  Timestamp
                       wobble inside the events list is ignored per the
                       agreed diff criterion.
      ``added``      — strictly more events than before.  The fix recovered
                       previously-missed events.
      ``removed``    — strictly fewer events than before.  The fix cleared
                       previously-emitted misreads.
      ``mixed``      — total count is the same but at least one of K/D/A
                       counts moved (e.g. a kill misread as a death now
                       reads correctly).  Worth flagging.
      ``parse_recover`` — the prior file was unparseable; treat as a change.
    """
    if prior is None:
        return "new"
    if prior.get("parse_error"):
        return "parse_recover"
    if prior["total"] == new["total"]:
        # Same total — drill in to see if per-type breakdown shifted.
        for key in ("kills", "deaths", "assists"):
            if prior[key] != new[key]:
                return "mixed"
        return "unchanged"
    if new["total"] > prior["total"]:
        return "added"
    return "removed"


# --- Per-video work --------------------------------------------------------

def _write_payload(
    video: Path,
    events: list[dict],
    gods_seen: list[str],
) -> None:
    """Always write the .events.json, even with zero events.  Diverges
    from extract_events.py which historically skipped the write on empty.
    """
    source_video = str(video.resolve())
    payload = render_events_json(source_video, events, gods_seen=gods_seen)
    events_json_path(video).write_text(payload, encoding="utf-8")


# --- Parallel worker state (mirrors extract_events.py) --------------------

_WORKER_DETECTOR: Optional["VodDetector"] = None


def _worker_init(
    data_dir_str: str,
    tesseract_path: Optional[str],
    opts_dict: dict,
) -> None:
    global _WORKER_DETECTOR
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from core.kda_reader import KdaReader as _KdaReader
    from tools.vod_detector import (
        VodDetector as _VodDetector,
        VodDetectorOptions as _VodDetectorOptions,
    )

    reader = _KdaReader(
        data_dir=Path(data_dir_str),
        tesseract_path=tesseract_path,
        debug=False,
    )
    opts = _VodDetectorOptions(**opts_dict)
    _WORKER_DETECTOR = _VodDetector(reader, opts)


def _worker_scan(video_path_str: str) -> dict:
    if _WORKER_DETECTOR is None:
        return {
            "path": video_path_str,
            "events": [],
            "gods_seen": [],
            "error": "Worker not initialized",
        }
    try:
        events = _WORKER_DETECTOR.detect(video_path_str)
        gods_seen = list(getattr(_WORKER_DETECTOR, "gods_seen", []) or [])
        return {
            "path": video_path_str,
            "events": events,
            "gods_seen": gods_seen,
            "error": None,
        }
    except Exception as e:
        return {
            "path": video_path_str,
            "events": [],
            "gods_seen": [],
            "error": f"{type(e).__name__}: {e}",
        }


# --- Main driver -----------------------------------------------------------

def _change_label(category: str) -> str:
    """Stable short labels for the console summary table."""
    return {
        "new":           "NEW",
        "added":         "ADDED",
        "removed":       "REMOVED",
        "mixed":         "MIXED",
        "parse_recover": "RECOVERED",
        "unchanged":     "unchanged",
    }.get(category, category.upper())


def _format_counts(c: dict) -> str:
    """e.g. '5 (K=3 D=1 A=1)' for the report tables."""
    if c.get("parse_error"):
        return "<unparseable>"
    return f"{c['total']} (K={c['kills']} D={c['deaths']} A={c['assists']})"


def _print_summary(records: list[dict]) -> None:
    """Render the changed-clips summary table on stdout."""
    changed = [r for r in records if r["category"] != "unchanged"]
    print()
    print("=" * 78)
    print(f"  Rescan summary: {len(records)} clip(s) scanned, "
          f"{len(changed)} with changed event counts.")
    print("=" * 78)
    if not changed:
        print("  (no changes — every clip's event count matches its prior .events.json)")
        return

    # Column widths
    max_name = max(len(r["clip"]) for r in changed)
    col_clip = max(8, min(max_name, 40))
    print(f"  {'CHANGE':<10} {'CLIP':<{col_clip}} {'PRIOR':<18} {'NEW':<18}")
    print(f"  {'-'*10} {'-'*col_clip} {'-'*18} {'-'*18}")
    for r in changed:
        label = _change_label(r["category"])
        clip = r["clip"]
        if len(clip) > col_clip:
            clip = clip[:col_clip - 1] + "…"
        prior_s = _format_counts(r["prior_counts"]) if r["prior_counts"] else "—"
        new_s = _format_counts(r["new_counts"])
        print(f"  {label:<10} {clip:<{col_clip}} {prior_s:<18} {new_s:<18}")


def _write_diff_sidecar(
    diff_path: Path,
    folder: Path,
    records: list[dict],
) -> None:
    """Write the machine-readable rescan diff JSON.

    Schema (versioned so downstream Vegas script can refuse mismatches):

        {
          "schema_version": 1,
          "scanned_at":     ISO timestamp,
          "folder":         "<absolute path>",
          "total_clips":    N,
          "changed_clips":  M,
          "clips": [
             {
               "clip": "Atlas-6.mp4",
               "video_path": "<absolute>",
               "events_json": "<absolute>",
               "category": "added" | "removed" | "mixed" | "new" | ...,
               "prior_counts": {...} | null,
               "new_counts":   {...},
               "gods_seen":    [...]
             },
             ...
          ]
        }

    Only clips with category != "unchanged" appear in ``clips`` so
    consumers can iterate the array directly without filtering.  Use
    ``total_clips`` to know how many were inspected.
    """
    payload = {
        "schema_version": 1,
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "folder": str(folder.resolve()),
        "total_clips": len(records),
        "changed_clips": sum(1 for r in records if r["category"] != "unchanged"),
        "clips": [
            {
                "clip": r["clip"],
                "video_path": r["video_path"],
                "events_json": r["events_json"],
                "category": r["category"],
                "prior_counts": r["prior_counts"],
                "new_counts": r["new_counts"],
                "gods_seen": r["gods_seen"],
            }
            for r in records
            if r["category"] != "unchanged"
        ],
    }
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        include_deaths, include_assists = _parse_include(args.include)
    except argparse.ArgumentTypeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.workers < 1:
        print("error: --workers must be >= 1", file=sys.stderr)
        return 2

    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(name)s: %(message)s")

    try:
        mp4s = find_mp4s(args.folder)
    except (FileNotFoundError, NotADirectoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not mp4s:
        print(f"No .mp4 files found in {args.folder}")
        return 0

    # Resolve Tesseract path the same way extract_events.py does.
    tesseract_path = args.tesseract
    if tesseract_path is None and Path(DEFAULT_TESSERACT_WIN).exists():
        tesseract_path = DEFAULT_TESSERACT_WIN

    reader = KdaReader(
        data_dir=args.data_dir,
        tesseract_path=tesseract_path,
        debug=False,
    )
    if not reader.is_ready:
        print(
            "error: neither Tesseract OCR nor a digit template library is "
            "available.  Install Tesseract or populate "
            f"{args.data_dir / 'digit_templates'}.",
            file=sys.stderr,
        )
        return 2

    opts = VodDetectorOptions(
        coarse_interval=args.coarse,
        refine_precision=args.precision,
        include_deaths=include_deaths,
        include_assists=include_assists,
        # We deliberately never enable template enrollment from the
        # rescan path — the live + first-scan paths handle that and we
        # don't want a rescan to mutate the template library.
        enroll_templates=False,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        verbose=args.verbose,
        no_refine=args.no_refine,
        ffmpeg_crop=not args.no_ffmpeg_crop,
        lobby_skip=not args.no_lobby_skip,
        hwaccel=args.hwaccel,
        merge_overlaps=not args.no_merge_overlaps,
    )

    diff_path = args.diff_out or (args.folder / DEFAULT_DIFF_FILENAME)

    include_label = ["kills"]
    if include_deaths:
        include_label.append("deaths")
    if include_assists:
        include_label.append("assists")
    worker_label = (
        "1 worker (serial)"
        if args.workers == 1
        else f"{args.workers} workers (parallel)"
    )
    print(f"Rescanning {len(mp4s)} clip(s) in {args.folder}")
    print(f"  event types: {', '.join(include_label)}")
    print(f"  workers:     {worker_label}")
    print(f"  diff file:   {diff_path}")
    if args.dry_run:
        print("  (DRY RUN — no .events.json files will be written)")
    print()

    # Snapshot the prior event counts BEFORE we touch any file.  This is
    # the canonical "before" picture for the diff report.
    prior_snapshots: dict[Path, Optional[dict]] = {}
    for video in mp4s:
        prior_snapshots[video] = _count_prior_events(events_json_path(video))

    # Dry-run path: just print what we'd do and exit.
    if args.dry_run:
        print(f"{'CLIP':<40} {'PRIOR':<30}")
        print(f"{'-'*40} {'-'*30}")
        for video in mp4s:
            prior = prior_snapshots[video]
            prior_s = (
                _format_counts(prior) if prior is not None else "(no .events.json)"
            )
            print(f"{video.name:<40} {prior_s:<30}")
        print()
        print(f"Dry run complete. {len(mp4s)} clip(s) would be rescanned.")
        return 0

    # Real run — drive the scan.
    records: list[dict] = []
    errors = 0

    if args.workers > 1:
        opts_dict = asdict(opts)
        opts_dict["progress_callback"] = None
        t0 = time.time()
        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=_worker_init,
            initargs=(str(args.data_dir), tesseract_path, opts_dict),
        ) as pool:
            futures = {
                pool.submit(_worker_scan, str(v)): v for v in mp4s
            }
            done = 0
            for fut in as_completed(futures):
                video = futures[fut]
                done += 1
                try:
                    result = fut.result()
                except Exception as e:
                    print(f"[{done}/{len(mp4s)}] {video.name} -> "
                          f"ERROR: {type(e).__name__}: {e}")
                    errors += 1
                    continue
                if result["error"]:
                    print(f"[{done}/{len(mp4s)}] {video.name} -> "
                          f"ERROR: {result['error']}")
                    errors += 1
                    continue

                new_counts = _new_event_counts(result["events"])
                prior = prior_snapshots[video]
                category = _classify_change(prior, new_counts)

                # ALWAYS write the JSON — even empty events — so the
                # rescan is the new truth for this clip.
                _write_payload(video, result["events"], result["gods_seen"])

                records.append({
                    "clip": video.name,
                    "video_path": str(video.resolve()),
                    "events_json": str(events_json_path(video).resolve()),
                    "category": category,
                    "prior_counts": prior,
                    "new_counts": new_counts,
                    "gods_seen": result["gods_seen"],
                })
                tag = _change_label(category)
                print(f"[{done}/{len(mp4s)}] {video.name} -> "
                      f"{tag}  prior={_format_counts(prior) if prior else '—'}  "
                      f"new={_format_counts(new_counts)}")

        elapsed = time.time() - t0
        print(f"\nParallel batch finished in "
              f"{int(elapsed // 60)}m {int(elapsed % 60)}s.")
    else:
        detector = VodDetector(reader, opts)
        for i, video in enumerate(mp4s, 1):
            prefix = f"[{i}/{len(mp4s)}] {video.name}"

            # Per-video progress ticker like extract_events.py uses.
            def _on_progress(scan_t: float, duration: float) -> None:
                pct = 100.0 * scan_t / duration if duration > 0 else 0.0
                line = (
                    f"{prefix} -> scanning... {scan_t:.0f}s / "
                    f"{duration:.0f}s ({pct:.0f}%)"
                )
                sys.stdout.write("\r" + line)
                sys.stdout.flush()

            sys.stdout.write(f"{prefix} -> scanning...")
            sys.stdout.flush()
            detector.opts.progress_callback = _on_progress
            try:
                events = detector.detect(video)
            except VodDetectorError as e:
                sys.stdout.write("\n")
                print(f"  ERROR: {e}")
                errors += 1
                continue
            except Exception as e:
                sys.stdout.write("\n")
                print(f"  ERROR: {type(e).__name__}: {e}")
                traceback.print_exc()
                errors += 1
                continue
            finally:
                detector.opts.progress_callback = None

            # Clear progress line.
            sys.stdout.write("\r" + " " * 100 + "\r")
            sys.stdout.flush()

            new_counts = _new_event_counts(events)
            prior = prior_snapshots[video]
            category = _classify_change(prior, new_counts)
            gods_seen = list(getattr(detector, "gods_seen", []) or [])

            _write_payload(video, events, gods_seen)

            records.append({
                "clip": video.name,
                "video_path": str(video.resolve()),
                "events_json": str(events_json_path(video).resolve()),
                "category": category,
                "prior_counts": prior,
                "new_counts": new_counts,
                "gods_seen": gods_seen,
            })
            tag = _change_label(category)
            print(f"{prefix} -> {tag}  "
                  f"prior={_format_counts(prior) if prior else '—'}  "
                  f"new={_format_counts(new_counts)}")

    # Final outputs: console summary table + sidecar JSON.
    _print_summary(records)
    _write_diff_sidecar(diff_path, args.folder, records)
    print()
    print(f"Diff sidecar written: {diff_path}")
    if errors:
        print(f"\n{errors} clip(s) errored during scan — see messages above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
