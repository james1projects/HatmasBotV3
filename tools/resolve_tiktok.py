#!/usr/bin/env python3
"""
resolve_tiktok.py
=================
One-button "gameplay recording -> vertical TikTok project" for DaVinci
Resolve, plus a TuneFrame-style preset capture.  Successor to the Vegas
vertical_tiktok pipeline (vegas_scripts/TuneFrame.cs + ProcessVideo.cs).

Layout (from tools/tiktok_preset.json — TikTok safe-zone version):
  V4  "Items Overlay"   the Smite item bar cropped out and blown up into
                        a full-width band below TikTok's top nav overlay.
  V3  "Gameplay"        wide 9:16-friendly crop (full source height)
                        ending above the username/description zone.
  V2  "Blur BG"         blurred, darkened still of the recording's middle
                        frame filling the whole canvas behind everything
                        (made by tools/make_blur_bg.py via the FindIt
                        venv's cv2+Pillow — Resolve's API cannot script
                        live blur effects; Fusion comp edits via API are
                        ignored by the renderer, verified 2026-07-03).
  V1  "Source"          the original clip, disabled — it exists because
                        CreateTimelineFromClips is the only API path that
                        fans OBS multi-track audio out to A1..A4.

Build mode (default):
  1. Pops a file picker over the recordings folder (skipped when a video
     path is passed on the command line).
  2. Renders the blurred background PNG next to the recording (in a
     .tiktok_bg subfolder).
  3. Connects to Resolve (launching it if needed), creates a project
     named "<recording> TikTok" from resolve_gameplay_template.drp (the
     template carries the playback frame rate, which the API cannot set),
     and forces the timeline to 1080x1920.
  4. Builds the three video layers + audio on an empty timeline and
     applies the preset's Zoom / Position / Crop to each — the same
     properties you see in the Inspector.
  5. Names the audio tracks, disables the ones the preset says to
     (Discord / Misc), switches to the Edit page and saves.

Capture mode (--save), the TuneFrame equivalent:
  Fine-tune the transforms in the Inspector on the currently open
  timeline, then run with --save to read Zoom / Position / Crop back off
  the video layers and rewrite tools/tiktok_preset.json.  Every future
  build uses the tuned values.  Crops are stored normalized, so one
  preset serves both 1080p and 1440p recordings.

Usage:
    python tools/resolve_tiktok.py                # file picker
    python tools/resolve_tiktok.py <video>        # skip the picker
    python tools/resolve_tiktok.py --save         # capture current tune
    python tools/resolve_tiktok.py --preset p.json <video>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))
from resolve_import import (  # noqa: E402
    AUDIO_TRACK_NAMES,
    PROJECT_TEMPLATE,
    RECORDINGS_DIR,
    VIDEO_EXTENSIONS,
    connect_resolve,
    fail,
    unique_project_name,
)

DEFAULT_PRESET = Path(__file__).with_name("tiktok_preset.json")

# The timeline is created FROM the clip (the only API path that fans OBS
# multi-track audio out to A1..A4; appending audio to an empty timeline
# lands everything as one item on A1).  That original full-frame video
# item stays on V1 disabled, and the preset's layers stack above it.
TRACK_OFFSET = 1
BLUR_HELPER = Path(__file__).with_name("make_blur_bg.py")
# make_blur_bg.py needs cv2 + Pillow; the FindIt venv has both.
FINDIT_PY = Path(r"C:\Projects\HatmasBot\.venv-findit\Scripts\python.exe")

# TimelineItem property keys applied per layer (Inspector: Transform + Cropping).
ZOOM_KEYS = ("ZoomX", "ZoomY")
CROP_KEYS = {"left": "CropLeft", "right": "CropRight",
             "top": "CropTop", "bottom": "CropBottom"}


def pick_video() -> Path:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    initial = RECORDINGS_DIR if RECORDINGS_DIR.is_dir() else Path.home()
    name = filedialog.askopenfilename(
        parent=root,
        title="Build TikTok vertical from recording",
        initialdir=str(initial),
        filetypes=VIDEO_EXTENSIONS,
    )
    root.destroy()
    if not name:
        print("No file selected - nothing to do.")
        sys.exit(0)
    return Path(name)


def load_preset(path: Path) -> dict:
    if not path.is_file():
        fail(f"preset not found: {path}")
    preset = json.loads(path.read_text(encoding="utf-8"))
    if not preset.get("layers"):
        fail(f"preset has no layers: {path}")
    return preset


def make_blur_bg(video: Path, layer: dict, frame: dict) -> Path | None:
    """Render the blurred background PNG; None if the helper can't run."""
    out = video.parent / ".tiktok_bg" / f"{video.stem}.png"
    if not FINDIT_PY.is_file():
        print(f"WARNING: {FINDIT_PY} missing - building without the "
              f"blurred background layer.")
        return None
    cmd = [str(FINDIT_PY), str(BLUR_HELPER), str(video), str(out),
           "--width", str(frame.get("width", 1080)),
           "--height", str(frame.get("height", 1920)),
           "--blur", str(layer.get("blur_radius", 45)),
           "--brightness", str(layer.get("brightness", 0.65))]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not out.is_file():
        print(f"WARNING: blur background failed - building without it.\n"
              f"{res.stderr.strip()}")
        return None
    return out


def conformed_size(item, frame_w: int, frame_h: int) -> tuple[float, float]:
    """Size of the item's media after Resolve fits it into the timeline.

    Resolve's Inspector crop values are in pixels of this conformed image
    (pre-zoom), NOT source pixels — measured empirically 2026-07-03.  For a
    16:9 source in a 1080x1920 timeline this is always 1080x607.5, which is
    what makes the preset resolution-independent.
    """
    mpi = item.GetMediaPoolItem()
    resolution = (mpi.GetClipProperty("Resolution") or "") if mpi else ""
    if "x" not in resolution:
        fail("cannot read source resolution off the timeline item")
    src_w, src_h = (int(v) for v in resolution.split("x"))
    k = min(frame_w / src_w, frame_h / src_h)
    return src_w * k, src_h * k


def apply_layer(item, layer: dict, frame_w: int, frame_h: int) -> None:
    conf_w, conf_h = conformed_size(item, frame_w, frame_h)
    props = {k: float(layer["zoom"]) for k in ZOOM_KEYS}
    props["Pan"] = float(layer["pan"])
    props["Tilt"] = float(layer["tilt"])
    crop = layer.get("crop_norm") or {}
    for side, key in CROP_KEYS.items():
        frac = float(crop.get(side, 0.0))
        span = conf_w if side in ("left", "right") else conf_h
        props[key] = max(0.0, frac * span)
    for key, value in props.items():
        if not item.SetProperty(key, value):
            print(f"WARNING: SetProperty({key}, {value:.2f}) failed on "
                  f"{layer['name']!r}")


def read_layer(item, layer: dict, frame_w: int, frame_h: int) -> dict:
    """Inverse of apply_layer: capture the item's transform into the preset."""
    conf_w, conf_h = conformed_size(item, frame_w, frame_h)
    captured = dict(layer)
    captured["zoom"] = round(float(item.GetProperty("ZoomX")), 5)
    captured["pan"] = round(float(item.GetProperty("Pan")), 2)
    captured["tilt"] = round(float(item.GetProperty("Tilt")), 2)
    captured["crop_norm"] = {
        side: round(float(item.GetProperty(key))
                    / (conf_w if side in ("left", "right") else conf_h), 7)
        for side, key in CROP_KEYS.items()
    }
    return captured


# --- build mode ---------------------------------------------------------------

def append_at(media_pool, timeline, clip_info: dict, track: int,
              record_frame: int | None):
    """AppendToTimeline pinned to a position, with placement verification.

    Without recordFrame a bare append lands at the timeline END, leaving
    layers back to back instead of stacked (bit us 2026-07-03).
    """
    info = dict(clip_info)
    info["trackIndex"] = track
    if record_frame is not None:
        info["recordFrame"] = record_frame
    added = media_pool.AppendToTimeline([info])
    if not added:
        fail(f"could not append clip to V{track}")
    item = added[0]
    if record_frame is not None and item.GetStart() != record_frame:
        fail(f"clip on V{track} landed at frame {item.GetStart()}, wanted "
             f"{record_frame} - Resolve API changed?")
    return item


def build(video: Path, preset: dict) -> None:
    print(f"Video: {video}")
    frame = preset.get("frame") or {}
    fw, fh = int(frame.get("width", 1080)), int(frame.get("height", 1920))

    layers = sorted(preset["layers"], key=lambda l: l["track"])
    bg_png = None
    for layer in layers:
        if layer.get("media") == "blur_still":
            bg_png = make_blur_bg(video, layer, frame)
            if bg_png:
                print(f"Background: {bg_png}")

    resolve = connect_resolve()
    pm = resolve.GetProjectManager()
    if pm.GetCurrentProject():
        pm.SaveProject()  # don't lose open work before switching projects

    name = unique_project_name(pm, f"{video.stem} TikTok")
    if not PROJECT_TEMPLATE.is_file():
        fail(f"project template missing: {PROJECT_TEMPLATE}")
    # Template instead of CreateProject: it bakes in the playback frame
    # rate, which SetSetting cannot touch (see resolve_import.py).
    if not pm.ImportProject(str(PROJECT_TEMPLATE), name):
        fail(f"could not import project template {PROJECT_TEMPLATE}")
    project = pm.LoadProject(name)
    if not project:
        fail(f"imported {name!r} but could not load it")
    print(f"Project: {name}")

    project.SetSetting("timelineResolutionWidth", str(fw))
    project.SetSetting("timelineResolutionHeight", str(fh))

    media_pool = project.GetMediaPool()
    clips = media_pool.ImportMedia([str(video)])
    if not clips:
        fail("Resolve could not import the video")
    clip = clips[0]
    bg_clip = None
    if bg_png:
        bg_clips = media_pool.ImportMedia([str(bg_png)])
        if bg_clips:
            bg_clip = bg_clips[0]
        else:
            print("WARNING: could not import the background PNG - skipping it.")

    fps = clip.GetClipProperty("FPS")
    if fps and str(fps) not in (str(frame.get("fps", 60)), "60.0"):
        print(f"WARNING: clip is {fps}fps; template playback rate is 60 - "
              f"fix Project Settings > Master Settings if playback looks off.")
    video_frames = int(float(clip.GetClipProperty("Frames") or 0))

    timeline = media_pool.CreateTimelineFromClips(f"{video.stem} vertical",
                                                  [clip])
    if not timeline:
        fail("could not create timeline")
    project.SetCurrentTimeline(timeline)

    # the original full-frame clip stays on V1, muted - it only exists to
    # carry the fanned-out A1..A4 audio
    v1_items = timeline.GetItemListInTrack("video", 1)
    if not v1_items:
        fail("timeline has no clip on V1")
    record_frame = v1_items[0].GetStart()
    if not v1_items[0].SetClipEnabled(False):
        print("WARNING: could not disable the V1 source clip - mute the "
              "track eye icon on V1 by hand.")
    timeline.SetTrackName("video", 1, "Source (audio only)")

    for layer in layers:
        while timeline.GetTrackCount("video") < layer["track"] + TRACK_OFFSET:
            if not timeline.AddTrack("video"):
                fail("could not add a video track")

    for layer in layers:
        track = layer["track"] + TRACK_OFFSET
        if layer.get("media") == "blur_still":
            if not bg_clip:
                continue
            # Stills always come in at the user-preference duration (the
            # endFrame hint and the Duration clip property are both
            # ignored/read-only via API), so tile copies across the whole
            # clip and lock the track afterwards.
            item = append_at(media_pool, timeline,
                             {"mediaPoolItem": bg_clip,
                              "startFrame": 0,
                              "endFrame": max(video_frames - 1, 0),
                              "mediaType": 1},
                             track, record_frame)
            covered = item.GetDuration()
            while covered < video_frames:
                extra = append_at(media_pool, timeline,
                                  {"mediaPoolItem": bg_clip,
                                   "startFrame": 0,
                                   "endFrame": video_frames - covered - 1,
                                   "mediaType": 1},
                                  track, record_frame + covered)
                got = extra.GetDuration()
                if got <= 0:
                    print("WARNING: could not extend the background to the "
                          "full clip length - trim by hand if it runs short.")
                    break
                covered += got
        else:
            append_at(media_pool, timeline,
                      {"mediaPoolItem": clip, "mediaType": 1},
                      track, record_frame)

    for layer in layers:
        track = layer["track"] + TRACK_OFFSET
        timeline.SetTrackName("video", track, layer["name"])
        if layer.get("media") == "blur_still":
            # the background is tiled stills - lock them against stray edits
            timeline.SetTrackLock("video", track, True)
            continue
        items = timeline.GetItemListInTrack("video", track)
        if not items:
            fail(f"no clip found on V{track} for {layer['name']!r}")
        apply_layer(items[0], layer, fw, fh)
        print(f"V{track} {layer['name']}: zoom={layer['zoom']} "
              f"pan={layer['pan']} tilt={layer['tilt']}")

    audio = preset.get("audio") or {}
    names = audio.get("names", AUDIO_TRACK_NAMES)
    enabled = audio.get("enabled", [True] * len(names))
    audio_tracks = timeline.GetTrackCount("audio")
    if audio_tracks < len(names):
        print(f"WARNING: expected {len(names)} audio tracks, got "
              f"{audio_tracks} - was this recorded with all OBS tracks on?")
    for i in range(1, audio_tracks + 1):
        if i <= len(names):
            timeline.SetTrackName("audio", i, names[i - 1])
        if i <= len(enabled) and not enabled[i - 1]:
            if not timeline.SetTrackEnable("audio", i, False):
                print(f"WARNING: could not disable audio track {i}")

    resolve.OpenPage("edit")
    pm.SaveProject()
    print("Done - vertical project is open on the Edit page.")


# --- capture mode (--save) ----------------------------------------------------

def save(preset_path: Path, preset: dict) -> None:
    resolve = connect_resolve()
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    if not timeline:
        fail("no timeline open - open your tuned vertical project first")
    print(f"Capturing from: {project.GetName()} / {timeline.GetName()}")
    fw = int(project.GetSetting("timelineResolutionWidth") or 1080)
    fh = int(project.GetSetting("timelineResolutionHeight") or 1920)

    captured = []
    for layer in sorted(preset["layers"], key=lambda l: l["track"]):
        if layer.get("media") == "blur_still":
            captured.append(layer)  # blur params are edited in the JSON
            continue
        track = layer["track"] + TRACK_OFFSET
        items = timeline.GetItemListInTrack("video", track)
        if not items:
            fail(f"no clip on V{track} - is this a TikTok "
                 f"project built by this script?")
        got = read_layer(items[0], layer, fw, fh)
        captured.append(got)
        print(f"V{track} {got['name']}: zoom={got['zoom']} "
              f"pan={got['pan']} tilt={got['tilt']} crop={got['crop_norm']}")

    preset["layers"] = captured
    preset_path.write_text(json.dumps(preset, indent=2) + "\n",
                           encoding="utf-8")
    print(f"Preset written: {preset_path}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build a vertical TikTok project in DaVinci Resolve")
    ap.add_argument("video", nargs="?", help="recording to build from")
    ap.add_argument("--save", action="store_true",
                    help="capture the open timeline's tune into the preset")
    ap.add_argument("--preset", type=Path, default=DEFAULT_PRESET,
                    help=f"preset JSON (default: {DEFAULT_PRESET.name})")
    args = ap.parse_args()

    preset = load_preset(args.preset)
    if args.save:
        save(args.preset, preset)
        return

    video = Path(args.video) if args.video else pick_video()
    if not video.is_file():
        fail(f"video not found: {video}")
    build(video, preset)


if __name__ == "__main__":
    main()
