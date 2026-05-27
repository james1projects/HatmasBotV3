#!/usr/bin/env python3
"""
check_kda_region.py
===================
Pull one frame from a recording and draw the detector's region boxes
on top of it so you can visually confirm whether the KDA bar is
actually where the detector is looking.

Three boxes are drawn:
    red     — KDA crop (the tiny bar where digits are read)
    yellow  — HUD ability-bar check (used to detect gameplay screens)
    green   — wider gameplay check (portrait + health/mana)

If the red box doesn't sit cleanly over the "K / D / A" digits on your
recording — even by a few pixels — that explains the misreads.  The
fix is either:

    a) Make the Main scene's gameplay source pixel-perfect 1920x1080 at
       (0,0), so the recording matches what the live detector sees.
    b) Pass explicit coords on the CLI via --kda-region (not wired up
       yet — we'll add that once we know the offset).

Usage:
    python tools/check_kda_region.py "path/to/video.mp4"
    python tools/check_kda_region.py "path/to/video.mp4" 450.0
    python tools/check_kda_region.py "path/to/video.mp4" --timestamp 450

The PNG is written next to the input video as <name>_kda_check.png.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.kda_reader import (
    GAMEPLAY_CHECK_REGION,
    HUD_CHECK_REGION,
    KDA_REGION,
    OVERLAY_CHECK_REGION,
)
from core.god_matcher import PORTRAIT_REGION


def _probe_duration(video: Path, ffprobe: str) -> float | None:
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=15,
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


def _extract_frame(video: Path, t: float, ffmpeg: str) -> Image.Image | None:
    """Extract one frame at `t` and normalize to 1920x1080.

    The scale filter matches what vod_detector does at scan time, so
    the regions overlaid here line up with what the live offline
    matcher actually sees. Without it, a 4K recording would put the
    region boxes in the upper-left quarter of the frame.
    """
    try:
        result = subprocess.run(
            [
                ffmpeg, "-v", "error",
                "-ss", f"{max(0.0, t):.3f}",
                "-i", str(video),
                "-frames:v", "1",
                "-vf", "scale=1920:1080",
                "-f", "image2pipe",
                "-c:v", "png",
                "-",
            ],
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        print(f"error: could not run {ffmpeg!r}", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout:
        err = result.stderr.decode("utf-8", errors="ignore")[:300]
        print(f"ffmpeg failed: {err}", file=sys.stderr)
        return None
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Overlay detector regions on a frame of the recording."
    )
    parser.add_argument("video", type=Path)
    parser.add_argument(
        "timestamp",
        nargs="?",
        type=float,
        default=None,
        help="Seconds into the video (default: 30%% of duration).",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--crop-only",
        action="store_true",
        help="Also write <name>_kda_crop.png — just the KDA bar at 8x.",
    )
    parser.add_argument(
        "--region-offset",
        default="0,0",
        help=(
            "Shift ALL four regions by (dx,dy) pixels.  Positive dx shifts "
            "right, positive dy shifts down.  Useful when every region is "
            "off by the same amount (e.g. the entire game source is "
            "offset in the OBS scene).  When only KDA is wrong, prefer "
            "--kda-region instead."
        ),
    )
    parser.add_argument(
        "--kda-region",
        default=None,
        help=(
            "Override KDA_REGION absolutely as 'x1,y1,x2,y2'.  Leaves the "
            "other three regions (HUD / Gameplay / Overlay) untouched.  "
            "Use this to iterate on the KDA crop alone — e.g. "
            "'--kda-region 570,903,695,925'.  When set, --region-offset "
            "still shifts the OTHER regions but does not affect the KDA "
            "region."
        ),
    )
    args = parser.parse_args(argv)

    try:
        dx_str, dy_str = args.region_offset.split(",")
        dx, dy = int(dx_str), int(dy_str)
    except ValueError:
        print(
            f"error: --region-offset must be 'dx,dy' (got {args.region_offset!r})",
            file=sys.stderr,
        )
        return 2

    # Parse --kda-region override.  Validates 'x1,y1,x2,y2' with
    # x2>x1 and y2>y1.  When set, this region is used as-is (no
    # --region-offset applied) and the OTHER regions still get the
    # offset, so the user can mix-and-match.
    kda_region_override = None
    if args.kda_region is not None:
        try:
            parts = [int(s) for s in args.kda_region.split(",")]
            if len(parts) != 4:
                raise ValueError
            x1, y1, x2, y2 = parts
            if x2 <= x1 or y2 <= y1:
                raise ValueError
            kda_region_override = (x1, y1, x2, y2)
        except ValueError:
            print(
                f"error: --kda-region must be 'x1,y1,x2,y2' with x2>x1 "
                f"and y2>y1 (got {args.kda_region!r})",
                file=sys.stderr,
            )
            return 2

    if not args.video.exists():
        print(f"error: video not found: {args.video}", file=sys.stderr)
        return 2

    t = args.timestamp
    if t is None:
        duration = _probe_duration(args.video, args.ffprobe)
        if duration is None or duration <= 0:
            print("error: couldn't probe duration; pass a timestamp manually.",
                  file=sys.stderr)
            return 2
        # Pick a timestamp 30% in — usually well into a match.
        t = duration * 0.3
        print(f"Using t={t:.1f}s (30% of {duration:.1f}s duration)")

    img = _extract_frame(args.video, t, args.ffmpeg)
    if img is None:
        return 2

    print(f"Frame resolution: {img.size[0]}x{img.size[1]}")
    if img.size != (1920, 1080):
        print(
            f"WARNING: frame is not 1920x1080.  The detector's region "
            f"coords are tuned for 1920x1080 and will not match this "
            f"recording's layout."
        )

    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 24)
    except Exception:
        font = ImageFont.load_default()

    # The KDA region honors --kda-region override; the other three are
    # always shifted by --region-offset so we can validate that they
    # stayed correct when only KDA needs moving.
    kda_region_to_draw = kda_region_override if kda_region_override else KDA_REGION

    regions = [
        ("KDA crop (red)", kda_region_to_draw, (255, 60, 60), False),
        ("HUD check (yellow)", HUD_CHECK_REGION, (255, 220, 0), True),
        ("Gameplay check (green)", GAMEPLAY_CHECK_REGION, (60, 220, 60), True),
        ("Overlay check (blue)", OVERLAY_CHECK_REGION, (80, 140, 255), True),
        ("Portrait crop (magenta)", PORTRAIT_REGION, (255, 80, 220), True),
    ]
    if dx or dy:
        print(f"Applying region offset: dx={dx}, dy={dy} (KDA region NOT shifted when --kda-region is set)")
    if kda_region_override is not None:
        print(f"Using KDA region override: {kda_region_override}")

    # Track the KDA region we actually drew, for --crop-only below.
    if kda_region_override is not None:
        kda_region_shifted = kda_region_override
    else:
        kda_region_shifted = (
            KDA_REGION[0] + dx, KDA_REGION[1] + dy,
            KDA_REGION[2] + dx, KDA_REGION[3] + dy,
        )

    for label, region, color, apply_offset in regions:
        if apply_offset:
            x1 = region[0] + dx
            y1 = region[1] + dy
            x2 = region[2] + dx
            y2 = region[3] + dy
        else:
            x1, y1, x2, y2 = region
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        # Put the label just above the top edge (or below if it'd clip the top).
        label_y = y1 - 28 if y1 >= 30 else y2 + 4
        draw.text((x1 + 2, label_y), label, fill=color, font=font)

    out = args.video.with_name(f"{args.video.stem}_kda_check.png")
    img.save(out)
    print(f"Saved: {out}")

    if args.crop_only:
        # Re-extract a clean frame so the crop isn't covered in the diagnostic
        # rectangles we just drew onto `img`.
        clean = _extract_frame(args.video, t, args.ffmpeg)
        if clean is None:
            return 2
        crop = clean.crop(kda_region_shifted)
        w, h = crop.size
        crop_8x = crop.resize((w * 8, h * 8), Image.NEAREST)
        crop_out = args.video.with_name(f"{args.video.stem}_kda_crop.png")
        crop_8x.save(crop_out)
        print(f"Saved: {crop_out}")

    print()
    print("Open the *_kda_check.png and look at the RED box.  It should sit")
    print("cleanly over the 'K / D / A' digits on the bottom-left HUD.  If")
    print("it's shifted up/down/left/right, we know exactly how much the")
    print("Main scene is offsetting the gameplay source vs. the live scene.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
