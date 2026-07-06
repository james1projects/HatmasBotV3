#!/usr/bin/env python3
"""
make_blur_bg.py
===============
Grab a frame from a gameplay recording and turn it into a blurred,
darkened 9:16 background image for the TikTok vertical template
(resolve_tiktok.py drops it on V1 behind the gameplay/items layers).

Runs under the FindIt venv (needs cv2 + Pillow); resolve_tiktok.py
invokes it as a subprocess.

Usage:
    python tools/make_blur_bg.py <video> <out.png>
        [--width 1080] [--height 1920] [--blur 45] [--brightness 0.65]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
from PIL import Image, ImageEnhance, ImageFilter


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--blur", type=float, default=45.0)
    ap.add_argument("--brightness", type=float, default=0.65)
    args = ap.parse_args()

    cap = cv2.VideoCapture(str(args.video))
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if frames > 1:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frames // 2)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        print(f"ERROR: could not read a frame from {args.video}",
              file=sys.stderr)
        sys.exit(1)

    im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    # center-crop to the target aspect, then scale to cover
    src_w, src_h = im.size
    want = args.width / args.height
    crop_w = min(src_w, int(src_h * want))
    crop_h = min(src_h, int(crop_w / want))
    x0 = (src_w - crop_w) // 2
    y0 = (src_h - crop_h) // 2
    im = im.crop((x0, y0, x0 + crop_w, y0 + crop_h))
    im = im.resize((args.width, args.height), Image.LANCZOS)
    im = im.filter(ImageFilter.GaussianBlur(args.blur))
    im = ImageEnhance.Brightness(im).enhance(args.brightness)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    im.save(args.out)
    print(f"background written: {args.out}")


if __name__ == "__main__":
    main()
