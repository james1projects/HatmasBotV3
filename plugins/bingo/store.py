"""
plugins/bingo/store.py — rounds, cards, and calls in data/bingo.db.

Synchronous sqlite3 behind a lock (same pattern as the co-caster's chat
log): every operation is a few milliseconds, so it runs inline on the
event loop; the public server also touches it from request handlers.

Tables
  rounds  one per bingo round (a stream, usually): status open|closed,
          base prize, Hats collected from card sales, the winner.
  cards   one per viewer card: 25 square ids, the called-square marks,
          price paid, and when it hit bingo.
  calls   every square call in a round, with its source.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .pool import FREE, has_bingo, marked_indexes, squares_to_bingo

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id           INTEGER PRIMARY KEY,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    status       TEXT NOT NULL DEFAULT 'open',   -- open | closed
    base_prize   INTEGER NOT NULL DEFAULT 0,
    sales        INTEGER NOT NULL DEFAULT 0,     -- Hats spent on extra cards
    pot_share    REAL NOT NULL DEFAULT 0.5,      -- fraction of sales added to the pot
    winner_login TEXT,
    winner_name  TEXT,
    winner_card  INTEGER,
    prize_paid   INTEGER,
    prize_ok     INTEGER
);
CREATE TABLE IF NOT EXISTS cards (
    id         INTEGER PRIMARY KEY,
    round_id   INTEGER NOT NULL,
    login      TEXT NOT NULL,
    display    TEXT,
    seq        INTEGER NOT NULL,                 -- 1 = free card, 2.. = bought
    price      INTEGER NOT NULL DEFAULT 0,
    squares    TEXT NOT NULL,                    -- JSON list of 25 ids
    marks      TEXT NOT NULL DEFAULT '[]',       -- JSON list of marked indexes
    created_at REAL NOT NULL,
    bingo_at   REAL,
    UNIQUE (round_id, login, seq)
);
CREATE INDEX IF NOT EXISTS idx_cards_round ON cards(round_id);
CREATE TABLE IF NOT EXISTS calls (
    id        INTEGER PRIMARY KEY,
    round_id  INTEGER NOT NULL,
    event_id  TEXT NOT NULL,
    label     TEXT,
    source    TEXT,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_round ON calls(round_id);
"""


class BingoStore:
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

    # ── rounds ────────────────────────────────────────────────────────

    def current_round(self) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def get_round(self, round_id: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM rounds WHERE id=?", (int(round_id),)).fetchone()
        return dict(row) if row else None

    def last_round(self) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def open_round(self, base_prize: int, pot_share: float, now: Optional[float] = None) -> dict:
        """Open a new round; any still-open round is closed without a winner."""
        now = time.time() if now is None else now
        with self._lock:
            self.conn.execute("UPDATE rounds SET status='closed', ended_at=? WHERE status='open'", (now,))
            cur = self.conn.execute(
                "INSERT INTO rounds (started_at, status, base_prize, sales, pot_share) VALUES (?,?,?,?,?)",
                (now, "open", int(base_prize), 0, float(pot_share)))
            self.conn.commit()
            rid = int(cur.lastrowid)
        return self.get_round(rid)

    def close_round(self, round_id: int, winner: Optional[dict] = None, prize_paid: Optional[int] = None,
                    prize_ok: Optional[bool] = None, now: Optional[float] = None) -> Optional[dict]:
        now = time.time() if now is None else now
        with self._lock:
            self.conn.execute(
                "UPDATE rounds SET status='closed', ended_at=?, winner_login=?, winner_name=?, winner_card=?,"
                " prize_paid=?, prize_ok=? WHERE id=?",
                (now, (winner or {}).get("login"), (winner or {}).get("display"),
                 (winner or {}).get("card_id"), prize_paid,
                 None if prize_ok is None else int(bool(prize_ok)), int(round_id)))
            self.conn.commit()
        return self.get_round(round_id)

    def pot(self, round_id: int) -> int:
        r = self.get_round(round_id)
        if not r:
            return 0
        return int(r["base_prize"]) + int(round(int(r["sales"]) * float(r["pot_share"])))

    # ── cards ─────────────────────────────────────────────────────────

    def cards_for(self, round_id: int, login: str) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM cards WHERE round_id=? AND login=? ORDER BY seq",
                (int(round_id), login.lower())).fetchall()
        return [self._card(r) for r in rows]

    def all_cards(self, round_id: int) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM cards WHERE round_id=? ORDER BY id", (int(round_id),)).fetchall()
        return [self._card(r) for r in rows]

    def add_card(self, round_id: int, login: str, display: str, seq: int, price: int,
                 squares: Sequence[str], called: Iterable[str], now: Optional[float] = None) -> dict:
        """Insert a card; squares already called this round start marked
        (a late joiner is not punished for the round's history)."""
        now = time.time() if now is None else now
        marks = sorted(marked_indexes(list(squares), called))
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO cards (round_id, login, display, seq, price, squares, marks, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (int(round_id), login.lower(), display or login, int(seq), int(price),
                 json.dumps(list(squares)), json.dumps(marks), now))
            if price > 0:
                self.conn.execute("UPDATE rounds SET sales = sales + ? WHERE id=?", (int(price), int(round_id)))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM cards WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._card(row)

    def card_count(self, round_id: int) -> Tuple[int, int]:
        """(cards, distinct players)"""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT login) FROM cards WHERE round_id=?", (int(round_id),)).fetchone()
        return int(row[0]), int(row[1])

    @staticmethod
    def _card(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["squares"] = json.loads(d["squares"])
        d["marks"] = json.loads(d["marks"] or "[]")
        d["to_bingo"] = squares_to_bingo(set(d["marks"]))
        return d

    # ── calls / marking ───────────────────────────────────────────────

    def calls(self, round_id: int) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM calls WHERE round_id=? ORDER BY id", (int(round_id),)).fetchall()
        return [dict(r) for r in rows]

    def called_ids(self, round_id: int) -> List[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT event_id FROM calls WHERE round_id=?", (int(round_id),)).fetchall()
        return [r[0] for r in rows]

    def mark_event(self, round_id: int, event_id: str, label: str, source: str,
                   now: Optional[float] = None) -> dict:
        """Record a call and mark it on every card in the round.
        -> {"already": bool, "changed": [card...], "winners": [card...]}
        A square called twice in a round is a no-op the second time."""
        now = time.time() if now is None else now
        result = {"already": False, "changed": [], "winners": []}
        with self._lock:
            already = self.conn.execute(
                "SELECT 1 FROM calls WHERE round_id=? AND event_id=? LIMIT 1",
                (int(round_id), event_id)).fetchone()
            self.conn.execute(
                "INSERT INTO calls (round_id, event_id, label, source, ts) VALUES (?,?,?,?,?)",
                (int(round_id), event_id, label, source, now))
            if already:
                self.conn.commit()
                result["already"] = True
                return result
            rows = self.conn.execute(
                "SELECT * FROM cards WHERE round_id=? AND bingo_at IS NULL ORDER BY id",
                (int(round_id),)).fetchall()
            for row in rows:
                squares = json.loads(row["squares"])
                marks: Set[int] = set(json.loads(row["marks"] or "[]"))
                hits = {i for i, sid in enumerate(squares) if sid == event_id}
                if not hits or hits <= marks:
                    continue
                marks |= hits
                bingo = has_bingo(marks)
                self.conn.execute("UPDATE cards SET marks=?, bingo_at=? WHERE id=?",
                                  (json.dumps(sorted(marks)), now if bingo else None, row["id"]))
                card = self._card(self.conn.execute("SELECT * FROM cards WHERE id=?", (row["id"],)).fetchone())
                result["changed"].append(card)
                if bingo:
                    result["winners"].append(card)
            self.conn.commit()
        return result

    def uncall(self, round_id: int, event_id: str) -> dict:
        """Undo a call (a mis-pressed deck key): delete its rows and rebuild
        every card's marks from the calls that remain.
        -> {"removed": n_call_rows, "changed": [card...]}"""
        result = {"removed": 0, "changed": []}
        with self._lock:
            cur = self.conn.execute("DELETE FROM calls WHERE round_id=? AND event_id=?",
                                    (int(round_id), event_id))
            result["removed"] = int(cur.rowcount or 0)
            if not result["removed"]:
                self.conn.commit()
                return result
            called = {r[0] for r in self.conn.execute(
                "SELECT DISTINCT event_id FROM calls WHERE round_id=?", (int(round_id),)).fetchall()}
            rows = self.conn.execute("SELECT * FROM cards WHERE round_id=? ORDER BY id", (int(round_id),)).fetchall()
            for row in rows:
                squares = json.loads(row["squares"])
                old: Set[int] = set(json.loads(row["marks"] or "[]"))
                new = marked_indexes(squares, called)
                if new == old:
                    continue
                self.conn.execute("UPDATE cards SET marks=?, bingo_at=? WHERE id=?",
                                  (json.dumps(sorted(new)), row["bingo_at"] if has_bingo(new) else None, row["id"]))
                result["changed"].append(self._card(
                    self.conn.execute("SELECT * FROM cards WHERE id=?", (row["id"],)).fetchone()))
            self.conn.commit()
        return result

    # ── summaries ─────────────────────────────────────────────────────

    def rounds(self, limit: int = 20) -> List[dict]:
        """Most recent rounds first (open or closed)."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def card_list(self, round_id: int) -> List[dict]:
        """Compact view of every card in a round for the control page."""
        return [{"id": c["id"], "login": c["login"], "display": c["display"], "seq": c["seq"],
                 "price": c["price"], "marked": len(c["marks"]), "to_bingo": c["to_bingo"],
                 "bingo": c["bingo_at"] is not None, "created_at": c["created_at"]}
                for c in self.all_cards(round_id)]

    def leaders(self, round_id: int, limit: int = 5) -> List[dict]:
        """Closest cards to bingo (fewest squares missing), one per player."""
        best: Dict[str, dict] = {}
        for c in self.all_cards(round_id):
            cur = best.get(c["login"])
            if cur is None or c["to_bingo"] < cur["to_bingo"]:
                best[c["login"]] = c
        rows = sorted(best.values(), key=lambda c: (c["to_bingo"], c["created_at"]))[:limit]
        return [{"login": c["login"], "display": c["display"], "to_bingo": c["to_bingo"],
                 "cards": len(self.cards_for(round_id, c["login"]))} for c in rows]

    def summary(self, round_id: int) -> dict:
        r = self.get_round(round_id) or {}
        cards, players = self.card_count(round_id)
        calls = self.calls(round_id)
        return {
            "round_id": int(round_id),
            "status": r.get("status"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
            "cards": cards,
            "players": players,
            "pot": self.pot(round_id),
            "base_prize": int(r.get("base_prize") or 0),
            "sales": int(r.get("sales") or 0),
            "calls": [{"event_id": c["event_id"], "label": c["label"], "source": c["source"], "ts": c["ts"]}
                      for c in calls],
            "called_ids": sorted({c["event_id"] for c in calls}),
            "leaders": self.leaders(round_id),
            "winner": {"login": r.get("winner_login"), "display": r.get("winner_name"),
                       "prize": r.get("prize_paid")} if r.get("winner_login") else None,
        }
