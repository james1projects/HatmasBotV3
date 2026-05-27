#!/usr/bin/env python3
"""
diagnose_god_detection.py
=========================
Pull a handful of frames out of a recording and tell you, per frame:

  * whether the gameplay check passes
  * whether the overlay (store/scoreboard) check is clear
  * what the god matcher's top 3 candidates are, with confidence scores
  * whether each candidate would have been accepted by the matcher's
    threshold rules

Plus saves the cropped portrait region from each sample so you can
eyeball whether the matcher is even looking at the right pixels.  Use
this to figure out why a recording landed in ``recordings/unknown/``:
the failure is usually one of:

  1. Gameplay check never passes (HUD layout shifted, recording is
     menu-only, etc.) → no gameplay frames means no god ID attempts.
  2. Gameplay check passes but matcher confidence stays below
     threshold → the played god isn't in the icon library, or the
     portrait region is misaligned.
  3. Top candidate keeps flickering between gods → 3-frame
     confirmation rule never fires.

Usage:
    python tools/diagnose_god_detection.py "path/to/video.mp4"
    python tools/diagnose_god_detection.py "path/to/video.mp4" --samples 8
    python tools/diagnose_god_detection.py "path/to/video.mp4" \
        --output-dir data/god_diag

The script writes ``<video_basename>_diag/`` next to the input video by
default with one ``portrait_t<seconds>.png`` per sample plus a
``frame_t<seconds>.png`` (full frame, for context).  Pass
``--output-dir`` to redirect.
"""

from __future__ import annotations

import argparse
import io
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.god_matcher import (
    GodMatcher,
    MARGIN_GAP,
    MIN_CONFIDENCE,
    MIN_CONFIDENCE_MARGIN,
    PORTRAIT_REGION,
)
from core.kda_reader import KdaReader


DEFAULT_DATA_DIR = _REPO_ROOT / "data"
DEFAULT_OVERLAY_ICONS_DIR = _REPO_ROOT / "Custom God Icons"
DEFAULT_REFERENCE_ICONS_DIR = _REPO_ROOT / "Portrait_Source"
DEFAULT_TESSERACT_WIN = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _probe_duration(video: Path, ffprobe: str) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        print(f"error: could not run {ffprobe!r}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"ffprobe failed: {result.stderr.strip()}", file=sys.stderr)
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _extract_frame(
    video: Path,
    t: float,
    ffmpeg: str,
    hwaccel: Optional[str] = None,
) -> Optional[Image.Image]:
    cmd = [ffmpeg, "-v", "error"]
    if hwaccel:
        cmd.extend(["-hwaccel", hwaccel])
    cmd.extend([
        "-ss", f"{max(0.0, t):.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-f", "image2pipe",
        "-c:v", "png",
        "-",
    ])
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        print(f"error: could not run {ffmpeg!r}", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def _classify_acceptance(
    best_score: float, second_score: float
) -> str:
    """Mirror GodMatcher.identify()'s acceptance logic so we can label
    each candidate with WHY it would or wouldn't be accepted.
    """
    if best_score >= MIN_CONFIDENCE:
        return f"accepted (above {MIN_CONFIDENCE:.2f} threshold)"
    margin = best_score - second_score
    if best_score >= MIN_CONFIDENCE_MARGIN and margin >= MARGIN_GAP:
        return (
            f"accepted (margin: {best_score:.2f} >= {MIN_CONFIDENCE_MARGIN:.2f} "
            f"and gap {margin:.2f} >= {MARGIN_GAP:.2f})"
        )
    if best_score < MIN_CONFIDENCE_MARGIN:
        return (
            f"REJECTED (score {best_score:.2f} below margin floor "
            f"{MIN_CONFIDENCE_MARGIN:.2f})"
        )
    return (
        f"REJECTED (score {best_score:.2f} between thresholds, "
        f"margin gap {margin:.2f} < {MARGIN_GAP:.2f})"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sample frames from a recording and show what the god "
            "matcher would have done with them."
        ),
    )
    parser.add_argument("video", type=Path)
    parser.add_argument(
        "--samples",
        type=int,
        default=6,
        help="Number of evenly-spaced frames to sample (default: 6).",
    )
    parser.add_argument(
        "--start-pct",
        type=float,
        default=0.10,
        help="First sample at this fraction of duration (default: 0.10).",
    )
    parser.add_argument(
        "--end-pct",
        type=float,
        default=0.85,
        help="Last sample at this fraction of duration (default: 0.85).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Where to write portrait crops + sampled frames.  Default: "
            "<video_dir>/<video_stem>_diag/."
        ),
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
    )
    parser.add_argument(
        "--overlay-icons-dir",
        type=Path,
        default=DEFAULT_OVERLAY_ICONS_DIR,
        help=(
            "Custom OBS overlay icons directory (default: "
            f"{DEFAULT_OVERLAY_ICONS_DIR}).  Pass an empty string to "
            f"disable overlay matching."
        ),
    )
    parser.add_argument(
        "--reference-icons-dir",
        type=Path,
        default=DEFAULT_REFERENCE_ICONS_DIR,
        help=(
            "Capture-from-recording reference icons directory (default: "
            f"{DEFAULT_REFERENCE_ICONS_DIR}).  Pass an empty string to "
            f"disable reference matching."
        ),
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--tesseract", default=None)
    parser.add_argument(
        "--hwaccel",
        default=None,
        help=(
            "ffmpeg -hwaccel value (e.g. 'cuda').  Mirror what the "
            "actual VOD scan is using to reproduce its scoring exactly. "
            "Default: software decode (matches the live plugin)."
        ),
    )
    args = parser.parse_args(argv)

    if not args.video.exists():
        print(f"error: video not found: {args.video}", file=sys.stderr)
        return 2

    duration = _probe_duration(args.video, args.ffprobe)
    if duration is None or duration <= 0:
        print("error: could not probe video duration", file=sys.stderr)
        return 2

    out_dir = args.output_dir or args.video.with_name(
        f"{args.video.stem}_diag"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve Tesseract for the KDA reader so gameplay/overlay checks work.
    tesseract_path = args.tesseract
    if tesseract_path is None and Path(DEFAULT_TESSERACT_WIN).exists():
        tesseract_path = DEFAULT_TESSERACT_WIN

    logging.basicConfig(level=logging.WARNING)

    reader = KdaReader(
        data_dir=args.data_dir,
        tesseract_path=tesseract_path,
        debug=False,
    )

    overlay_dir = (
        None if str(args.overlay_icons_dir) == ""
        else args.overlay_icons_dir
    )
    reference_dir = (
        None if str(args.reference_icons_dir) == ""
        else args.reference_icons_dir
    )
    matcher = GodMatcher(
        icons_dir=args.data_dir / "god_icons",
        overlay_icons_dir=overlay_dir,
        reference_icons_dir=reference_dir,
    )
    if not matcher.load_icons():
        print(
            "WARNING: god matcher loaded zero icons. "
            f"Check {args.data_dir / 'god_icons'} exists and has files.",
            file=sys.stderr,
        )
        return 2

    print(f"Video:         {args.video}")
    print(f"Duration:      {duration:.1f}s")
    print(
        f"God library:   {matcher.god_count} unique gods, "
        f"{matcher.icon_count} fingerprints"
    )
    print(f"Output folder: {out_dir}")
    print()

    # Build the sample timestamps.
    if args.samples < 1:
        print("error: --samples must be >= 1", file=sys.stderr)
        return 2
    if args.samples == 1:
        sample_ts = [duration * (args.start_pct + args.end_pct) / 2]
    else:
        step = (args.end_pct - args.start_pct) / (args.samples - 1)
        sample_ts = [
            duration * (args.start_pct + step * i)
            for i in range(args.samples)
        ]

    # Per-frame report.
    confirmed_candidates: dict[str, int] = {}
    gameplay_passes = 0
    overlay_clears = 0

    for ts in sample_ts:
        print(f"=== t={ts:6.1f}s ({ts/duration*100:4.1f}% of duration) ===")
        img = _extract_frame(args.video, ts, args.ffmpeg, hwaccel=args.hwaccel)
        if img is None:
            print("  could not extract frame")
            continue

        # Save full frame for context.
        full_path = out_dir / f"frame_t{ts:07.1f}.png"
        img.save(str(full_path))

        # Run the same gameplay + overlay checks that vod_detector uses.
        img_array = np.array(img)
        gameplay = reader.is_gameplay_screen(img_array)
        print(f"  gameplay check:  {'PASS' if gameplay else 'FAIL'}")
        if gameplay:
            gameplay_passes += 1

        overlay_open = reader.is_overlay_open(img_array)
        print(f"  overlay open:    {'YES (skipped)' if overlay_open else 'no'}")
        if not overlay_open:
            overlay_clears += 1

        # Save the portrait crop regardless — useful even if gameplay
        # check failed, because it shows what the matcher *would* see.
        portrait = img.crop(PORTRAIT_REGION)
        portrait_path = out_dir / f"portrait_t{ts:07.1f}.png"
        # Upscale 4x for easier visual inspection.
        portrait_4x = portrait.resize(
            (portrait.size[0] * 4, portrait.size[1] * 4),
            Image.NEAREST,
        )
        portrait_4x.save(str(portrait_path))

        # Run the matcher's top-N regardless of gameplay state.  Even if
        # the gameplay check failed, knowing what the matcher would
        # have said tells us whether it's a gameplay-detection problem
        # or a god-not-in-library problem.
        top3 = matcher.identify_top_n(img, n=3)
        if top3:
            best_score = top3[0][1]
            second_score = top3[1][1] if len(top3) > 1 else -1.0
            print("  top 3 god matches:")
            for i, (name, score) in enumerate(top3):
                marker = " <-- best" if i == 0 else ""
                print(f"    {name:25s} {score:.3f}{marker}")
            verdict = _classify_acceptance(best_score, second_score)
            print(f"  matcher verdict: {verdict}")
            if "accepted" in verdict:
                confirmed_candidates[top3[0][0]] = (
                    confirmed_candidates.get(top3[0][0], 0) + 1
                )
        else:
            print("  matcher returned no candidates (icon library empty?)")
        print()

    # Summary
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Gameplay frames:   {gameplay_passes}/{len(sample_ts)}")
    print(f"Overlay-clear:     {overlay_clears}/{len(sample_ts)}")
    if confirmed_candidates:
        accepted = ", ".join(
            f"{god}={count}" for god, count in confirmed_candidates.items()
        )
        print(f"Accepted matches:  {accepted}")
        most_common = max(
            confirmed_candidates.items(), key=lambda kv: kv[1],
        )
        if most_common[1] >= 3:
            print(
                f"Likely god:        {most_common[0]} "
                f"(would have been confirmed by 3-frame rule)"
            )
        else:
            print(
                f"Best candidate:    {most_common[0]} "
                f"({most_common[1]} samples — would NOT be confirmed; "
                f"need 3 in a row)"
            )
    else:
        print("Accepted matches:  none — god matcher confidence stayed low")
        print(
            "  → either the played god isn't in the icon library, or the "
            "portrait region\n    is misaligned, or the recording shows "
            "non-gameplay content throughout."
        )

    if gameplay_passes == 0:
        print()
        print(
            "NOTE: zero gameplay frames detected.  This recording may "
            "predate the HUD-position\n      change, or its KDA / HUD "
            "regions may not match the live constants.  Run\n      "
            "tools/check_kda_region.py against this video to verify."
        )

    print()
    print(f"Diagnostic crops saved to: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
