#!/usr/bin/env python3
"""
resolve_import.py
=================
One-button "video -> ready-to-edit DaVinci Resolve project" for gameplay
recordings.  Designed for a Stream Deck button (see resolve_import.bat).

What it does:
  1. Pops a file picker over the recordings folder (skipped when a video
     path is passed on the command line).
  2. Connects to DaVinci Resolve, launching it first if it isn't running.
  3. Creates a fresh project named after the recording (unique-ified with
     " (2)", " (3)", ... if the name is taken) by importing
     resolve_gameplay_template.drp — the template carries the 1080p60
     master settings *including the playback frame rate*, which the API
     cannot set (SetSetting("timelinePlaybackFrameRate", ...) always
     returns False in Resolve 21 and GetSetting reads back a stale value;
     a project left at the default 24 plays a 60fps timeline in slow
     motion at 40% speed).
  4. Re-checks timeline resolution and frame rate against the clip in
     case a recording ever deviates from 1080p60.
  5. Imports the recording and builds a timeline from it.  OBS multi-track
     audio comes in as one stereo timeline track per OBS track.
  6. Names the audio tracks (Game / Mic / Discord / Misc), switches to the
     Edit page and saves.

Requires DaVinci Resolve *Studio* (the free edition doesn't allow external
scripting) with external scripting permitted (Preferences > System >
General > "External scripting using" set to Local — the default works).

Usage:
    python tools/resolve_import.py             # file picker
    python tools/resolve_import.py <video>     # skip the picker
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# --- configuration -----------------------------------------------------------

RECORDINGS_DIR = Path(r"C:\Projects\HatmasBot\recordings")

# OBS track order -> Resolve audio track names (A1..A4).
AUDIO_TRACK_NAMES = ["Game", "Mic", "Discord", "Misc"]

VIDEO_EXTENSIONS = [("Videos", "*.mp4 *.mkv *.mov"), ("All files", "*.*")]

PROJECT_TEMPLATE = Path(__file__).with_name("resolve_gameplay_template.drp")

RESOLVE_EXE = Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe")
RESOLVE_MODULES = (
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve"
    r"\Support\Developer\Scripting\Modules"
)
RESOLVE_LAUNCH_TIMEOUT = 180  # seconds to wait for the API after launching


def fail(msg: str) -> "NoReturn":
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# --- step 1: pick the video --------------------------------------------------

def pick_video() -> Path:
    if len(sys.argv) > 1:
        video = Path(sys.argv[1])
        if not video.is_file():
            fail(f"video not found: {video}")
        return video

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # picker must beat Resolve for focus
    initial = RECORDINGS_DIR if RECORDINGS_DIR.is_dir() else Path.home()
    name = filedialog.askopenfilename(
        parent=root,
        title="Import gameplay into DaVinci Resolve",
        initialdir=str(initial),
        filetypes=VIDEO_EXTENSIONS,
    )
    root.destroy()
    if not name:
        print("No file selected — nothing to do.")
        sys.exit(0)
    return Path(name)


# --- step 2: connect to Resolve ----------------------------------------------

def connect_resolve():
    sys.path.append(RESOLVE_MODULES)
    import DaVinciResolveScript as dvr

    resolve = dvr.scriptapp("Resolve")
    if resolve:
        return resolve

    if not RESOLVE_EXE.is_file():
        fail(f"Resolve not installed at {RESOLVE_EXE}")
    print("DaVinci Resolve isn't running — launching it...")
    subprocess.Popen([str(RESOLVE_EXE)])
    deadline = time.monotonic() + RESOLVE_LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(3)
        resolve = dvr.scriptapp("Resolve")
        if resolve:
            # The API answers before the project manager is fully up;
            # poll until it can actually hand out projects.
            if resolve.GetProjectManager():
                return resolve
    fail("timed out waiting for DaVinci Resolve to accept API connections")


# --- step 3-6: build the project ---------------------------------------------

def unique_project_name(pm, base: str) -> str:
    taken = set(pm.GetProjectListInCurrentFolder() or [])
    if base not in taken:
        return base
    n = 2
    while f"{base} ({n})" in taken:
        n += 1
    return f"{base} ({n})"


def main() -> None:
    video = pick_video()
    print(f"Video: {video}")

    resolve = connect_resolve()
    pm = resolve.GetProjectManager()

    name = unique_project_name(pm, video.stem)
    if PROJECT_TEMPLATE.is_file():
        # ImportProject + LoadProject instead of CreateProject: the template
        # bakes in the playback frame rate, which is not API-settable.
        if not pm.ImportProject(str(PROJECT_TEMPLATE), name):
            fail(f"could not import project template {PROJECT_TEMPLATE}")
        project = pm.LoadProject(name)
        if not project:
            fail(f"imported {name!r} but could not load it")
    else:
        print(f"WARNING: template missing ({PROJECT_TEMPLATE}) — creating a "
              f"blank project; playback may run at 24fps (slow motion) until "
              f"you set Project Settings > Master Settings > Playback frame rate.")
        project = pm.CreateProject(name)
        if not project:
            fail(f"could not create project {name!r}")
    print(f"Project: {name}")

    media_pool = project.GetMediaPool()
    clips = media_pool.ImportMedia([str(video)])
    if not clips:
        fail("Resolve could not import the video (unsupported codec/container?)")
    clip = clips[0]

    # The template is already 1080p60; adjust only if this recording differs.
    # Must happen *before* the timeline exists — frame rate is locked per
    # timeline once created.
    fps = clip.GetClipProperty("FPS")
    resolution = clip.GetClipProperty("Resolution") or ""
    if fps:
        fps = str(int(fps) if float(fps) == int(float(fps)) else fps)
        if fps != "60":
            print(f"WARNING: clip is {fps}fps but the template's playback frame "
                  f"rate is fixed at 60 — playback speed will be wrong until you "
                  f"change Project Settings > Master Settings > Playback frame rate "
                  f"to {fps} (the API cannot set it).")
        if not project.SetSetting("timelineFrameRate", fps):
            print(f"WARNING: could not set timeline frame rate to {fps}")
    if "x" in resolution:
        width, height = resolution.split("x")
        project.SetSetting("timelineResolutionWidth", width)
        project.SetSetting("timelineResolutionHeight", height)

    timeline = media_pool.CreateTimelineFromClips(video.stem, [clip])
    if not timeline:
        fail("could not create timeline")

    audio_tracks = timeline.GetTrackCount("audio")
    print(f"Timeline: {timeline.GetName()} "
          f"({timeline.GetTrackCount('video')} video / {audio_tracks} audio tracks)")
    if audio_tracks < len(AUDIO_TRACK_NAMES):
        print(f"WARNING: expected {len(AUDIO_TRACK_NAMES)} audio tracks, "
              f"got {audio_tracks} — was this recorded with all OBS tracks enabled?")
    for i, track_name in enumerate(AUDIO_TRACK_NAMES[:audio_tracks], start=1):
        timeline.SetTrackName("audio", i, track_name)

    resolve.OpenPage("edit")
    pm.SaveProject()
    print("Done — project is open on the Edit page.")


if __name__ == "__main__":
    main()
