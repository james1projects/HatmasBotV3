"""
Windows CUDA runtime preload for ctranslate2 / faster-whisper.

`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` drops the DLLs under
site-packages/nvidia/<lib>/bin, but ctranslate2 resolves them with a
plain LoadLibrary at first use, which only honours PATH, not
`os.add_dll_directory`. Verified 2026-09-04: without this shim
`WhisperModel(...).transcribe()` dies with "Library cublas64_12.dll is
not found" even though the DLL is installed. Call `preload_cuda_dlls()`
before importing faster_whisper. No-op off Windows.
"""

from __future__ import annotations

import ctypes
import glob
import os
import site
import sys

_DONE = False
_PRELOAD = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll")


def _candidate_dirs() -> list[str]:
    roots: list[str] = []
    try:
        roots.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    roots.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    dirs: list[str] = []
    for root in dict.fromkeys(roots):
        dirs.extend(glob.glob(os.path.join(root, "nvidia", "*", "bin")))
    return list(dict.fromkeys(dirs))


def preload_cuda_dlls() -> list[str]:
    """Prepend the pip-installed NVIDIA bin dirs to PATH and preload the
    libraries ctranslate2 needs. Returns the directories used (empty
    when nothing was found or not on Windows). Idempotent."""
    global _DONE
    if _DONE or sys.platform != "win32":
        return []
    _DONE = True
    dirs = _candidate_dirs()
    if not dirs:
        return []
    os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
    for name in _PRELOAD:
        for d in dirs:
            f = os.path.join(d, name)
            if os.path.exists(f):
                try:
                    ctypes.CDLL(f)
                except OSError:
                    pass
                break
    return dirs
