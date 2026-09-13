"""
vod_calibrate.py — build a detector profile for another streamer's HUD
=======================================================================

The offline kill/death detector reads fixed pixel boxes of a 1920x1080
frame (K/D/A bar, god portrait, HUD/gameplay/overlay checks). Those boxes
are James's; another channel's VOD needs its own set, stored as a
DetectorProfile JSON (core/detector_profile.py) and passed to
process_recordings.py --profile. This tool is the manual calibration loop:

  1. frames   dump gridded 1080p sample frames so the boxes can be read
              off the picture:
                python tools\\vod_calibrate.py frames "D:\\...\\v123.mp4" [--count 6] [--grid 100]
  2. write    create / update the profile with the boxes (x1,y1,x2,y2):
                python tools\\vod_calibrate.py write data\\vod\\channels\\foo\\profile.json ^
                    --channel foo --kda 1490,44,1580,66 --portrait 1620,30,1700,110 ^
                    --hud-check 600,1000,780,1080 --gameplay-check 600,900,760,1080 ^
                    --overlay-check 0,100,1152,900 [--no-portrait] [--group-mode gaps]
  3. preview  draw the profile's boxes on frames, dump the 8x KDA crop
              and run the real reader/matcher on them:
                python tools\\vod_calibrate.py preview "D:\\...\\v123.mp4" --profile ... [--timestamp T | --count N]

Iterate 2-3 until preview reads the K/D/A you can see. Boxes are in
1920x1080 coordinates whatever the source resolution (frames are scaled
exactly as the scan pipeline scales them). Output PNGs land next to the
video by default (--out to change).
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image, ImageDraw

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.detector_profile import (GROUP_MODES, REGION_KEYS, DetectorProfile,  # noqa: E402
                                   ProfileError)
from tools.check_kda_region import _extract_frame, _probe_duration  # noqa: E402

COLORS = {
    "kda": (255, 40, 40),
    "portrait": (255, 0, 255),
    "hud_check": (255, 220, 0),
    "gameplay_check": (40, 220, 40),
    "overlay_check": (60, 140, 255),
}


# ── shared ────────────────────────────────────────────────────────────

def _sample_times(duration: float, count: int, lo: float = 0.2, hi: float = 0.8) -> List[float]:
    if count <= 1:
        return [duration * (lo + hi) / 2]
    return [duration * (lo + (hi - lo) * i / (count - 1)) for i in range(count)]


def _out_dir(video: Path, out: Optional[Path]) -> Path:
    d = out or video.parent / f"{video.stem}_calibrate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _frames(video: Path, a) -> List[tuple[float, Image.Image]]:
    if getattr(a, "timestamp", None) is not None:
        times = [float(a.timestamp)]
    else:
        dur = _probe_duration(video, a.ffprobe)
        if not dur:
            raise SystemExit(f"could not probe {video}")
        times = _sample_times(dur, a.count)
    out = []
    for t in times:
        img = _extract_frame(video, t, a.ffmpeg)
        if img is None:
            print(f"  t={t:.0f}s: frame extraction failed")
            continue
        out.append((t, img))
    return out


def _parse_box(s: str) -> tuple:
    parts = [int(p) for p in s.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("box must be x1,y1,x2,y2")
    return tuple(parts)


# ── frames ────────────────────────────────────────────────────────────

def cmd_frames(a) -> int:
    video = Path(a.video)
    out = _out_dir(video, a.out)
    frames = _frames(video, a)
    for t, img in frames:
        canvas = img.copy()
        draw = ImageDraw.Draw(canvas)
        g = max(20, int(a.grid))
        for x in range(0, 1920, g):
            major = (x % (g * 2) == 0)
            draw.line([(x, 0), (x, 1080)], fill=(255, 255, 255) if major else (150, 150, 150), width=1)
            if major:
                draw.text((x + 2, 2), str(x), fill=(255, 255, 0))
                draw.text((x + 2, 1066), str(x), fill=(255, 255, 0))
        for y in range(0, 1080, g):
            major = (y % (g * 2) == 0)
            draw.line([(0, y), (1920, y)], fill=(255, 255, 255) if major else (150, 150, 150), width=1)
            if major:
                draw.text((2, y + 2), str(y), fill=(255, 255, 0))
                draw.text((1890, y + 2), str(y), fill=(255, 255, 0))
        path = out / f"{video.stem}_t{int(t):05d}_grid.png"
        canvas.save(path)
        img.save(out / f"{video.stem}_t{int(t):05d}.png")
        print(f"  {path}")
    print(f"\n{len(frames)} frame(s) in {out}. Read the K/D/A bar and portrait boxes off the grid "
          f"(labels every {2 * max(20, int(a.grid))} px), then `write` them into the profile.")
    return 0


# ── write ─────────────────────────────────────────────────────────────

def cmd_write(a) -> int:
    path = Path(a.profile)
    try:
        prof = DetectorProfile.load(path) if path.exists() else DetectorProfile.from_dict({}, name=path.parent.name)
    except ProfileError as e:
        print(f"error: existing profile is invalid: {e}")
        return 2
    regions = dict(prof.regions)
    for key in REGION_KEYS:
        val = getattr(a, key)
        if val is not None:
            regions[key] = tuple(val)
    changes = dict(regions=regions)
    if a.channel:
        changes["name"] = a.channel
    if a.group_mode:
        changes["group_mode"] = a.group_mode
    if a.no_portrait:
        changes["portrait_enabled"] = False
    if a.portrait_on:
        changes["portrait_enabled"] = True
    if a.notes is not None:
        changes["notes"] = a.notes
    if a.templates is not None:
        changes["digit_templates_dir"] = Path(a.templates) if a.templates else None
    new = dataclasses.replace(prof, **changes)
    try:
        new = DetectorProfile.from_dict(new.to_dict(), name=new.name, source_path=path)   # validate
    except ProfileError as e:
        print(f"error: {e}")
        return 2
    new.save(path)
    print(f"wrote {path}")
    for k in REGION_KEYS:
        print(f"  {k:<15}{new.regions[k]}")
    x, y, w, h = new.crop_box()
    print(f"  group_mode={new.group_mode} portrait={'on' if new.portrait_enabled else 'off'} "
          f"crop={w}x{h}+{x}+{y}")
    return 0


# ── preview ───────────────────────────────────────────────────────────

def cmd_preview(a) -> int:
    video = Path(a.video)
    try:
        prof = DetectorProfile.load(a.profile)
    except ProfileError as e:
        print(f"error: {e}")
        return 2
    out = _out_dir(video, a.out)
    from core.kda_reader import KdaReader
    reader = KdaReader(regions=prof.regions, group_mode=prof.group_mode,
                       kda_field_windows=prof.kda_field_windows,
                       template_dir=prof.digit_templates_dir)
    if not reader.is_ready:
        print("warning: reader has no digit templates and no Tesseract")
    matcher = None
    if prof.portrait_enabled:
        from core.god_matcher import GodMatcher
        matcher = GodMatcher(portrait_region=prof.regions["portrait"])
        if not matcher.load_icons():
            print("warning: god icon library empty (run download_god_icons.py)")
            matcher = None
    print(f"profile {prof.name}: {prof.source_path}")
    ok = 0
    frames = _frames(video, a)
    for t, img in frames:
        canvas = img.copy()
        draw = ImageDraw.Draw(canvas)
        for key in REGION_KEYS:
            if key == "portrait" and not prof.portrait_enabled:
                continue
            x1, y1, x2, y2 = prof.regions[key]
            draw.rectangle([x1, y1, x2, y2], outline=COLORS[key], width=2)
            draw.text((x1 + 3, max(0, y1 - 12)), key, fill=COLORS[key])
        canvas.save(out / f"{video.stem}_t{int(t):05d}_boxes.png")
        x1, y1, x2, y2 = prof.regions["kda"]
        crop = img.crop((x1, y1, x2, y2))
        crop.resize((crop.width * 8, crop.height * 8), Image.NEAREST).save(
            out / f"{video.stem}_t{int(t):05d}_kda8x.png")

        arr = np.array(img)
        scene = reader.scene_classification_with_details(arr)
        th = scene["thresholds"]
        kda = reader.read_kda(img)
        det = reader.read_kda_with_details(img)
        print(f"\n== t={t:.0f}s  gameplay={'PASS' if scene['is_gameplay'] else 'FAIL'} "
              f"(hud std {scene['hud_std']:.0f}/{th['hud_min_std']} mean {scene['hud_mean']:.0f}/{th['hud_min_mean']}, "
              f"gameplay std {scene['portrait_std']:.0f}/{th['portrait_min_std']})  "
              f"overlay={'OPEN' if scene['overlay_open'] else 'no'} "
              f"(dark {scene['overlay_dark_ratio']:.2f}/{th['overlay_dark_threshold']}, bar std {scene['overlay_bar_std']:.0f})")
        print(f"   read_kda ({prof.group_mode}): {kda}    details (gaps): {det.get('kda')}  "
              f"{('reason=' + det['failure_reason']) if det.get('failure_reason') else ''}")
        for gi, g in enumerate(det.get("groups") or []):
            digits = ", ".join(
                f"{d.get('best', '?')}[{d.get('method', '-')[:4]} d={d.get('distance', '?')} m={d.get('margin', '?')}]"
                for d in g.get("digits", []))
            print(f"     group {gi}: {g.get('concatenated', '?')}  {digits}")
        if det.get("binary_8x") is not None:
            det["binary_8x"].save(out / f"{video.stem}_t{int(t):05d}_kda_binary.png")
        if kda is not None:
            ok += 1
        if matcher is not None:
            name, score = matcher.identify(img)
            top = matcher.identify_top_n(img, 3) if hasattr(matcher, "identify_top_n") else []
            print(f"   portrait: {name} ({score:.2f})  top3={[(n, round(s, 2)) for n, s in top]}")
    print(f"\n{ok}/{len(frames)} frames produced a K/D/A read. Images in {out}")
    return 0 if ok else 1


# ── CLI ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Calibrate a detector profile for another channel's HUD.")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("frames", help="dump gridded 1080p frames to read box coordinates from")
    f.add_argument("video", type=Path)
    f.add_argument("--count", type=int, default=6)
    f.add_argument("--timestamp", type=float, default=None, help="one frame at this second instead of --count samples")
    f.add_argument("--grid", type=int, default=100, help="minor grid spacing in px (labels every 2x)")
    f.add_argument("--out", type=Path, default=None)
    f.add_argument("--ffmpeg", default="ffmpeg")
    f.add_argument("--ffprobe", default="ffprobe")
    f.set_defaults(fn=cmd_frames)

    w = sub.add_parser("write", help="create/update a profile JSON with region boxes")
    w.add_argument("profile", type=Path)
    for key in REGION_KEYS:
        w.add_argument(f"--{key.replace('_', '-')}", dest=key, type=_parse_box, default=None,
                       help=f"{key} box as x1,y1,x2,y2 (1080p coords)")
    w.add_argument("--channel", default=None)
    w.add_argument("--group-mode", choices=GROUP_MODES, default=None)
    w.add_argument("--no-portrait", action="store_true", help="a facecam/overlay covers the portrait")
    w.add_argument("--portrait-on", action="store_true")
    w.add_argument("--notes", default=None)
    w.add_argument("--templates", default=None, help="per-profile digit template dir ('' to clear)")
    w.set_defaults(fn=cmd_write)

    v = sub.add_parser("preview", help="draw the profile's boxes and run the reader on sample frames")
    v.add_argument("video", type=Path)
    v.add_argument("--profile", type=Path, required=True)
    v.add_argument("--count", type=int, default=4)
    v.add_argument("--timestamp", type=float, default=None)
    v.add_argument("--out", type=Path, default=None)
    v.add_argument("--ffmpeg", default="ffmpeg")
    v.add_argument("--ffprobe", default="ffprobe")
    v.set_defaults(fn=cmd_preview)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
