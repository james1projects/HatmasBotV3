"""
Download one Twitch VOD (archive) by id with yt-dlp into a channel folder.
One download at a time by design; yt-dlp resumes .part files so an interrupted download continues on the next run.
The sidecar carries Helix metadata so the indexer can date the recording.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

GB_PER_HOUR = 5.0   # rough size of a 1080p60 Twitch archive per hour (a 3 h source VOD was ~16 GB)

class DownloadError(Exception):
    kind: str   # one of "sub_only", "unavailable", "network", "other"
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)

def classify_error(msg: str) -> str:
    m = msg.lower()
    if any(k in m for k in ("subscri", "sub-only", "subscriber-only")):
        return "sub_only"
    # network first: "HTTP Error 503: Service Unavailable" must not read as a missing VOD
    if any(k in m for k in ("http error 5", "timed out", "timeout", "connection", "temporarily", "unable to download", "eof")):
        return "network"
    if any(k in m for k in ("does not exist", "404", "unavailable", "removed", "not found", "private video")):
        return "unavailable"
    return "other"

def build_ydl_opts(
    inbox: Path,
    vod_id: str,
    quality: str,
    fragments: int,
    progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    return {
        "format": quality,
        "outtmpl": str(inbox / f"v{vod_id}.%(ext)s"),
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": max(1, int(fragments)),
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": False,
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "progress_hooks": [progress] if progress else [],
        # No explicit remux postprocessor: yt-dlp's own FixupM3u8 already
        # rewrites an HLS download into a clean .mp4 when needed, and a
        # forced FFmpegVideoRemuxer copied a finished 8.7 GB file a second
        # time (seen 2026-09-06).
        "postprocessors": [],
    }

class ProgressPrinter:
    def __init__(self, vod_id: str, log: Callable[[str], None] = print, every_s: float = 5.0) -> None:
        self.vod_id = vod_id
        self.log = log
        self.every_s = every_s
        self.last_print_time: float = 0.0

    def __call__(self, d: dict) -> None:
        status = d.get("status")
        if status == "finished":
            self.log(f"v{self.vod_id} download finished")
            return
        if status != "downloading":
            return

        now = time.time()
        if now - self.last_print_time < self.every_s:
            return
        self.last_print_time = now

        done_bytes = d.get("downloaded_bytes", 0) or 0
        total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate", 0) or 0
        speed = d.get("speed", 0) or 0

        pct = (done_bytes / total_bytes * 100) if total_bytes > 0 else -1.0
        done_gb = done_bytes / 1e9
        total_gb = total_bytes / 1e9
        mb_s = speed / 1e6

        pct_str = f"{pct:5.1f}" if pct >= 0 else "   ?"
        done_str = f"{done_gb:.2f}" if done_bytes > 0 else "?"
        total_str = f"{total_gb:.2f}" if total_bytes > 0 else "?"
        speed_str = f"{mb_s:.1f}" if speed > 0 else "?"

        self.log(f"v{self.vod_id} {pct_str}% {done_str}/{total_str} GB {speed_str} MB/s")

def download_vod(
    vod_id: str,
    inbox: Path,
    dest_root: Path,
    quality: str,
    fragments: int,
    log: Callable[[str], None] = print,
) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    dest_root.mkdir(parents=True, exist_ok=True)

    try:
        import yt_dlp
    except ImportError:
        raise DownloadError("other", "yt-dlp is not installed (pip install yt-dlp)")

    opts = build_ydl_opts(inbox, vod_id, quality, fragments, ProgressPrinter(vod_id, log))
    url = f"https://www.twitch.tv/videos/{vod_id}"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except KeyboardInterrupt:
        raise
    except Exception as e:
        raise DownloadError(classify_error(str(e)), str(e)[:500]) from e

    located = inbox / f"v{vod_id}.mp4"
    if not located.exists():
        candidates = [f for f in inbox.glob(f"v{vod_id}.*") if f.suffix.lower() not in {".part", ".ytdl", ".json"}]
        if candidates:
            located = candidates[0]
        else:
            raise DownloadError("other", "download produced no file")

    if located.suffix.lower() != ".mp4":
        raise DownloadError("other", f"unexpected output {located.name}")

    final = dest_root / f"v{vod_id}.mp4"
    os.replace(located, final)
    return final

def write_twitch_sidecar(mp4: Path, meta: dict) -> Path:
    sidecar = mp4.with_name(mp4.stem + ".twitch.json")
    fd, tmp_path = tempfile.mkstemp(dir=mp4.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(sidecar))
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return sidecar

def twitch_sidecar_path(mp4: Path) -> Path:
    return mp4.with_name(mp4.stem + ".twitch.json")

def free_gb(path: Path) -> float:
    p = path.resolve()
    while not p.exists():
        p = p.parent
    usage = shutil.disk_usage(p)
    return usage.free / 1e9

def estimate_gb(duration_s: float) -> float:
    return duration_s / 3600 * GB_PER_HOUR
