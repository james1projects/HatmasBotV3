"""
plugins/cocaster/chatlog.py — local chat log (the bot's first telemetry).

Every chat message the bot sees is appended to data/chat_log.db. Nothing
leaves the PC: the file lives under data/ (gitignored) and only the
co-caster reads it, to summarize the last minute of chat into James's
ear. It also finally answers "what do viewers actually do", which the
2026-09-04 audit could not (no message log existed).

sqlite3 with a lock, opened once by the plugin; every call is a few
milliseconds so it runs inline on the event loop.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,          -- unix seconds
    user       TEXT NOT NULL,          -- lowercase login
    display    TEXT,
    text       TEXT NOT NULL,
    is_command INTEGER DEFAULT 0,
    is_mod     INTEGER DEFAULT 0,
    channel    TEXT DEFAULT 'twitch'
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user);
"""


class ChatLog:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def add(self, user: str, display: Optional[str], text: str,
            is_command: bool = False, is_mod: bool = False,
            ts: Optional[float] = None, channel: str = "twitch") -> int:
        text = (text or "").strip()
        if not text:
            return 0
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO messages (ts, user, display, text, is_command, is_mod, channel)"
                " VALUES (?,?,?,?,?,?,?)",
                (float(ts if ts is not None else time.time()), (user or "").lower(),
                 display or user, text, int(bool(is_command)), int(bool(is_mod)), channel))
            self.conn.commit()
            return int(cur.lastrowid)

    def recent(self, since_ts: Optional[float] = None, limit: int = 200,
               include_commands: bool = False) -> List[Dict]:
        """Messages newer than `since_ts` (or the newest `limit`), oldest first."""
        limit = max(1, min(int(limit), 2000))
        where = []
        params: list = []
        if since_ts is not None:
            where.append("ts > ?")
            params.append(float(since_ts))
        if not include_commands:
            where.append("is_command = 0")
        sql = "SELECT id, ts, user, display, text, is_command, is_mod FROM messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in reversed(rows)]

    def count_since(self, ts: float, include_commands: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM messages WHERE ts > ?"
        if not include_commands:
            sql += " AND is_command = 0"
        with self._lock:
            return int(self.conn.execute(sql, (float(ts),)).fetchone()[0])

    def stats(self) -> Dict:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT user) AS users, MIN(ts) AS first_ts,"
                " MAX(ts) AS last_ts, SUM(is_command) AS commands FROM messages").fetchone()
        return {"messages": int(row["n"] or 0), "users": int(row["users"] or 0),
                "commands": int(row["commands"] or 0),
                "first_ts": row["first_ts"], "last_ts": row["last_ts"]}
