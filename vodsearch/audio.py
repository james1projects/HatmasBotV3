"""
ffmpeg helpers for vodsearch: probe a recording and decode one or more
audio tracks to 16 kHz mono float32 in a single demux pass.

OBS multi-track recordings carry one stream per OBS track (Game / Mic /
Discord / Misc in HatmasBot's setup). Decoding each track with its own
ffmpeg run would re-read the whole 10-15 GB file per track, so
`decode_tracks()` builds one filter graph that downmixes every wanted
track to mono, resamples, and `join`s them into an N-channel raw
stream we split back apart in numpy.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np

SAMPLE_RATE = 16000


@dataclass
class AudioTrack:
    index: int          # 0-based among the file's audio streams (ffmpeg 0:a:N)
    codec: str = ""
    channels: int = 0


@dataclass
class ProbeResult:
    duration_s: float = 0.0
    audio_tracks: List[AudioTrack] = field(default_factory=list)
    video_codec: str = ""
    width: int = 0
    height: int = 0


def probe(path: Path | str, ffprobe: str = "ffprobe") -> ProbeResult:
    """Container duration + audio stream layout via ffprobe JSON."""
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout or "{}")
    res = ProbeResult()
    try:
        res.duration_s = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        res.duration_s = 0.0
    a_idx = 0
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind == "audio":
            res.audio_tracks.append(AudioTrack(
                index=a_idx, codec=s.get("codec_name", ""),
                channels=int(s.get("channels") or 0)))
            a_idx += 1
        elif kind == "video" and not res.video_codec:
            res.video_codec = s.get("codec_name", "")
            res.width = int(s.get("width") or 0)
            res.height = int(s.get("height") or 0)
    return res


def _mono_chain(track: int, label: str, sample_rate: int) -> str:
    # pan= handles mono/stereo alike: average of the first two channels.
    # For mono input c1 doesn't exist, ffmpeg treats that gain as 0 and
    # the result is still c0. aresample after pan so join sees one rate.
    return (f"[0:a:{track}]pan=mono|c0=0.5*c0+0.5*c1,"
            f"aresample={sample_rate}[{label}]")


def decode_tracks(path: Path | str, tracks: Sequence[int],
                  ffmpeg: str = "ffmpeg",
                  sample_rate: int = SAMPLE_RATE) -> Dict[int, np.ndarray]:
    """Decode the given audio tracks (0-based) to float32 mono arrays at
    `sample_rate`, in one ffmpeg pass. Returns {track_index: samples}.
    Raises RuntimeError if ffmpeg fails."""
    tracks = list(dict.fromkeys(int(t) for t in tracks))
    if not tracks:
        return {}
    labels = [f"t{i}" for i in range(len(tracks))]
    chains = [_mono_chain(t, lab, sample_rate) for t, lab in zip(tracks, labels)]
    if len(tracks) == 1:
        graph = ";".join(chains)
        out_label = labels[0]
    else:
        layout = {2: "stereo", 3: "3.0", 4: "4.0", 5: "5.0", 6: "6.0"}.get(
            len(tracks), f"{len(tracks)}c")
        graph = (";".join(chains) + ";"
                 + "".join(f"[{lab}]" for lab in labels)
                 + f"join=inputs={len(tracks)}:channel_layout={layout}[out]")
        out_label = "out"
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", str(path),
           "-filter_complex", graph, "-map", f"[{out_label}]",
           "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[:400]
        raise RuntimeError(f"ffmpeg decode failed for {path}: {err}")
    raw = np.frombuffer(proc.stdout, dtype=np.float32)
    n = len(tracks)
    if n > 1:
        raw = raw[: (len(raw) // n) * n].reshape(-1, n)
        return {t: np.ascontiguousarray(raw[:, i]) for i, t in enumerate(tracks)}
    return {tracks[0]: raw}


def rms_db(samples: np.ndarray) -> float:
    """Overall RMS level in dBFS; -120 for silence/empty."""
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    if rms <= 1e-6:
        return -120.0
    return 20.0 * math.log10(rms)


def peak_db(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    peak = float(np.max(np.abs(samples)))
    if peak <= 1e-6:
        return -120.0
    return 20.0 * math.log10(peak)


def speech_fraction(samples: np.ndarray, sample_rate: int = SAMPLE_RATE,
                    window_s: float = 0.5, threshold_db: float = -45.0) -> float:
    """Fraction of `window_s` windows whose RMS exceeds `threshold_db`.
    A cheap "is there anything on this track" gate that survives a
    single loud pop (unlike peak) and long silences (unlike overall RMS)."""
    if samples.size == 0:
        return 0.0
    win = max(1, int(window_s * sample_rate))
    n = samples.size // win
    if n == 0:
        return 1.0 if rms_db(samples) > threshold_db else 0.0
    chunks = samples[: n * win].reshape(n, win).astype(np.float64)
    rms = np.sqrt(np.mean(np.square(chunks), axis=1))
    thr = 10 ** (threshold_db / 20.0)
    return float(np.mean(rms > thr))


def same_audio(a: np.ndarray, b: np.ndarray, stride: int = 97, atol: float = 1e-4) -> bool:
    """True when two decoded tracks are (near) byte-identical: same
    length and every strided sample within `atol`. Cheap enough to run
    on every track pair per recording."""
    if a.shape != b.shape or a.size == 0:
        return False
    return bool(np.allclose(a[::stride], b[::stride], atol=atol, rtol=0.0))
