#!/usr/bin/env python3
"""
extract_events.py
=================
Scans a folder of Smite 2 OBS recordings and writes a sibling
``<basename>.events.json`` next to each ``.mp4``.  The JSON matches
HighlightBuilder.cs's hand-rolled mini-parser (see 2026-04-16
17-46-49.events.json for a worked example) and lists the kill /
death / assist moments detected by the existing HatmasBot KDA
detector, repackaged to run offline against recordings.

Usage:
    python tools/extract_events.py <folder> [options]

    # Default — kills only, skip anything that already has events.
    python tools/extract_events.py "C:\\Users\\james\\Videos"

    # Include deaths and assists.
    python tools/extract_events.py "C:\\Users\\james\\Videos" --include deaths,assists

    # Redo files that already have .events.json.
    python tools/extract_events.py "C:\\Users\\james\\Videos" --overwrite

    # Report what would happen without writing anything.
    python tools/extract_events.py "C:\\Users\\james\\Videos" --dry-run

See --help for the full option list.  This tool does not touch Sony
Vegas; the generated JSON is consumed by HighlightBuilder.cs inside
Vegas to build the vertical highlight timeline.
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

# Make sure the repo root is on sys.path regardless of where the script
# is invoked from (e.g. `python tools/extract_events.py ...` vs
# `python -m tools.extract_events`).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.kda_reader import KdaReader
from tools.vod_detector import VodDetector, VodDetectorOptions, VodDetectorError

# We intentionally avoid importing core.config here — that module pulls
# in bot credentials, OBS settings, and optional config_local.py.  The
# CLI needs only a data directory and (optionally) a Tesseract path.
# Resolve the same defaults that core.config resolves, without its
# side effects.
DEFAULT_DATA_DIR = _REPO_ROOT / "data"
DEFAULT_TESSERACT_WIN = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


# --- Argument parsing ------------------------------------------------------

def _parse_include(raw: str) -> tuple[bool, bool]:
    """Parse --include into (include_deaths, include_assists).

    Accepts comma-separated lists: "deaths", "assists", "deaths,assists",
    "all", or empty (kills only).  Case-insensitive.  Rejects unknown
    tokens loudly so typos don't silently include nothing.
    """
    if not raw:
        return False, False

    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    include_deaths = False
    include_assists = False

    for tok in tokens:
        if tok == "deaths":
            include_deaths = True
        elif tok == "assists":
            include_assists = True
        elif tok == "all":
            include_deaths = True
            include_assists = True
        else:
            raise argparse.ArgumentTypeError(
                f"Unknown --include value {tok!r}. "
                f"Valid: deaths, assists, all."
            )

    return include_deaths, include_assists


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Run the HatmasBot KDA detector over a folder of Smite 2 "
            "recordings and emit <name>.events.json files for HighlightBuilder."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "folder",
        type=Path,
        help="Folder of .mp4 recordings to scan (non-recursive).",
    )
    p.add_argument(
        "--include",
        default="",
        help=(
            "Comma-separated extra event types to include. "
            "Options: deaths, assists, all. Default: kills only."
        ),
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-generate .events.json files that already exist.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing any files.",
    )
    p.add_argument(
        "--enroll-templates",
        action="store_true",
        help=(
            "Save confirmed digit crops back into data/digit_templates/ "
            "(same behavior as the live detector).  Off by default so "
            "batch runs don't mutate the live library."
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
            "Skip the binary-search refinement step.  Events emit at the "
            "coarse-scan timestamp with widened pre_sec=7.5 and "
            "post_sec=6.5 to cover the +/- half-interval uncertainty.  "
            "Trades ~0.5-2s clip offset for much faster batch runs — "
            "the highlight will still cover the real moment."
        ),
    )
    p.add_argument(
        "--no-ffmpeg-crop",
        action="store_true",
        help=(
            "Disable the ffmpeg-side HUD-strip crop.  Only useful for "
            "debugging a recording where regions appear misaligned; "
            "normal runs should leave this on."
        ),
    )
    p.add_argument(
        "--no-lobby-skip",
        action="store_true",
        help=(
            "Disable the pre-match lobby sparse-sampling optimization.  "
            "Use if the detector appears to skip over real events early "
            "in a recording."
        ),
    )
    p.add_argument(
        "--no-merge-overlaps",
        action="store_true",
        help=(
            "Keep events with overlapping pre/post clip windows as "
            "separate entries.  By default a kill+death trade (or any "
            "back-to-back sequence within each other's pre/post window) "
            "collapses into one wider event so Vegas imports a single "
            "clip covering both moments instead of two overlapping "
            "clips.  The merged event anchors on the highest-priority "
            "type (kill > death > assist)."
        ),
    )
    p.add_argument(
        "--hwaccel",
        default=None,
        help=(
            "ffmpeg -hwaccel value for GPU video decode.  Biggest single "
            "speedup available for HEVC OBS recordings — 'cuda' on NVIDIA "
            "(recommended), 'd3d11va' / 'dxva2' on other Windows GPUs, "
            "'auto' to let ffmpeg pick.  Default: off (software decode, "
            "matches the live plugin).  If the GPU can't decode a given "
            "stream ffmpeg will transparently fall back to software."
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
            f"Windows, None elsewhere).  Tesseract is optional — the "
            f"digit-template matcher handles most reads on its own."
        ),
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=(
            "HatmasBot data directory (holds digit_templates/).  "
            f"Default: {DEFAULT_DATA_DIR}"
        ),
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of videos to scan concurrently (default: 1). "
            "Each worker runs its own KdaReader in a subprocess; 3-4 is "
            "usually the sweet spot on a typical gaming rig.  Not "
            "compatible with --enroll-templates (templates folder would "
            "race)."
        ),
    )
    p.add_argument(
        "--debug-misreads",
        nargs="?",
        const="data/vod_debug",
        default=None,
        metavar="DIR",
        help=(
            "Save the offending frame + raw KDA crop every time the "
            "detector rejects a sample at the KDA-level (partial "
            "decrease, max-jump rejection — anything that prints '(not "
            "a clean 0/0/0 reset)' or 'KDA jump too large').  Per-video "
            "subfolders so different scans don't collide.  Pass a path "
            "to override; bare flag uses 'data/vod_debug'.  Off by "
            "default."
        ),
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print per-sample detection progress.",
    )
    return p


# --- JSON output -----------------------------------------------------------

def render_events_json(
    source_video: str,
    events: list[dict],
    gods_seen: Optional[list[str]] = None,
) -> str:
    """Serialize the output in the same style as the worked example —
    top-level pretty, each event on its own single line.

    HighlightBuilder's mini-parser only looks for ``source_video`` and
    ``events`` so adding ``gods_seen`` as a new top-level field is
    backwards-compatible.  Downstream tools (the recording sorter,
    future analytics) read it to route the file by which god(s)
    appeared in the recording.
    """
    lines = ["{"]
    lines.append(f"  \"source_video\": {json.dumps(source_video)},")

    if gods_seen is not None:
        lines.append(f"  \"gods_seen\": {json.dumps(gods_seen)},")

    if not events:
        lines.append("  \"events\": []")
    else:
        lines.append("  \"events\": [")
        event_lines = ["    " + json.dumps(ev) for ev in events]
        lines.append(",\n".join(event_lines))
        lines.append("  ]")

    lines.append("}")
    return "\n".join(lines) + "\n"


# --- Folder scanning -------------------------------------------------------

def find_mp4s(folder: Path) -> list[Path]:
    """Return all top-level .mp4 files in ``folder``, sorted by filename.

    Case-insensitive match so Windows ``.MP4`` also works.  James's
    recordings are timestamp-named (``YYYY-MM-DD HH-MM-SS.mp4``) so
    alphabetical sort is also oldest-first.
    """
    if not folder.exists():
        raise FileNotFoundError(f"Folder does not exist: {folder}")
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    mp4s = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() == ".mp4"
    ]
    mp4s.sort(key=lambda p: p.name.lower())
    return mp4s


def events_json_path(video_path: Path) -> Path:
    """<video>.mp4 → <video>.events.json next to it."""
    return video_path.with_suffix(".events.json")


# --- Main driver -----------------------------------------------------------

def process_one(
    video: Path,
    idx: int,
    total: int,
    detector: VodDetector,
    *,
    overwrite: bool,
    dry_run: bool,
) -> tuple[str, int, int, int]:
    """Process one video.  Returns (status, kills, deaths, assists).

    ``status`` is one of: ``written``, ``skipped``, ``empty``, ``error``,
    ``would_write``, ``would_skip``.  The stdout progress line is
    printed here so the batch loop can stay concise.
    """
    out_path = events_json_path(video)
    prefix = f"[{idx}/{total}] {video.name}"

    # Already done?
    if out_path.exists() and not overwrite:
        print(f"{prefix} -> skipped (already has .events.json)")
        return ("would_skip" if dry_run else "skipped", 0, 0, 0)

    if dry_run:
        print(f"{prefix} -> would scan")
        return ("would_write", 0, 0, 0)

    # Immediate feedback — detect() on a 30-minute recording can take several
    # minutes, so print the starting line now and flush it so the user sees
    # the scan begin.  We then overwrite this line with progress ticks via
    # \r carriage returns, and finalize with the real summary below.
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

    # Attach the callback on the shared detector's options for the
    # duration of this one video.  VodDetectorOptions is a dataclass so
    # we can set the field directly; we restore None when we're done.
    detector.opts.progress_callback = _on_progress
    try:
        events = detector.detect(video)
    except VodDetectorError as e:
        sys.stdout.write("\n")
        print(f"  ERROR: {e}")
        return ("error", 0, 0, 0)
    except Exception as e:
        sys.stdout.write("\n")
        print(f"  ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        return ("error", 0, 0, 0)
    finally:
        detector.opts.progress_callback = None

    # Clear the progress line so the summary prints on a fresh row.
    sys.stdout.write("\r" + " " * 100 + "\r")
    sys.stdout.flush()

    k = sum(1 for e in events if e["type"] == "kill")
    d = sum(1 for e in events if e["type"] == "death")
    a = sum(1 for e in events if e["type"] == "assist")

    if not events:
        print(f"{prefix} -> no events detected (skipping .events.json)")
        return ("empty", 0, 0, 0)

    # Absolute path with OS-native separators.  json.dumps will escape
    # backslashes on Windows automatically.
    source_video = str(video.resolve())
    gods_seen = list(getattr(detector, "gods_seen", []) or [])
    payload = render_events_json(source_video, events, gods_seen=gods_seen)
    out_path.write_text(payload, encoding="utf-8")

    # Summary line.  Only show counts for types we actually emit so the
    # line stays honest (kills-only default won't mention deaths).
    bits = [f"{k} kills"]
    if d:
        bits.append(f"{d} deaths")
    if a:
        bits.append(f"{a} assists")
    if gods_seen:
        bits.append(f"god: {', '.join(gods_seen)}")
    print(f"{prefix} -> {', '.join(bits)}")

    return ("written", k, d, a)


# --- Parallel worker state ------------------------------------------------

# Each subprocess in the ProcessPoolExecutor lazily builds its own reader +
# detector once, on first call to ``_worker_init``, and reuses it across
# every video that worker is assigned.  This avoids re-loading the digit
# template library and god icon histograms per video.
_WORKER_DETECTOR: Optional["VodDetector"] = None


def _worker_init(
    data_dir_str: str,
    tesseract_path: Optional[str],
    opts_dict: dict,
) -> None:
    """Initializer run once per ProcessPoolExecutor worker."""
    global _WORKER_DETECTOR
    # Re-apply sys.path in the child — spawn-started workers don't
    # inherit our parent's modifications.
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
    """Scan one video inside a worker process; return a plain dict.

    Never raises — exceptions are serialized into the return value so the
    parent process can report them cleanly alongside the other results.
    """
    if _WORKER_DETECTOR is None:
        return {
            "path": video_path_str,
            "events": [],
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


def _write_events_file(
    video: Path,
    events: list[dict],
    gods_seen: Optional[list[str]] = None,
) -> tuple[int, int, int]:
    """Serialize ``events`` to the sibling .events.json.  Returns (k, d, a)."""
    k = sum(1 for e in events if e["type"] == "kill")
    d = sum(1 for e in events if e["type"] == "death")
    a = sum(1 for e in events if e["type"] == "assist")
    source_video = str(video.resolve())
    payload = render_events_json(source_video, events, gods_seen=gods_seen)
    events_json_path(video).write_text(payload, encoding="utf-8")
    return k, d, a


def _run_parallel(
    mp4s: list[Path],
    *,
    data_dir: Path,
    tesseract_path: Optional[str],
    opts: VodDetectorOptions,
    workers: int,
    overwrite: bool,
    dry_run: bool,
    include_deaths: bool,
    include_assists: bool,
) -> tuple[int, int, int, int, int, int, int]:
    """Scan videos in parallel.  Returns aggregate counters.

    Returns: (written, skipped, empty, errors, total_k, total_d, total_a).
    """
    total = len(mp4s)

    # Partition into skip vs. work upfront so the [N/M] counter stays stable.
    to_scan: list[tuple[int, Path]] = []
    written = skipped_count = empty = errors = 0
    total_k = total_d = total_a = 0

    for i, video in enumerate(mp4s, 1):
        out_path = events_json_path(video)
        if out_path.exists() and not overwrite:
            print(f"[{i}/{total}] {video.name} -> skipped (already has .events.json)")
            skipped_count += 1
            continue
        if dry_run:
            print(f"[{i}/{total}] {video.name} -> would scan")
            continue
        to_scan.append((i, video))

    if dry_run or not to_scan:
        return written, skipped_count, empty, errors, total_k, total_d, total_a

    # Prepare a picklable dict of options for the workers.
    opts_dict = asdict(opts)
    opts_dict["progress_callback"] = None  # not picklable, not needed

    print(
        f"\nDispatching {len(to_scan)} video(s) to {workers} worker(s).  "
        f"Per-video progress ticks are disabled in parallel mode; you'll "
        f"see each video's summary as it finishes."
    )

    t_batch_start = time.time()

    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(str(data_dir), tesseract_path, opts_dict),
    ) as pool:
        future_to_info = {
            pool.submit(_worker_scan, str(video)): (idx, video)
            for (idx, video) in to_scan
        }
        done_count = 0
        for future in as_completed(future_to_info):
            idx, video = future_to_info[future]
            done_count += 1
            try:
                result = future.result()
            except Exception as e:
                print(f"[{idx}/{total}] {video.name} -> ERROR: {type(e).__name__}: {e}")
                errors += 1
                continue

            if result["error"]:
                print(f"[{idx}/{total}] {video.name} -> ERROR: {result['error']}")
                errors += 1
                continue

            events = result["events"]
            gods_seen = result.get("gods_seen") or []
            if not events:
                print(f"[{idx}/{total}] {video.name} -> no events detected (skipping .events.json)")
                empty += 1
                continue

            k, d, a = _write_events_file(video, events, gods_seen=gods_seen)
            bits = [f"{k} kills"]
            if d:
                bits.append(f"{d} deaths")
            if a:
                bits.append(f"{a} assists")
            if gods_seen:
                bits.append(f"god: {', '.join(gods_seen)}")
            suffix = f"  ({done_count}/{len(to_scan)} scans done)"
            print(f"[{idx}/{total}] {video.name} -> {', '.join(bits)}{suffix}")
            written += 1
            total_k += k
            total_d += d
            total_a += a

    elapsed = time.time() - t_batch_start
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    print(f"\nParallel batch finished in {mins}m {secs}s.")

    return written, skipped_count, empty, errors, total_k, total_d, total_a


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        include_deaths, include_assists = _parse_include(args.include)
    except argparse.ArgumentTypeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.workers < 1:
        print("error: --workers must be >= 1", file=sys.stderr)
        return 2
    if args.workers > 1 and args.enroll_templates:
        print(
            "error: --enroll-templates is incompatible with --workers > 1 "
            "(the digit templates folder would race across workers).  "
            "Run the enrollment pass serially first, then re-run with "
            "--workers for bulk scanning.",
            file=sys.stderr,
        )
        return 2

    # Logging setup — the reader + detector log to these names.
    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(name)s: %(message)s",
    )

    try:
        mp4s = find_mp4s(args.folder)
    except (FileNotFoundError, NotADirectoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not mp4s:
        print(f"No .mp4 files found in {args.folder}")
        return 0

    # Resolve Tesseract path — explicit flag wins; otherwise use the
    # Windows default if it exists on disk; otherwise leave unset.
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
        enroll_templates=args.enroll_templates,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        verbose=args.verbose,
        no_refine=args.no_refine,
        ffmpeg_crop=not args.no_ffmpeg_crop,
        lobby_skip=not args.no_lobby_skip,
        hwaccel=args.hwaccel,
        merge_overlaps=not args.no_merge_overlaps,
        misread_debug_dir=(
            Path(args.debug_misreads) if args.debug_misreads else None
        ),
    )

    # Header — shared by both serial and parallel paths.
    worker_label = (
        "1 worker (serial)"
        if args.workers == 1
        else f"{args.workers} workers (parallel)"
    )
    print(
        f"Processing {len(mp4s)} video(s) with {worker_label} "
        f"({'dry run — no files will be written' if args.dry_run else 'writing to sibling .events.json'})"
    )

    include_label = ["kills"]
    if include_deaths:
        include_label.append("deaths")
    if include_assists:
        include_label.append("assists")
    print(f"Emitting event types: {', '.join(include_label)}")
    print()

    written = 0
    skipped_count = 0
    empty = 0
    errors = 0
    total_kills = total_deaths = total_assists = 0

    if args.workers > 1:
        (
            written,
            skipped_count,
            empty,
            errors,
            total_kills,
            total_deaths,
            total_assists,
        ) = _run_parallel(
            mp4s,
            data_dir=args.data_dir,
            tesseract_path=tesseract_path,
            opts=opts,
            workers=args.workers,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            include_deaths=include_deaths,
            include_assists=include_assists,
        )
    else:
        detector = VodDetector(reader, opts)
        for i, video in enumerate(mp4s, 1):
            status, k, d, a = process_one(
                video,
                i,
                len(mp4s),
                detector,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
            if status == "written":
                written += 1
                total_kills += k
                total_deaths += d
                total_assists += a
            elif status in ("skipped", "would_skip"):
                skipped_count += 1
            elif status == "empty":
                empty += 1
            elif status == "error":
                errors += 1

    print()
    if args.dry_run:
        print(
            f"Dry run complete: {len(mp4s)} video(s) inspected, "
            f"{skipped_count} already have .events.json."
        )
    else:
        bits = [f"{written} written"]
        if skipped_count:
            bits.append(f"{skipped_count} skipped")
        if empty:
            bits.append(f"{empty} empty")
        if errors:
            bits.append(f"{errors} errors")
        print(f"Done: {', '.join(bits)}.")
        if written:
            detail = [f"{total_kills} kills"]
            if include_deaths:
                detail.append(f"{total_deaths} deaths")
            if include_assists:
                detail.append(f"{total_assists} assists")
            print(f"Totals across successful scans: {', '.join(detail)}.")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
