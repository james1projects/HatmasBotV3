#!/usr/bin/env python3
"""
sort_unknowns.py
================
Walk through ``recordings/unknown/`` one recording at a time, suggest
the most likely god from a quick sample, ask you to confirm, then:

  1. Capture a portrait reference for that god (under
     ``Portrait_Source/<God>/<recording-basename>.png``) using the same
     decode pipeline you'd run real scans with.
  2. Move the .mp4 and its sibling .events.json into the right
     per-god subfolder under ``recordings/``, with the standard
     ``{God}-N.{ext}`` naming and the JSON's ``source_video`` rewritten.

Each captured reference accumulates as another fingerprint for that
god, so future scans (CUDA or otherwise) recognise both the in-game
portrait *and* whatever custom OBS overlay was visible at capture
time.  Same god, multiple visual sources, all stored together under
the god's subfolder.

Usage
-----
    python tools/sort_unknowns.py
    python tools/sort_unknowns.py --hwaccel cuda          # match orchestrator
    python tools/sort_unknowns.py --source recordings/unknown
    python tools/sort_unknowns.py --no-open               # skip auto-opening
                                                          # the preview folder

Per-recording prompt:
    [Y]es     → use the suggested god
    [n]       → type the correct god name
    [O]ther   → not actual gameplay (lobby, menu, demo, etc.) — moves
                to recordings/Other/Other-N.<ext> without capturing
                a portrait reference, since there's no god to learn
    [s]kip    → leave this recording in unknown/, move on
    [?]       → show top-3 candidates from each sampled frame
    [r]eopen  → re-open the preview folder
    [q]uit    → stop the loop, leave remaining recordings in place
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.god_matcher import GodMatcher, PORTRAIT_REGION
from tools.extract_events import render_events_json, find_mp4s
from tools.process_recordings import next_index


DEFAULT_SOURCE = _REPO_ROOT / "recordings" / "unknown"
DEFAULT_TARGET_ROOT = _REPO_ROOT / "recordings"
DEFAULT_DATA_DIR = _REPO_ROOT / "data"
DEFAULT_OVERLAY_ICONS_DIR = _REPO_ROOT / "Custom God Icons"
DEFAULT_REFERENCE_ICONS_DIR = _REPO_ROOT / "Portrait_Source"
DEFAULT_PREVIEWS_DIR = _REPO_ROOT / "data" / "sort_previews"


# --- ffmpeg helpers --------------------------------------------------------

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
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


# --- Per-recording sampling ------------------------------------------------

def _sample_recording(
    video: Path,
    matcher: GodMatcher,
    *,
    samples: int,
    hwaccel: Optional[str],
    ffmpeg: str,
    ffprobe: str,
) -> list[dict]:
    """Sample ``samples`` evenly-spaced frames between 15-85% of duration.

    For each: extract the frame, identify_top_n, save the portrait crop.
    Returns a list of dicts (one per sample) — empty if duration probe
    failed or no frames extracted.  Each dict::
        {"t": seconds, "img": PIL.Image, "top": [(god, score), ...],
         "best_god": str | None, "best_conf": float}
    """
    duration = _probe_duration(video, ffprobe)
    if duration is None or duration <= 0:
        return []
    if samples < 1:
        return []
    if samples == 1:
        ts_list = [duration * 0.5]
    else:
        ts_list = [
            duration * (0.15 + (0.85 - 0.15) * i / (samples - 1))
            for i in range(samples)
        ]

    out: list[dict] = []
    for ts in ts_list:
        img = _extract_frame(video, ts, ffmpeg, hwaccel=hwaccel)
        if img is None:
            continue
        top = matcher.identify_top_n(img, n=3)
        # We use the top-1 score even when below the matcher's
        # acceptance threshold — these are unknown recordings, the
        # whole point is "matcher failed but we can still see what it
        # *thought* was best."
        best_god = top[0][0] if top else None
        best_conf = top[0][1] if top else 0.0
        out.append({
            "t": ts,
            "img": img,
            "top": top,
            "best_god": best_god,
            "best_conf": best_conf,
        })
    return out


def _suggest_god(samples: list[dict]) -> tuple[Optional[str], float]:
    """Pick the most-likely god across sampled frames.

    Strategy: count how often each god is the per-frame #1.  The god
    that wins the most frames is the suggestion.  Confidence reported
    is the mean top-1 score for that god across the frames it won.
    """
    if not samples:
        return None, 0.0
    counts: dict[str, int] = {}
    sum_conf: dict[str, float] = {}
    for s in samples:
        g = s["best_god"]
        if g is None:
            continue
        counts[g] = counts.get(g, 0) + 1
        sum_conf[g] = sum_conf.get(g, 0.0) + s["best_conf"]
    if not counts:
        return None, 0.0
    # Tiebreak by total accumulated confidence so a god that wins the
    # same number of frames at higher scores beats a god that wins by
    # narrow margins.
    best_god = max(
        counts.keys(),
        key=lambda g: (counts[g], sum_conf[g]),
    )
    mean_conf = sum_conf[best_god] / counts[best_god]
    return best_god, mean_conf


# --- Preview I/O -----------------------------------------------------------

def _save_previews(
    samples: list[dict],
    preview_dir: Path,
) -> Path:
    """Drop a frame.png and a portrait.png per sample into preview_dir."""
    preview_dir.mkdir(parents=True, exist_ok=True)
    # Wipe stale previews so the folder only shows this recording's data.
    for old in preview_dir.iterdir():
        try:
            if old.is_file():
                old.unlink()
            elif old.is_dir():
                shutil.rmtree(old)
        except Exception:
            pass
    for i, s in enumerate(samples, 1):
        # Save full frame for context.
        full_path = preview_dir / f"sample{i:02d}_t{s['t']:07.1f}_frame.png"
        s["img"].save(str(full_path))
        # Save portrait crop, upscaled 4x, for at-a-glance god ID.
        portrait = s["img"].crop(PORTRAIT_REGION)
        portrait_4x = portrait.resize(
            (portrait.size[0] * 4, portrait.size[1] * 4),
            Image.NEAREST,
        )
        portrait_path = (
            preview_dir / f"sample{i:02d}_t{s['t']:07.1f}_portrait.png"
        )
        portrait_4x.save(str(portrait_path))
    return preview_dir


def _open_folder(folder: Path) -> None:
    """Best-effort open in the OS file manager."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception:
        pass  # never fatal


# --- Reference capture -----------------------------------------------------

def _capture_reference(
    samples: list[dict],
    god_name: str,
    output_root: Path,
    recording_basename: str,
) -> Optional[Path]:
    """Save a portrait crop for ``god_name`` from the highest-confidence
    sample.

    Always saves into the nested layout
    ``Portrait_Source/<God>/<recording-basename>.png`` so multiple
    references for the same god accumulate cleanly without overwriting
    each other.

    Returns the saved path, or None if there's nothing to capture.
    """
    if not samples:
        return None
    # Pick the sample where the matcher was most confident — even if
    # that sample's top-1 wasn't ``god_name`` (the user may have
    # corrected the suggestion, but the cleanest *frame* is still the
    # one where the matcher had the highest signal).
    best_sample = max(samples, key=lambda s: s["best_conf"])
    portrait = best_sample["img"].crop(PORTRAIT_REGION)
    god_dir = output_root / god_name
    god_dir.mkdir(parents=True, exist_ok=True)
    out_path = god_dir / f"{recording_basename}.png"
    portrait.save(str(out_path))
    return out_path


# --- Move + JSON-update ----------------------------------------------------

def _read_events_json(path: Path) -> tuple[list[dict], list[str]]:
    """Best-effort read of a Python-emitted .events.json — returns
    ``(events, gods_seen)``.  If the file is missing or unparseable,
    returns ``([], [])``."""
    if not path.exists():
        return [], []
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        events = list(data.get("events") or [])
        gods_seen = list(data.get("gods_seen") or [])
        return events, gods_seen
    except Exception:
        return [], []


def _move_and_emit(
    video: Path,
    god_name: str,
    target_root: Path,
    *,
    gods_seen: Optional[list[str]] = None,
) -> tuple[Path, Path]:
    """Move ``video`` (and its sibling .events.json if present) into
    ``target_root/<God>/`` with the standard ``{God}-N.{ext}`` name,
    rewriting source_video in the JSON.  Returns ``(new_video,
    new_json)``.

    ``gods_seen`` overrides the JSON's gods_seen field.  Defaults to
    ``[god_name]`` (the typical case where the folder name IS the
    confirmed god).  Pass an empty list for Other / lobby / non-game
    clips so downstream consumers don't see "Other" listed as a god.
    """
    target_dir = target_root / god_name
    target_dir.mkdir(parents=True, exist_ok=True)
    n = next_index(target_dir, god_name)
    new_video = target_dir / f"{god_name}-{n}{video.suffix}"
    new_json = target_dir / f"{god_name}-{n}.events.json"

    # Read existing events (if any) so we preserve them across the move.
    old_json = video.with_suffix(".events.json")
    events, _ = _read_events_json(old_json)

    if gods_seen is None:
        gods_seen = [god_name]

    shutil.move(str(video), str(new_video))

    # Force gods_seen to the user-confirmed value; preserve existing
    # events if they were detected on the original scan.
    payload = render_events_json(
        str(new_video.resolve()),
        events,
        gods_seen=gods_seen,
    )
    new_json.write_text(payload, encoding="utf-8")

    # Clean up the old .events.json now that the new one's in place.
    if old_json.exists() and old_json.resolve() != new_json.resolve():
        try:
            old_json.unlink()
        except Exception:
            pass

    return new_video, new_json


# --- CLI -------------------------------------------------------------------

def _print_top3(samples: list[dict]) -> None:
    print()
    for i, s in enumerate(samples, 1):
        print(f"  sample {i:2d}  t={s['t']:6.1f}s")
        for name, score in s["top"]:
            print(f"    {name:25s} {score:.3f}")
    print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Interactively process recordings/unknown/ one at a time, "
            "capturing a portrait reference for each confirmed god and "
            "moving the recording into its proper subfolder."
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"Folder of unknown recordings (default: {DEFAULT_SOURCE}).",
    )
    parser.add_argument(
        "--target-root",
        type=Path,
        default=DEFAULT_TARGET_ROOT,
        help=(
            f"Root folder where confirmed recordings move into "
            f"per-god subfolders (default: {DEFAULT_TARGET_ROOT})."
        ),
    )
    parser.add_argument(
        "--reference-dir",
        type=Path,
        default=DEFAULT_REFERENCE_ICONS_DIR,
        help=(
            f"Where to save captured portrait references (default: "
            f"{DEFAULT_REFERENCE_ICONS_DIR}).  Each capture goes into "
            f"<reference-dir>/<God>/<recording-basename>.png."
        ),
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=6,
        help="Frames to sample per recording for the suggestion (default: 6).",
    )
    parser.add_argument(
        "--hwaccel",
        default="cuda",
        help=(
            "ffmpeg -hwaccel for sampling AND reference capture.  "
            "Should match what your orchestrator runs.  Pass 'none' "
            "to disable.  Default: cuda."
        ),
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't auto-open the preview folder in the file manager.",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
    )
    parser.add_argument(
        "--overlay-icons-dir",
        type=Path,
        default=DEFAULT_OVERLAY_ICONS_DIR,
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"error: source folder does not exist: {args.source}",
              file=sys.stderr)
        return 2

    # Resolve hwaccel.
    hwaccel = None if args.hwaccel.lower() in ("", "none", "off", "no") \
        else args.hwaccel

    logging.basicConfig(level=logging.WARNING)

    # Build the matcher with all three sources so suggestions
    # benefit from any references already captured for OTHER gods.
    overlay_dir = args.overlay_icons_dir if args.overlay_icons_dir.exists() else None
    reference_dir = args.reference_dir if args.reference_dir.exists() else None
    matcher = GodMatcher(
        icons_dir=args.data_dir / "god_icons",
        overlay_icons_dir=overlay_dir,
        reference_icons_dir=reference_dir,
    )
    if not matcher.load_icons():
        print(
            f"error: god matcher loaded zero icons from "
            f"{args.data_dir / 'god_icons'}",
            file=sys.stderr,
        )
        return 2

    try:
        mp4s = find_mp4s(args.source)
    except (FileNotFoundError, NotADirectoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not mp4s:
        print(f"No recordings found in {args.source}.")
        return 0

    decoder_label = f"--hwaccel {hwaccel}" if hwaccel else "software"
    print(f"Found {len(mp4s)} recording(s) in {args.source}")
    print(f"Decoder for sampling + capture: {decoder_label}")
    print(f"References save to:             {args.reference_dir}")
    print(f"Per-god subfolders root:        {args.target_root}")
    print()

    moved = skipped = errors = 0
    quit_requested = False

    for idx, video in enumerate(mp4s, 1):
        if quit_requested:
            break
        prefix = f"[{idx}/{len(mp4s)}]"
        print(f"\n{'=' * 60}")
        print(f"{prefix} {video.name}")
        print("=" * 60)

        try:
            samples = _sample_recording(
                video, matcher,
                samples=args.samples,
                hwaccel=hwaccel,
                ffmpeg=args.ffmpeg, ffprobe=args.ffprobe,
            )
        except Exception as e:
            print(f"  ERROR sampling: {type(e).__name__}: {e}")
            errors += 1
            continue

        if not samples:
            print("  could not extract any frames — skipping")
            skipped += 1
            continue

        # Save preview frames + open the folder.
        preview_dir = DEFAULT_PREVIEWS_DIR / video.stem
        _save_previews(samples, preview_dir)
        print(f"  preview frames: {preview_dir}")
        if not args.no_open:
            _open_folder(preview_dir)

        # Build the suggestion.
        suggested, mean_conf = _suggest_god(samples)
        if suggested:
            print(
                f"  suggested god:  {suggested}  "
                f"(mean top-1 conf {mean_conf:.3f} across "
                f"{len([s for s in samples if s['best_god']==suggested])}/{len(samples)} samples)"
            )
        else:
            print("  suggested god:  (matcher returned no candidates)")

        # Prompt loop.  ``chosen_god`` becomes the target subfolder
        # name when set; ``is_other`` is the special path for non-game
        # clips that go to recordings/Other/ without a captured
        # portrait reference.
        chosen_god: Optional[str] = None
        is_other = False
        while True:
            options = "Y/n/O/?/r/skip/quit"
            prompt = (
                f"  Use '{suggested}'? [{options}] "
                if suggested
                else f"  Type god name (or O/skip/quit) [{options}]: "
            )
            try:
                ans = input(prompt).strip()
            except EOFError:
                quit_requested = True
                break
            ans_l = ans.lower()
            if ans_l in ("q", "quit"):
                quit_requested = True
                break
            if ans_l in ("s", "skip"):
                break
            if ans_l in ("o", "other"):
                # Not actual gameplay — move to recordings/Other/
                # without capturing a portrait reference.  We still
                # write a fresh .events.json at the new location so
                # downstream tooling sees the recording the same way
                # it would any sorted file.
                chosen_god = "Other"
                is_other = True
                break
            if ans_l in ("?", "details"):
                _print_top3(samples)
                continue
            if ans_l in ("r", "reopen"):
                _open_folder(preview_dir)
                continue
            if ans_l in ("", "y", "yes") and suggested is not None:
                chosen_god = suggested
                break
            if ans_l == "n":
                # Prompt again for a typed name.
                try:
                    typed = input("  Enter god name (or blank to cancel): ").strip()
                except EOFError:
                    typed = ""
                if not typed:
                    print("  cancelled — re-prompting for this recording")
                    continue
                chosen_god = typed
                break
            # Anything else is treated as a typed god name.
            chosen_god = ans
            break

        if quit_requested:
            print("  quit requested — leaving remaining recordings in place")
            break

        if chosen_god is None:
            print("  skipped")
            skipped += 1
            continue

        # Capture reference (skip when it's an 'Other' clip — there's
        # no god to learn, and we don't want a Portrait_Source/Other/
        # folder that the matcher would then start trying to match
        # against real gameplay frames).
        captured: Optional[Path] = None
        if not is_other:
            try:
                captured = _capture_reference(
                    samples, chosen_god, args.reference_dir, video.stem,
                )
            except Exception as e:
                print(f"  ERROR capturing reference: {type(e).__name__}: {e}")
                errors += 1
                continue

        # Move files.  For Other clips we pass an empty gods_seen so
        # the JSON doesn't mislead downstream tooling into thinking
        # "Other" is a god.
        try:
            new_video, new_json = _move_and_emit(
                video,
                chosen_god,
                args.target_root,
                gods_seen=[] if is_other else [chosen_god],
            )
        except Exception as e:
            print(f"  ERROR moving files: {type(e).__name__}: {e}")
            errors += 1
            continue

        rel_target = new_video.relative_to(args.target_root)
        if captured is not None:
            print(f"  reference saved: {captured.relative_to(_REPO_ROOT)}")
        elif is_other:
            print(f"  reference saved: (skipped — Other clip)")
        print(f"  moved to:        recordings/{rel_target}")
        moved += 1

    # Summary.
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  moved:   {moved}")
    print(f"  skipped: {skipped}")
    if errors:
        print(f"  errors:  {errors}")
    if quit_requested:
        remaining = len(mp4s) - moved - skipped - errors
        print(f"  remaining (unprocessed): {remaining}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
