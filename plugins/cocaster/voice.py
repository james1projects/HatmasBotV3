"""
plugins/cocaster/voice.py — fully local text-to-speech on a chosen output.

Windows' built-in SAPI voices (System.Speech via PowerShell, ~0.4 s per
line, no network) render to a WAV, which is then played through a
specific output device via sounddevice. That device choice is the whole
point: "Headphones (Elgato XLR Dock)" is James's ear and never reaches
the stream mix, while a Wave Link virtual input like "SFX" would.

Synthesis and playback are synchronous; the plugin runs them in a
thread (asyncio.to_thread). Lines are serialized by a lock so two never
overlap.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

_PS = "powershell"
_SAPI_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { if ('{VOICE}' -ne '') { $s.SelectVoice('{VOICE}') } } catch {}
$s.Rate = {RATE}
$s.SetOutputToWaveFile('{OUT}')
$text = Get-Content -Raw -Encoding UTF8 '{TXT}'
$s.Speak($text)
$s.Dispose()
"""


def wav_to_array(path: Path | str) -> Tuple[np.ndarray, int]:
    """Read a PCM WAV (8/16/32-bit) into float32 [-1, 1], shape (n, channels)."""
    with wave.open(str(path), "rb") as w:
        ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    if ch > 1:
        data = data.reshape(-1, ch)
    else:
        data = data.reshape(-1, 1)
    return data, sr


def find_output_device(substring: str, devices=None) -> Optional[int]:
    """Index of the first output device whose name contains `substring`
    (case-insensitive), preferring the first host API (MME on Windows,
    which lists every device once). None = use the system default."""
    if not substring:
        return None
    if devices is None:
        try:
            import sounddevice as sd
            devices = list(sd.query_devices())
        except Exception:
            return None
    needle = substring.lower()
    best = None
    for i, d in enumerate(devices):
        try:
            if int(d.get("max_output_channels", 0)) <= 0:
                continue
            if needle not in str(d.get("name", "")).lower():
                continue
        except AttributeError:
            continue
        api = int(d.get("hostapi", 0))
        if best is None or api < best[0]:
            best = (api, i)
    return best[1] if best else None


def synthesize_sapi(text: str, out_path: Path | str, voice: str = "",
                    rate: int = 0, timeout_s: float = 20.0) -> Path:
    """Render `text` to a WAV with Windows SAPI. Text travels through a
    temp file so quoting never matters."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8",
                                     dir=str(out_path.parent)) as tf:
        tf.write(text)
        txt_path = tf.name
    script = (_SAPI_SCRIPT.replace("{VOICE}", voice.replace("'", "''"))
              .replace("{RATE}", str(int(max(-10, min(10, rate)))))
              .replace("{OUT}", str(out_path).replace("'", "''"))
              .replace("{TXT}", txt_path.replace("'", "''")))
    try:
        proc = subprocess.run([_PS, "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, timeout=timeout_s,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0 or not out_path.exists():
            err = proc.stderr.decode("utf-8", "replace").strip()[-300:]
            raise RuntimeError(f"SAPI synthesis failed: {err}")
    finally:
        try:
            Path(txt_path).unlink()
        except OSError:
            pass
    return out_path


def play_array(data: np.ndarray, sample_rate: int, device: Optional[int] = None) -> None:
    import sounddevice as sd
    sd.play(data, sample_rate, device=device, blocking=True)


class Voice:
    """One output channel: a device, a SAPI voice, a rate, and a lock."""

    def __init__(self, device_substring: str = "", voice_name: str = "", rate: int = 0,
                 work_dir: Path | str = ".", enabled: bool = True, label: str = "voice"):
        self.device_substring = device_substring
        self.voice_name = voice_name
        self.rate = rate
        self.work_dir = Path(work_dir)
        self.enabled = enabled
        self.label = label
        self._lock = threading.Lock()
        self._device: Optional[int] = None
        self._device_resolved = False
        self.last_spoken_at = 0.0
        self.last_text = ""
        self.speaking = False

    def resolve_device(self) -> Optional[int]:
        if not self._device_resolved:
            self._device = find_output_device(self.device_substring)
            self._device_resolved = True
        return self._device

    def speak(self, text: str) -> bool:
        """Blocking: synthesize + play. Returns False when disabled or on
        any failure (logged by the caller). Safe to call from a thread."""
        text = (text or "").strip()
        if not text or not self.enabled:
            return False
        with self._lock:
            self.speaking = True
            try:
                wav = self.work_dir / f"{self.label}_{int(time.time() * 1000)}.wav"
                synthesize_sapi(text, wav, self.voice_name, self.rate)
                data, sr = wav_to_array(wav)
                play_array(data, sr, self.resolve_device())
                self.last_spoken_at = time.time()
                self.last_text = text
                try:
                    wav.unlink()
                except OSError:
                    pass
                return True
            finally:
                self.speaking = False
