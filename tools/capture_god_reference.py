#!/usr/bin/env python3
"""
capture_god_reference.py
========================
Pull a clean god-portrait crop out of a recording and save it as a
custom-overlay reference icon, so the matcher can recognise that god
under whatever decode pipeline the recording was made with (CUDA,
software, etc.).

Why this exists
---------------
The matcher compares the in-game portrait region against a library of
fingerprints (HSV histograms).  When the orchestrator runs with
``--hwaccel cuda``, NVDEC produces subtly different pixel values than
software decode — invisible to the eye, but enough to drop histogram
correlation by a few hundredths.  For most gods that's fine; for
borderline ones (Vulcan, etc.) it pushes the score below the
acceptance threshold and the recording lands in ``recordings/unknown/``.

Capturing a reference frame **from the same decode pipeline** gives the
matcher a fingerprint that correlates near-1.0 with future scans of
that god, no matter how many decoder quirks are in the chain.  One
clean reference per borderline god typically fixes it permanently.

Usage
-----
    # Auto-detect the god from the recording:
    python tools/capture_god_reference.py "recordings/Vulcan/Vulcan-1.mp4"

    # Force a specific god name (skip auto-detect):
    python tools/capture_god_reference.py "recording.mp4" --god Vulcan

    # Match the orchestrator's CUDA pipeline so the captured frame
    # comes from the same decoder that does the real scans:
    python tools/capture_god_reference.py "recording.mp4" --hwaccel cuda

The chosen frame's portrait region (default ``PORTRAIT_REGION =
(635, 942, 715, 1022)``, 80x80 px) is saved as
``Custom God Icons/<God Name>.png`` next to your other custom overlays.
After running, re-run ``process_recordings.py`` (with CUDA on) and the
god should now be identified at high confidence.
"""

from __future__ import annotations

import argparse
import io
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.god_matcher import GodMatcher, PORTRAIT_REGION


DEFAULT_DATA_DIR = _REPO_ROOT / "data"
# Reference captures live in their own folder so they never mix with
# the user's decorative ``Custom God Icons/`` art.  The matcher loads
# from this folder on every scan via VodDetectorOptions.
DEFAULT_REFERENCE_ICONS_DIR = _REPO_ROOT / "Portrait_Source"


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
        result = subprocess.run(cmd, capture_output=True, timeout=30)
    except FileNotFoundError:
        print(f"error: could not run {ffmpeg!r}", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Capture a clean god-portrait reference icon from a "
            "recording, saved as Custom God Icons/<God>.png so the "
            "matcher can recognise that god under the same decode "
            "pipeline used by the recording."
        ),
    )
    parser.add_argument("video", type=Path)
    parser.add_argument(
        "--god",
        default=None,
        help=(
            "God name to use for the saved filename.  When omitted, "
            "the matcher's auto-detection picks the majority candidate "
            "across sampled frames.  Required if auto-detection can't "
            "agree on a single god (e.g. recording with multiple gods)."
        ),
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=10,
        help="Number of frames to sample for picking the best one (default: 10).",
    )
    parser.add_argument(
        "--start-pct",
        type=float,
        default=0.15,
        help=(
            "First sample at this fraction of duration.  Default 0.15 "
            "skips lobby / loading screens."
        ),
    )
    parser.add_argument(
        "--end-pct",
        type=float,
        default=0.85,
        help=(
            "Last sample at this fraction of duration.  Default 0.85 "
            "skips post-match cinematics."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_REFERENCE_ICONS_DIR,
        help=(
            "Where to save the captured icon.  Default: "
            f"{DEFAULT_REFERENCE_ICONS_DIR}.  This folder is "
            f"DISTINCT from 'Custom God Icons/' so reference captures "
            f"used by the matcher don't mix with the user's decorative "
            f"OBS overlay art."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing Custom God Icons/<God>.png.",
    )
    parser.add_argument(
        "--hwaccel",
        default=None,
        help=(
            "ffmpeg -hwaccel value (e.g. 'cuda').  Match what your "
            "orchestrator uses so the captured frame comes from the "
            "same decoder that does real scans.  Default: software."
        ),
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args(argv)

    if not args.video.exists():
        print(f"error: video not found: {args.video}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.WARNING)

    duration = _probe_duration(args.video, args.ffprobe)
    if duration is None or duration <= 0:
        print("error: could not probe video duration", file=sys.stderr)
        return 2

    # Use the existing library to auto-detect the god, but DO NOT
    # include the overlay library — we don't want a previous bad
    # capture to confirm itself.  This identification step needs to
    # rely on the (recording-pipeline-agnostic) base CDN art only.
    matcher = GodMatcher(
        icons_dir=args.data_dir / "god_icons",
        overlay_icons_dir=None,
    )
    if not matcher.load_icons():
        print(
            f"error: god matcher loaded zero icons from "
            f"{args.data_dir / 'god_icons'}",
            file=sys.stderr,
        )
        return 2

    # Build sample timestamps.
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

    print(f"Video:       {args.video}")
    print(f"Duration:    {duration:.1f}s")
    decoder = f"ffmpeg --hwaccel {args.hwaccel}" if args.hwaccel else "software"
    print(f"Decoder:     {decoder}")
    print(f"Sampling {args.samples} frame(s)...\n")

    # Per-frame: extract, identify, track best score per god.
    # Each entry: (god_name, score, frame_image, timestamp)
    samples: list[tuple[str, float, Image.Image, float]] = []
    for ts in sample_ts:
        img = _extract_frame(
            args.video, ts, args.ffmpeg, hwaccel=args.hwaccel,
        )
        if img is None:
            print(f"  t={ts:6.1f}s — could not extract frame")
            continue
        name, conf = matcher.identify(img)
        if name is None:
            top = matcher.identify_top_n(img, n=1)
            best = (
                f"best={top[0][0]} ({top[0][1]:.3f})" if top else "no candidate"
            )
            print(f"  t={ts:6.1f}s — no match ({best})")
            continue
        print(f"  t={ts:6.1f}s — {name:25s} conf={conf:.3f}")
        samples.append((name, conf, img, ts))

    if not samples:
        print(
            "\nerror: no sampled frame produced an accepted match.  "
            "Either:\n"
            "  * the recording shows non-gameplay throughout (try a longer "
            "or more central time window)\n"
            "  * the played god isn't in the in-game CDN library — pass "
            "--god <Name> explicitly\n"
            "  * the portrait region is misaligned for this recording — "
            "run check_kda_region.py to verify",
            file=sys.stderr,
        )
        return 2

    # Pick the target god name.  --god overrides; otherwise majority
    # vote across accepted samples.
    if args.god is not None:
        target_god = args.god.strip()
        candidates = [s for s in samples if s[0].lower() == target_god.lower()]
        if not candidates:
            # User insisted on a name the matcher never proposed —
            # use the highest-confidence sample regardless.  Common
            # case: capturing a borderline god whose existing CDN art
            # *is* the problem; the matcher won't match it, but the
            # frame is still a clean portrait.
            print(
                f"\nNote: no sampled frame matched '{target_god}' via the "
                f"existing library.  Using the highest-confidence sample "
                f"anyway since you've named the god explicitly."
            )
            candidates = samples
            # Re-key all to target_god so the rest of the flow is uniform.
            candidates = [(target_god, c[1], c[2], c[3]) for c in candidates]
    else:
        # Majority vote.
        counts: dict[str, int] = {}
        for s in samples:
            counts[s[0]] = counts.get(s[0], 0) + 1
        target_god, top_count = max(counts.items(), key=lambda kv: kv[1])
        if top_count < len(samples) / 2:
            print(
                f"\nwarning: god identification is split across multiple "
                f"candidates ({counts}).  Using majority winner "
                f"'{target_god}' but you may want to re-run with --god to "
                f"force a specific name.",
                file=sys.stderr,
            )
        candidates = [s for s in samples if s[0] == target_god]

    # Pick the candidate with the highest matcher confidence.  That
    # frame is most likely to be a clean, unobstructed portrait — no
    # ult animation, no death cam tinting, no overlay flash.
    best = max(candidates, key=lambda c: c[1])
    best_god, best_conf, best_img, best_ts = best

    # Crop the portrait region.
    portrait = best_img.crop(PORTRAIT_REGION)

    # Save.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output_dir / f"{best_god}.png"
    if out_path.exists() and not args.overwrite:
        print(
            f"\nerror: {out_path} already exists.  Pass --overwrite to "
            f"replace it.",
            file=sys.stderr,
        )
        return 2

    portrait.save(str(out_path))

    # Also save the source frame so the user can audit what got
    # captured without having to re-extract.  Audit folder lives next
    # to the saved icon, prefixed with an underscore so it sorts
    # together and is obviously not an icon itself.
    audit_dir = out_path.parent / "_capture_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_stem = f"{best_god}_t{best_ts:.0f}_conf{best_conf:.3f}"
    best_img.save(str(audit_dir / f"{audit_stem}_frame.png"))

    print()
    print(f"Captured at:    t={best_ts:.1f}s, confidence={best_conf:.3f}")
    print(f"Saved icon:     {out_path}")
    print(f"Audit frame:    {audit_dir / (audit_stem + '_frame.png')}")
    print()
    print(
        "Re-run process_recordings.py (with CUDA on if you use it) and "
        f"this god should now identify cleanly at near-1.0 confidence."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
