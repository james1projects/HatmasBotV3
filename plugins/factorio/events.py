"""
Outbox tailer: follows the Factorio mod's JSONL event file
(<script-output>/hatmas/events.jsonl) and dispatches parsed events.

The mod appends one JSON object per line via helpers.write_file. We
poll the file (1s), remember our byte offset, and start at end-of-file
on boot so old events are never replayed into chat. If the file
shrinks (deleted/rotated), the offset resets to 0.
"""

import asyncio
import json
from pathlib import Path
from typing import Callable, List, Optional

POLL_INTERVAL = 1.0


class OutboxTailer:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._offset: Optional[int] = None   # None = not yet anchored
        self._listeners: List[Callable] = []
        self._task: Optional[asyncio.Task] = None
        self.events_seen = 0

    def add_listener(self, callback):
        """callback: async def cb(event: dict)"""
        self._listeners.append(callback)

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self):
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[Factorio] Outbox tailer error: {e}")
            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                raise
        # not reached

    async def _poll_once(self):
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            # File appears on the mod's first event. Anchor at 0 so
            # the first events written after boot are picked up.
            self._offset = 0
            return
        if self._offset is None:
            # First sighting of an existing file: skip history.
            self._offset = size
            return
        if size < self._offset:
            self._offset = 0          # truncated/rotated
        if size == self._offset:
            return
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self._offset)
            chunk = fh.read()
            # Only consume complete lines; a partially-written line
            # stays unconsumed until the next poll.
            consumed = chunk.rfind("\n") + 1
            if consumed == 0:
                return
            self._offset += len(chunk[:consumed].encode("utf-8"))
            lines = chunk[:consumed].splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                print(f"[Factorio] Unparseable outbox line skipped "
                      f"({len(line)} chars)")
                continue
            self.events_seen += 1
            await self._dispatch(event)

    async def _dispatch(self, event):
        for cb in self._listeners:
            try:
                await cb(event)
            except Exception as e:
                print(f"[Factorio] Outbox listener error "
                      f"({getattr(cb, '__qualname__', cb)}): {e}")
