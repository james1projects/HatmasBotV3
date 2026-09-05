"""
Clip rendering for vodsearch.

Source recordings are 4K-ish HEVC at ~80 Mbps with four separate audio
tracks. Browsers can't reliably play HEVC and nobody wants a 15 GB
file behind a search result, so a clip is always a fresh render:
H.264 720p (NVENC when available, libx264 fallback), yuv420p, faststart,
with the chosen audio tracks mixed down to one stereo AAC stream.

`-ss` goes BEFORE `-i` (fast keyframe seek); because we re-encode,
ffmpeg still trims frame-accurately from the requested start.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

DEFAULT_PRE_S = 8.0
DEFAULT_POST_S = 12.0
MAX_CLIP_S = 90.0
MIN_CLIP_S = 3.0


def clip_window(start_s: float, end_s: Optional[float], duration_s: float,
                pre_s: float = DEFAULT_PRE_S, post_s: float = DEFAULT_POST_S,
                max_len: float = MAX_CLIP_S) -> Tuple[float, float]:
    """Compute a clamped [start, end] window. When `end_s` is None the
    window is `start_s - pre .. start_s + post` (a moment); otherwise the
    given span is padded by pre/post. Never longer than `max_len`, never
    outside the recording."""
    if end_s is None or end_s <= start_s:
        a, b = start_s - pre_s, start_s + post_s
    else:
        a, b = start_s - pre_s, end_s + post_s
    a = max(0.0, a)
    if duration_s and duration_s > 0:
        b = min(b, duration_s)
    if b - a > max_len:
        b = a + max_len
    if b - a < MIN_CLIP_S:
        b = a + MIN_CLIP_S
        if duration_s and b > duration_s:
            b = duration_s
            a = max(0.0, b - MIN_CLIP_S)
    return round(a, 2), round(b, 2)


def clip_name(recording_id: int, start_s: float, end_s: float, height: int = 720,
              audio_tracks: Sequence[int] = ()) -> str:
    tracks = "".join(str(t) for t in audio_tracks) or "x"
    return f"r{int(recording_id)}_{int(round(start_s * 10))}_{int(round(end_s * 10))}_{height}p_a{tracks}.mp4"


def _audio_args(audio_tracks: Sequence[int]) -> Tuple[List[str], List[str]]:
    """Return (filter_complex args, map args) for the wanted tracks."""
    tracks = list(dict.fromkeys(int(t) for t in audio_tracks))
    if not tracks:
        return [], ["-an"]
    if len(tracks) == 1:
        return [], ["-map", f"0:a:{tracks[0]}"]
    inputs = "".join(f"[0:a:{t}]" for t in tracks)
    graph = f"{inputs}amix=inputs={len(tracks)}:duration=longest:normalize=0[aout]"
    return ["-filter_complex", graph], ["-map", "[aout]"]


def build_clip_cmd(ffmpeg: str, src: Path | str, start_s: float, end_s: float,
                   out: Path | str, audio_tracks: Sequence[int] = (0, 1),
                   height: int = 720, encoder: str = "h264_nvenc",
                   audio_bitrate: str = "160k", hwaccel: Optional[str] = None) -> List[str]:
    """hwaccel="cuda" decodes the HEVC source on the GPU (ffmpeg copies
    frames back for the software scale filter by itself). Software
    decode of a 4K 80 Mbps source is the slow part of a render, so this
    is the difference between 3x and 10x+ realtime."""
    fc, amap = _audio_args(audio_tracks)
    if encoder == "h264_nvenc":
        venc = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "26",
                "-b:v", "0", "-maxrate", "5M", "-bufsize", "10M"]
    else:
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24"]
    hw = ["-hwaccel", hwaccel] if hwaccel else []
    return [
        ffmpeg, "-v", "error", "-y", "-nostdin", *hw,
        "-ss", f"{start_s:.3f}", "-t", f"{max(0.1, end_s - start_s):.3f}",
        "-i", str(src),
        *fc,
        "-map", "0:v:0", *amap,
        "-vf", f"scale=-2:{int(height)},format=yuv420p",
        *venc,
        "-c:a", "aac", "-b:a", audio_bitrate, "-ac", "2",
        "-movflags", "+faststart",
        str(out),
    ]


def build_stream_cmd(ffmpeg: str, src: Path | str, start_s: float, length_s: float,
                     audio_tracks: Sequence[int] = (0, 1), height: int = 720,
                     encoder: str = "h264_nvenc", audio_bitrate: str = "160k",
                     hwaccel: Optional[str] = None) -> List[str]:
    """Like build_clip_cmd but writes a fragmented MP4 to stdout so a
    browser can start playing while ffmpeg is still transcoding: "watch
    the full recording from this moment" with no render wait. Fragmented
    output has no known duration, so seeking is limited; that's the
    trade for instant start."""
    fc, amap = _audio_args(audio_tracks)
    if encoder == "h264_nvenc":
        venc = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "26",
                "-b:v", "0", "-maxrate", "5M", "-bufsize", "10M"]
    else:
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-tune", "zerolatency"]
    hw = ["-hwaccel", hwaccel] if hwaccel else []
    return [
        ffmpeg, "-v", "error", "-nostdin", *hw,
        "-ss", f"{max(0.0, start_s):.3f}", "-t", f"{max(1.0, length_s):.3f}",
        "-i", str(src),
        *fc,
        "-map", "0:v:0", *amap,
        "-vf", f"scale=-2:{int(height)},format=yuv420p",
        *venc, "-g", "60",
        "-c:a", "aac", "-b:a", audio_bitrate, "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]


def render_attempts(encoder: str = "h264_nvenc") -> List[Tuple[Optional[str], str]]:
    """(hwaccel, encoder) pairs to try in order: GPU decode + GPU encode,
    then CPU decode + GPU encode, then all-CPU."""
    attempts: List[Tuple[Optional[str], str]] = []
    if encoder != "libx264":
        attempts.append(("cuda", encoder))
        attempts.append((None, encoder))
    attempts.append((None, "libx264"))
    return attempts


def render_clip(ffmpeg: str, src: Path | str, start_s: float, end_s: float,
                out: Path | str, audio_tracks: Sequence[int] = (0, 1),
                height: int = 720, encoder: str = "h264_nvenc",
                timeout_s: float = 300.0) -> Path:
    """Synchronous render with automatic libx264 fallback if the GPU
    encoder is unavailable. Writes to a temp name and renames on success
    so a half-written file never gets served."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp4")
    last_err = ""
    for hw, enc in render_attempts(encoder):
        cmd = build_clip_cmd(ffmpeg, src, start_s, end_s, tmp, audio_tracks, height, enc,
                             hwaccel=hw)
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return out
        last_err = proc.stderr.decode("utf-8", "replace").strip()[:400]
        try:
            tmp.unlink()
        except OSError:
            pass
    raise RuntimeError(f"clip render failed: {last_err}")
