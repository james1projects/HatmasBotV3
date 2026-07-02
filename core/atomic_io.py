"""
core/atomic_io.py — crash-safe JSON state persistence.

Every plugin that persists state to data/*.json used to write the
file in place: `json.dump(obj, open(path, "w"))`. A crash, power
loss, or OS kill mid-write leaves the file truncated, and the next
boot either raises JSONDecodeError or silently resets state (queues,
jackpots, KDA baselines, paid priority requests...).

`atomic_write_json` writes to a `<name>.tmp` sibling first and then
`os.replace()`s it into place — on both Windows (NTFS) and POSIX the
replace is atomic, so readers only ever see the old complete file or
the new complete file, never a half-written one.

Usage (drop-in for the old pattern):

    from core.atomic_io import atomic_write_json
    atomic_write_json(path, obj, indent=2)

Callers keep their own try/except and logging — this module raises
on failure exactly like open()/json.dump() would.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def atomic_write_json(path, obj, *, indent=2, **json_kwargs) -> None:
    """Serialize `obj` as JSON to `path` atomically (tmp + replace)."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent, **json_kwargs)
    os.replace(tmp, path)


def atomic_write_text(path, text: str, *, encoding="utf-8") -> None:
    """Write text to `path` atomically (tmp + replace)."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding=encoding) as f:
        f.write(text)
    os.replace(tmp, path)
