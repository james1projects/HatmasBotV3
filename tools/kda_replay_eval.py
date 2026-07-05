"""
tools/kda_replay_eval.py — replay the KDA detector against saved
footage and diff the result with the archived .events.json sibling.

The archived events were written by an earlier detector run (see
process_recordings.py / move_and_emit) and then survived James's
highlight review, so they're the best available reference. This
harness reruns today's detector over the same video with zero side
effects (no file moves, no dashboard posts) and reports:

    matched  — archived event with a detected event of the same type
               within the tolerance window
    missed   — archived event today's detector did NOT reproduce
    new      — event today's detector found that isn't in the archive

Full agreement (no missed, no new) exits 0 — a regression harness for
any change to core/kda_reader.py or tools/vod_detector.py. Any
disagreement exits 1 and prints timestamps to eyeball in the VOD.

Run:
    python tools/kda_replay_eval.py "recordings/Atlas/Atlas-1.mp4"
    python tools/kda_replay_eval.py recordings/Atlas          # whole dir
    python tools/kda_replay_eval.py recordings/Atlas --ffmpeg <path> \
        --ffprobe <path> --hwaccel cuda
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.kda_reader import KdaReader  # noqa: E402
from tools.vod_detector import VodDetector, VodDetectorOptions  # noqa: E402

# An archived event and a detected event of the same type within this
# many seconds count as the same event. The archive came from a
# detector with 0.2s refinement, but coarse sampling differences can
# shift an event by up to one coarse interval.
MATCH_WINDOW_SEC = 8.0

# Same convention as diagnose_god_detection.py / extract_events.py:
# use the standard Windows Tesseract install when present.
DEFAULT_TESSERACT_WIN = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def diff_events(archived, detected):
    """Greedy nearest-first pairing of archived vs detected events."""
    unmatched_det = list(detected)
    matched, missed = [], []
    for a in archived:
        a_type = a["type"]
        a_t = float(a["timestamp_sec"])
        best = None
        best_dt = MATCH_WINDOW_SEC + 1
        for d in unmatched_det:
            if d["type"] != a_type:
                continue
            dt = abs(float(d["timestamp_sec"]) - a_t)
            if dt < best_dt:
                best, best_dt = d, dt
        if best is not None and best_dt <= MATCH_WINDOW_SEC:
            matched.append((a, best, best_dt))
            unmatched_det.remove(best)
        else:
            missed.append(a)
    return matched, missed, unmatched_det


def eval_video(video: Path, opts: VodDetectorOptions, verbose: bool,
               tesseract_path):
    events_file = video.parent / (video.stem + ".events.json")
    if not events_file.exists():
        print(f"  SKIP (no events.json): {video.name}")
        return None

    archived = json.loads(events_file.read_text(encoding="utf-8"))["events"]
    # The archive only ever contains kills and deaths; keep types the
    # detector is asked to produce aligned with what we compare.
    archived = [e for e in archived if e["type"] in ("kill", "death")]

    reader = KdaReader(data_dir=REPO_ROOT / "data",
                       tesseract_path=tesseract_path)
    detector = VodDetector(reader, opts)

    t0 = time.time()
    detected = detector.detect(str(video))
    scan_s = time.time() - t0
    detected = [e for e in detected if e["type"] in ("kill", "death")]

    matched, missed, new = diff_events(archived, detected)

    print(f"  {video.name}: archive {len(archived)}, detected {len(detected)} "
          f"-> matched {len(matched)}, missed {len(missed)}, new {len(new)} "
          f"({scan_s:.0f}s scan)")
    if verbose or missed or new:
        for a, d, dt in matched:
            print(f"    match  {a['type']:<5} archive {a['timestamp_sec']:>8.1f}s "
                  f"~ detected {d['timestamp_sec']:>8.1f}s (d={dt:.1f}s)")
    for a in missed:
        print(f"    MISSED {a['type']:<5} archived at {a['timestamp_sec']:.1f}s "
              f"{('(' + a['note'] + ')') if a.get('note') else ''}")
    for d in new:
        print(f"    NEW    {d['type']:<5} detected at {d['timestamp_sec']:.1f}s "
              f"(not in archive — eyeball the VOD here)")
    return len(missed), len(new)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="video file or directory of videos")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--hwaccel", default=None,
                        help="cuda | d3d11va | auto | (default: software)")
    parser.add_argument("--coarse-interval", type=float, default=5.0)
    parser.add_argument("--tesseract", default=None,
                        help=f"path to tesseract.exe (default: "
                             f"{DEFAULT_TESSERACT_WIN} when it exists)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    tesseract_path = args.tesseract
    if tesseract_path is None and Path(DEFAULT_TESSERACT_WIN).exists():
        tesseract_path = DEFAULT_TESSERACT_WIN

    target = Path(args.target)
    videos = sorted(target.glob("*.mp4")) if target.is_dir() else [target]
    videos = [v for v in videos
              if (v.parent / (v.stem + ".events.json")).exists()]
    if not videos:
        print("No videos with .events.json siblings found.")
        return 1

    opts = VodDetectorOptions(
        coarse_interval=args.coarse_interval,
        include_deaths=True,
        include_assists=False,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        hwaccel=args.hwaccel,
        enable_god_detection=False,
        # Keep raw per-event output: merging kill+death clip windows is
        # for Vegas exports and would break 1:1 diffing here.
        merge_overlaps=False,
    )

    total_missed = total_new = 0
    print(f"Replaying detector over {len(videos)} video(s):")
    for video in videos:
        result = eval_video(video, opts, args.verbose, tesseract_path)
        if result is not None:
            total_missed += result[0]
            total_new += result[1]

    print(f"\nTOTAL: missed {total_missed}, new {total_new}")
    agree = total_missed == 0 and total_new == 0
    print("RESULT:", "FULL AGREEMENT" if agree else "DISAGREEMENTS — review above")
    return 0 if agree else 1


if __name__ == "__main__":
    sys.exit(main())
