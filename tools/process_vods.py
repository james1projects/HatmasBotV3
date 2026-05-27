"""
process_vods.py — Sony Vegas automation pipeline orchestrator.

Scans an inbox of .mp4 OBS recordings and drives Sony Vegas via a single
mega-script (vegas_scripts/ProcessVideo.cs) per video. For each video:

    1. Run extract_events.py if no sibling .events.json exists yet.
    2. Write jobs/current.json with source + preset + render template info.
    3. Launch Vegas with -SCRIPT:ProcessVideo.cs.
    4. Poll jobs/phase_ready.flag. When it appears, prompt the user to
       review the Phase A timeline in Vegas and press Enter to render.
    5. On Enter, write jobs/go.flag. Vegas consumes it and renders Phase A.
    6. Wait for phase_ready.flag (now "B"). Prompt again.
    7. On Enter, write jobs/go.flag. Vegas renders Phase B and exits.
    8. Move the processed .mp4 to inbox/processed/ (unless --keep).

IMPORTANT: Vegas's CLI `-SCRIPT:` always spawns a NEW Vegas window, even
if one is already open. Preflight refuses to start when vegas210.exe is
already running, to avoid orphaned windows and ambiguous flag handoffs.
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────────
#   DEFAULT PATHS
# ──────────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).resolve().parent           # tools/
REPO_ROOT    = SCRIPT_DIR.parent                         # HatmasBot/
DEFAULT_CONFIG = REPO_ROOT / "config" / "vegas_pipeline.json"


# ──────────────────────────────────────────────────────────────────────────
#   ARGUMENT PARSING
# ──────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive Sony Vegas through inbox .mp4s, "
                    "producing full-gameplay + highlight renders per video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
    python tools\\process_vods.py
    python tools\\process_vods.py --include "*ranked*" --keep
""")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help=f"Path to vegas_pipeline.json (default: {DEFAULT_CONFIG})")
    p.add_argument("--include", default="*.mp4",
                   help="Glob pattern to filter inbox files (default: *.mp4)")
    p.add_argument("--keep", action="store_true",
                   help="Do NOT move processed .mp4s to inbox/processed/.")
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be processed and exit.")
    p.add_argument("--allow-existing-vegas", action="store_true",
                   help="Bypass the 'is Vegas already running?' check. "
                        "Only safe if you're sure — -SCRIPT: always spawns "
                        "a NEW window, so you'll end up with multiple "
                        "Vegas instances and possibly confused flag handoffs.")
    return p.parse_args()


# ──────────────────────────────────────────────────────────────────────────
#   CONFIG
# ──────────────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        die(f"Config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Resolve all paths to absolute (Python pathlib is forgiving).
    required = [
        "vegas_exe", "inbox_dir", "rendered_dir", "highlight_dir",
        "preset_dir", "jobs_dir", "scripts_dir",
        "render_templates", "presets",
    ]
    for key in required:
        if key not in cfg:
            die(f"Config missing required key '{key}': {config_path}")
    return cfg


# ──────────────────────────────────────────────────────────────────────────
#   IS VEGAS RUNNING?  (Windows tasklist — no psutil dependency)
# ──────────────────────────────────────────────────────────────────────────

def is_vegas_running(exe_name: str = "vegas210.exe") -> bool:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}"],
            stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return False  # can't tell — don't block the user
    return exe_name.lower() in out.lower()


# ──────────────────────────────────────────────────────────────────────────
#   EVENT EXTRACTION (delegates to extract_events.py)
# ──────────────────────────────────────────────────────────────────────────

def run_event_extraction(inbox_dir: Path, videos: list[Path]) -> None:
    """Run extract_events.py on the whole inbox folder once, up front.

    extract_events.py takes a folder (not a per-file glob) and skips any
    .mp4 that already has a sibling .events.json. So calling it once on
    the inbox gives us all the .events.json files we need for this batch
    without forcing a re-scan of already-processed videos.
    """
    missing = [v for v in videos if not v.with_suffix(".events.json").exists()]
    if not missing:
        return  # All events.json already present.

    extractor = SCRIPT_DIR / "extract_events.py"
    if not extractor.exists():
        die(f"extract_events.py not found at {extractor}")

    print(f"Extracting events for {len(missing)} video(s) "
          f"(extract_events will skip the rest)...")
    cmd = [sys.executable, str(extractor), str(inbox_dir)]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        die(f"extract_events.py failed (exit code {result.returncode}).")

    # Sanity-check: every video we listed must now have a sibling .events.json.
    still_missing = [v for v in missing
                     if not v.with_suffix(".events.json").exists()]
    if still_missing:
        names = ", ".join(v.name for v in still_missing)
        die(f"extract_events.py finished but these have no .events.json: {names}")
    print()


# ──────────────────────────────────────────────────────────────────────────
#   JOB FILE
# ──────────────────────────────────────────────────────────────────────────

def write_current_job(cfg: dict, mp4: Path, events: Path) -> None:
    jobs_dir = Path(cfg["jobs_dir"])
    jobs_dir.mkdir(parents=True, exist_ok=True)

    job = {
        "source_video":              str(mp4),
        "events_json":               str(events),
        "full_preset":               cfg["presets"]["full_gameplay"],
        "highlight_preset":          cfg["presets"]["highlight"],
        "full_render_template":      cfg["render_templates"]["full_gameplay"],
        "highlight_render_template": cfg["render_templates"]["highlight"],
        "rendered_dir":              cfg["rendered_dir"],
        "highlight_dir":              cfg["highlight_dir"],
    }
    (jobs_dir / "current.json").write_text(
        json.dumps(job, indent=2), encoding="utf-8")


def clear_stale_flags(jobs_dir: Path) -> None:
    for name in ("go.flag", "phase_ready.flag",
                 "phase_done.flag", "error.flag"):
        p = jobs_dir / name
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────
#   FLAG POLLING
# ──────────────────────────────────────────────────────────────────────────

def wait_for_phase_ready(jobs_dir: Path, vegas_proc: subprocess.Popen,
                         poll_sec: float = 0.25) -> str:
    """Block until jobs/phase_ready.flag appears. Return its text (phase label).

    Also bails out if the Vegas process dies early (returns ''), or if
    jobs/error.flag appears (dies with the error message).
    """
    flag    = jobs_dir / "phase_ready.flag"
    err     = jobs_dir / "error.flag"
    done    = jobs_dir / "phase_done.flag"

    while True:
        if err.exists():
            msg = err.read_text(encoding="utf-8", errors="replace")
            die(f"Vegas reported an error:\n{msg}")
        if flag.exists():
            try:
                return flag.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return ""
        if done.exists():
            return ""
        if vegas_proc.poll() is not None:
            return ""
        time.sleep(poll_sec)


def write_go_flag(jobs_dir: Path) -> None:
    (jobs_dir / "go.flag").write_text(
        time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")


def wait_for_go_consumed(jobs_dir: Path, vegas_proc: subprocess.Popen,
                         timeout_sec: float = 60.0,
                         poll_sec: float = 0.1) -> None:
    """Wait until Vegas consumes (deletes) go.flag. Warns after timeout."""
    flag = jobs_dir / "go.flag"
    start = time.time()
    while flag.exists():
        if vegas_proc.poll() is not None:
            return
        if time.time() - start > timeout_sec:
            print("  ! go.flag not consumed after "
                  f"{timeout_sec:.0f}s — Vegas may be stuck.", flush=True)
            return
        time.sleep(poll_sec)


# ──────────────────────────────────────────────────────────────────────────
#   PER-VIDEO PIPELINE
# ──────────────────────────────────────────────────────────────────────────

def process_one(cfg: dict, mp4: Path, idx: int, total: int) -> None:
    tag = f"[{idx}/{total}] {mp4.name}"
    print(f"{tag}  ▶", flush=True)

    # Step 1: events.json must already exist (batch-extracted before this loop).
    events = mp4.with_suffix(".events.json")
    if not events.exists():
        die(f"Missing {events.name} — was extract_events run?")

    # Step 2: job file + flag cleanup
    jobs_dir = Path(cfg["jobs_dir"])
    write_current_job(cfg, mp4, events)
    clear_stale_flags(jobs_dir)

    # Step 3: launch Vegas
    vegas_exe    = cfg["vegas_exe"]
    scripts_dir  = cfg["scripts_dir"]
    script_path  = str(Path(scripts_dir) / "ProcessVideo.cs")

    print(f"  launching Vegas ...", flush=True)
    proc = subprocess.Popen(
        [vegas_exe, f"-SCRIPT:{script_path}"],
        cwd=str(REPO_ROOT))

    try:
        # Phase A wait + prompt + render trigger
        label = wait_for_phase_ready(jobs_dir, proc)
        if label.upper().startswith("A") or "FULL" in label.upper():
            prompt = "full gameplay"
        else:
            prompt = f"phase A ({label or 'unknown'})"
        input(f"  ↪ {prompt} timeline ready. Tweak in Vegas, "
              "then press Enter to render... ")
        write_go_flag(jobs_dir)
        wait_for_go_consumed(jobs_dir, proc)
        print(f"  rendering {prompt} (may take a while)...", flush=True)

        # Phase B wait + prompt + render trigger
        label = wait_for_phase_ready(jobs_dir, proc)
        if not label:
            # Process may have already exited or errored.
            rc = proc.poll()
            if rc is not None:
                err = jobs_dir / "error.flag"
                if err.exists():
                    die(f"Vegas error during Phase A/B transition:\n"
                        f"{err.read_text(encoding='utf-8', errors='replace')}")
                die(f"Vegas exited unexpectedly after Phase A "
                    f"(return code {rc}).")
        if label.upper().startswith("B") or "HIGHLIGHT" in label.upper():
            prompt2 = "highlight"
        else:
            prompt2 = f"phase B ({label or 'unknown'})"
        input(f"  ↪ {prompt2} timeline ready. Tweak in Vegas, "
              "then press Enter to render... ")
        write_go_flag(jobs_dir)
        wait_for_go_consumed(jobs_dir, proc)
        print(f"  rendering {prompt2} (may take a while)...", flush=True)

        # Wait for Vegas to exit (or phase_done.flag).
        while proc.poll() is None:
            if (jobs_dir / "phase_done.flag").exists():
                break
            if (jobs_dir / "error.flag").exists():
                err_text = (jobs_dir / "error.flag").read_text(
                    encoding="utf-8", errors="replace")
                die(f"Vegas error during rendering:\n{err_text}")
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n  Ctrl+C — terminating Vegas child...", flush=True)
        try:
            proc.terminate()
        except Exception:
            pass
        raise


# ──────────────────────────────────────────────────────────────────────────
#   MAIN
# ──────────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()
    cfg  = load_config(args.config)

    # Make every relevant dir exist so later writes succeed.
    for key in ("inbox_dir", "rendered_dir", "highlight_dir",
                "jobs_dir", "preset_dir"):
        Path(cfg[key]).mkdir(parents=True, exist_ok=True)
    (Path(cfg["inbox_dir"]) / "processed").mkdir(parents=True, exist_ok=True)

    # Preflight: refuse to run if Vegas is already open. CLI -SCRIPT:
    # would spawn a NEW window and leave both instances confused.
    if is_vegas_running() and not args.allow_existing_vegas:
        die("Sony Vegas (vegas210.exe) is already running. "
            "Close it and re-run, or pass --allow-existing-vegas "
            "if you know what you're doing.")

    # Scan inbox.
    inbox = Path(cfg["inbox_dir"])
    all_mp4s = sorted(p for p in inbox.glob("*.mp4") if p.is_file())
    videos = [p for p in all_mp4s if fnmatch.fnmatch(p.name, args.include)]

    if not videos:
        die(f"No matching videos in {inbox} (pattern: {args.include}).")

    print(f"Found {len(videos)} video(s) in {inbox}:")
    for v in videos:
        print(f"  - {v.name}")
    print()

    if args.dry_run:
        print("--dry-run set, exiting.")
        return 0

    # Batch-extract events for any video missing its .events.json. Doing
    # this once up front (rather than per-video) matches extract_events.py's
    # folder-scan interface and avoids repeated Python startups.
    run_event_extraction(inbox, videos)

    total = len(videos)
    for idx, mp4 in enumerate(videos, start=1):
        try:
            process_one(cfg, mp4, idx, total)
        except SystemExit:
            raise
        except Exception as e:
            print(f"  ! processing {mp4.name} failed: {e}", flush=True)
            print(f"  ! leaving {mp4.name} in inbox/ for inspection.", flush=True)
            continue

        # Move to processed/.
        if not args.keep:
            processed_dir = inbox / "processed"
            target = processed_dir / mp4.name
            if target.exists():
                # Avoid clobber — append timestamp.
                stamp = time.strftime("%Y%m%d_%H%M%S")
                target = processed_dir / f"{mp4.stem}_{stamp}{mp4.suffix}"
            try:
                mp4.rename(target)
                # Also move the sibling .events.json to keep them paired.
                events = mp4.with_suffix(".events.json")
                if events.exists():
                    events.rename(target.with_suffix(".events.json"))
            except OSError as e:
                print(f"  ! could not move {mp4.name} to processed/: {e}",
                      flush=True)

        print(f"  ✓ {mp4.name} done.\n", flush=True)

    print("All videos processed.")
    return 0


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
