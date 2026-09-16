"""
vodsearch/trim.py — keep the plays, drop the recording.

The pure and filesystem halves of the Trim page (core/vod_web.py serves
it at /vod/trim, local only). Everything here is synchronous; the web
layer runs it in threads.

    moments  = decisions + defaults  -> which kill moments to keep
    plan     = moments -> (start, end, out path) render jobs
    render   = ffmpeg subclip, full resolution, every OBS audio track
               copied through, verified with ffprobe before it counts
    trash    = move a recording (+ sidecar) to TRIM_TRASH_DIR once every
               kept moment has a verified clip; empty_trash deletes

Rules James set on 2026-09-15: kills only (deaths are not trim material),
multi-kills pre-checked and single kills unchecked, nothing deleted
until Empty trash, replay-buffer clips are exempt (they already are the
moment) so the page never lists a *_Replay.mp4 as trashable.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from . import clips as clips_mod
from .store import Store, tier_label

TIER_BONUS_S = 5.0            # extra tail per kill beyond the first in a streak
VERIFY_SLACK_S = 1.5          # ffprobe duration may be this much under the asked length
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ── decisions ─────────────────────────────────────────────────────────

def effective_decision(moment: dict, default_keep_tier: int) -> str:
    """Explicit decision wins; otherwise keep any streak at or above the
    default tier (2 = double kill and up)."""
    d = moment.get("decision")
    if d in ("keep", "skip"):
        return d
    return "keep" if int(moment.get("tier") or 1) >= int(default_keep_tier) else "skip"


def is_replay(path: Path | str) -> bool:
    return "replay" in Path(path).name.lower()


def annotate(rec: dict, default_keep_tier: int) -> dict:
    """Add the derived fields the page and the trash gate use."""
    kept = rendered = 0
    for m in rec.get("moments", []):
        m["effective"] = effective_decision(m, default_keep_tier)
        if m["effective"] == "keep":
            kept += 1
            if m.get("clip_path"):
                rendered += 1
    rec["kept"] = kept
    rec["kept_rendered"] = rendered
    rec["replay"] = is_replay(rec.get("path") or "")
    rec["can_trash"] = (not rec.get("keep_whole")) and (not rec["replay"]) and rendered == kept
    rec["pending_render"] = kept - rendered
    return rec


# ── windows and names ─────────────────────────────────────────────────

def moment_window(moment: dict, duration_s: float, pre_s: float, post_s: float,
                  max_len: float) -> Tuple[float, float]:
    """Never tighter than the trim defaults, wider when the detector's own
    merged window is wider, longer tails for bigger streaks."""
    pre = max(float(moment.get("pre_s") or 0), float(pre_s))
    post = max(float(moment.get("post_s") or 0), float(post_s))
    tier = int(moment.get("tier") or 1)
    if tier >= 2:
        post += TIER_BONUS_S * (tier - 1)
    return clips_mod.clip_window(float(moment["ts_s"]), None, float(duration_s or 0),
                                 pre, post, max_len=max_len)


def clip_filename(stem: str, ts_s: float, tier: int) -> str:
    """'Atlas-16_12m34s_double-kill.mp4' — sortable by recording then time,
    readable in Resolve's media pool."""
    s = int(max(0.0, ts_s))
    label = _SLUG_RE.sub("-", tier_label(int(tier or 1)).lower()).strip("-")
    safe_stem = re.sub(r"[\\/:*?\"<>|]+", "_", stem or "clip")
    return f"{safe_stem}_{s // 60:02d}m{s % 60:02d}s_{label}.mp4"


def plan_jobs(rec: dict, clips_dir: Path, pre_s: float, post_s: float,
              max_len: float, default_keep_tier: int) -> List[dict]:
    """Render jobs for every kept moment that has no clip yet."""
    jobs = []
    god = rec.get("god") or rec.get("folder") or "unknown"
    for m in rec.get("moments", []):
        if effective_decision(m, default_keep_tier) != "keep" or m.get("clip_path"):
            continue
        start, end = moment_window(m, float(rec.get("duration_s") or 0), pre_s, post_s, max_len)
        out = Path(clips_dir) / god / clip_filename(rec.get("stem") or Path(rec["path"]).stem,
                                                   float(m["ts_s"]), int(m.get("tier") or 1))
        jobs.append({"event_id": int(m["event_id"]), "recording_id": int(rec["id"]),
                     "src": rec["path"], "start": start, "end": end, "out": out,
                     "tier": int(m.get("tier") or 1), "label": m.get("label") or tier_label(1),
                     "ts_s": float(m["ts_s"]), "god": god, "stem": rec.get("stem"),
                     "recorded_at": rec.get("recorded_at"), "recording_path": rec["path"]})
    return jobs


# ── ffmpeg ────────────────────────────────────────────────────────────

def build_trim_cmd(ffmpeg: str, src: Path | str, start_s: float, end_s: float,
                   out: Path | str, encoder: str = "hevc_nvenc", cq: int = 22,
                   maxrate: str = "30M", hwaccel: Optional[str] = None) -> List[str]:
    """Full-resolution, full-frame-rate subclip. Video is re-encoded (the
    cut cannot land on a keyframe otherwise); every audio stream is
    copied untouched so the OBS track layout survives into Resolve."""
    if encoder == "hevc_nvenc":
        venc = ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                "-cq", str(int(cq)), "-b:v", "0", "-maxrate", maxrate, "-bufsize", "60M",
                "-tag:v", "hvc1"]
    elif encoder == "h264_nvenc":
        venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                "-cq", str(int(cq)), "-b:v", "0", "-maxrate", maxrate, "-bufsize", "60M"]
    else:
        venc = ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    hw = ["-hwaccel", hwaccel] if hwaccel else []
    return [
        ffmpeg, "-v", "error", "-y", "-nostdin", *hw,
        "-ss", f"{max(0.0, start_s):.3f}", "-t", f"{max(0.1, end_s - start_s):.3f}",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",
        *venc,
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out),
    ]


def probe_duration(ffprobe: str, path: Path | str) -> float:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0.0


def render_job(job: dict, ffmpeg: str, ffprobe: str, encoder: str, cq: int, maxrate: str,
               runner: Callable[[List[str]], Tuple[int, str]] | None = None,
               timeout_s: float = 900.0) -> dict:
    """Render one job, trying GPU decode + GPU encode, CPU decode + GPU
    encode, then all-CPU. The clip counts only if ffprobe reads back a
    duration within VERIFY_SLACK_S of what was asked. Returns the job
    with `ok`, `size_bytes`, `error`."""
    out = Path(job["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    asked = float(job["end"]) - float(job["start"])

    def _default_runner(cmd: List[str]) -> Tuple[int, str]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            return int(p.returncode), (p.stderr or "")[-400:]
        except subprocess.TimeoutExpired:
            return 124, "ffmpeg timed out"
        except OSError as e:
            return 127, str(e)

    run = runner or _default_runner
    last = ""
    for hw, enc in clips_mod.render_attempts(encoder):
        rc, err = run(build_trim_cmd(ffmpeg, job["src"], job["start"], job["end"], tmp,
                                     enc, cq, maxrate, hwaccel=hw))
        if rc == 0 and tmp.exists() and tmp.stat().st_size > 0:
            got = probe_duration(ffprobe, tmp) if ffprobe else asked
            if got >= asked - VERIFY_SLACK_S:
                tmp.replace(out)
                res = dict(job)
                res.update(ok=True, size_bytes=out.stat().st_size, error=None, encoder=enc)
                return res
            last = f"verify: asked {asked:.1f}s, got {got:.1f}s"
        else:
            last = err or f"ffmpeg exit {rc}"
        try:
            tmp.unlink()
        except OSError:
            pass
    res = dict(job)
    res.update(ok=False, size_bytes=0, error=last or "render failed")
    return res


def record_clip(store: Store, res: dict) -> None:
    store.trim_add_clip(
        res["out"], recording_id=res["recording_id"], event_id=res["event_id"],
        recording_path=res["recording_path"], god=res["god"], stem=res["stem"],
        recorded_at=res["recorded_at"], ts_s=res["ts_s"], start_s=res["start"],
        end_s=res["end"], tier=res["tier"], label=res["label"], size_bytes=res["size_bytes"])


# ── trash ─────────────────────────────────────────────────────────────

def sidecar_for(video: Path) -> Path:
    return video.with_name(video.stem + ".events.json")


def trash_recording(store: Store, rec: dict, trash_dir: Path) -> dict:
    """Move the recording and its sidecar into trash_dir/<folder>/ and drop
    it from the index (its transcript pointed at a file that is gone).
    Refuses unless annotate() said can_trash. Returns bytes moved."""
    if not rec.get("can_trash"):
        raise PermissionError("recording is not ready to trash")
    src = Path(rec["path"])
    if not src.exists():
        raise FileNotFoundError(str(src))
    dest_dir = Path(trash_dir) / (rec.get("folder") or src.parent.name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in (src, sidecar_for(src)):
        if not f.exists():
            continue
        target = dest_dir / f.name
        if target.exists():
            target = dest_dir / f"{f.stem}_{datetime.now():%Y%m%d%H%M%S}{f.suffix}"
        size = f.stat().st_size
        shutil.move(str(f), str(target))
        moved += size
    store.delete_recording(int(rec["id"]))
    return {"recording_id": int(rec["id"]), "moved_bytes": moved, "trash_dir": str(dest_dir)}


def trash_contents(trash_dir: Path) -> Tuple[int, int]:
    """(files, bytes) sitting in the trash."""
    p = Path(trash_dir)
    if not p.is_dir():
        return 0, 0
    files = [f for f in p.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def empty_trash(trash_dir: Path) -> Tuple[int, int]:
    """Permanently delete everything in the trash. Returns (files, bytes)."""
    n, b = trash_contents(trash_dir)
    p = Path(trash_dir)
    if p.is_dir():
        for child in p.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
    return n, b


def clip_totals(store: Store) -> dict:
    rows = store.trim_clips()
    return {"clips": len(rows), "bytes": sum(int(r.get("size_bytes") or 0) for r in rows)}
