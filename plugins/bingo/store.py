"""
plugins/bingo/store.py — rounds, cards, and calls in data/bingo.db.

Synchronous sqlite3 behind a lock (same pattern as the co-caster's chat
log): every operation is a few milliseconds, so it runs inline on the
event loop; the public server also touches it from request handlers.

Viewers are `user_uuid`s (core/users.py); `login` / `display` on a
card are display text captured when it was claimed.

Tables
  rounds  one per bingo round (a stream, usually): status open|closed,
          base prize, Hats collected from card sales, the winner.
  cards   one per viewer card: 25 square ids, the called-square marks,
          price paid, and when it hit bingo.
  calls   every square call in a round, with its source.
  prefs   per viewer: "Show my card on stream" (kept across rounds).

A completed line does NOT win by itself: `mark_event` records it
(`bingo_at`), the viewer presses Bingo! on the site, and `claim`
re-checks the line against the round's calls before the plugin pays.
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
    winner_uuid  TEXT,
    winner_login TEXT,
    winner_name  TEXT,
    winner_card  INTEGER,
    prize_paid   INTEGER,
    prize_ok     INTEGER
);
CREATE TABLE IF NOT EXISTS cards (
    id         INTEGER PRIMARY KEY,
    round_id   INTEGER NOT NULL,
    user_uuid  TEXT NOT NULL,
    login      TEXT,
    display    TEXT,
    seq        INTEGER NOT NULL,                 -- 1 = free card, 2.. = bought
    price      INTEGER NOT NULL DEFAULT 0,
    squares    TEXT NOT NULL,                    -- JSON list of 25 ids
    marks      TEXT NOT NULL DEFAULT '[]',       -- JSON list of marked indexes
    created_at REAL NOT NULL,
    bingo_at   REAL,
    UNIQUE (round_id, user_uuid, seq)
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
CREATE TABLE IF NOT EXISTS prefs (
    user_uuid  TEXT PRIMARY KEY,
    on_stream  INTEGER NOT NULL DEFAULT 0,          -- "Show my card on stream"
    updated_at REAL
);
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
        self._migrate_login_era()
        self.conn.executescript(SCHEMA)

    def _migrate_login_era(self) -> None:
        """Tables from before user_uuid (keyed on the Twitch login) are
        parked as _legacy_* and recreated; login-era cards belong to
        rounds that are long closed, and prefs are re-set by the viewer
        with one click."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(cards)")}
        if cols and "user_uuid" not in cols:
            for t in ("rounds", "cards", "calls", "prefs"):
                self.conn.execute(f"DROP TABLE IF EXISTS _legacy_{t}")
                self.conn.execute(f"ALTER TABLE {t} RENAME TO _legacy_{t}")
            self.conn.commit()
            print("[Bingo] store re-keyed on user_uuid (login-era tables kept as _legacy_*)")

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # ── account merges (core/users.py hook) ────────────────────────────

    def repoint_user(self, absorbed_uuid: str, survivor_uuid: str) -> dict:
        """Move the absorbed viewer's cards and prefs to the survivor.
        Cards are re-sequenced after the survivor's own so the
        (round, user, seq) key never collides and no card is lost."""
        moved = 0
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, round_id FROM cards WHERE user_uuid=? ORDER BY round_id, seq",
                (absorbed_uuid,)).fetchall()
            for row in rows:
                nxt = self.conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM cards WHERE round_id=? AND user_uuid=?",
                    (row["round_id"], survivor_uuid)).fetchone()[0]
                self.conn.execute("UPDATE cards SET user_uuid=?, seq=? WHERE id=?",
                                  (survivor_uuid, int(nxt), row["id"]))
                moved += 1
            self.conn.execute("UPDATE rounds SET winner_uuid=? WHERE winner_uuid=?",
                              (survivor_uuid, absorbed_uuid))
            self.conn.execute("INSERT OR IGNORE INTO prefs (user_uuid, on_stream, updated_at)"
                              " SELECT ?, on_stream, updated_at FROM prefs WHERE user_uuid=?",
                              (survivor_uuid, absorbed_uuid))
            self.conn.execute("DELETE FROM prefs WHERE user_uuid=?", (absorbed_uuid,))
            self.conn.commit()
        return {"cards": moved}

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
        w = winner or {}
        with self._lock:
            self.conn.execute(
                "UPDATE rounds SET status='closed', ended_at=?, winner_uuid=?, winner_login=?, winner_name=?,"
                " winner_card=?, prize_paid=?, prize_ok=? WHERE id=?",
                (now, w.get("user_uuid"), w.get("login"), w.get("display"), w.get("card_id"), prize_paid,
                 None if prize_ok is None else int(bool(prize_ok)), int(round_id)))
            self.conn.commit()
        return self.get_round(round_id)

    def pot(self, round_id: int) -> int:
        r = self.get_round(round_id)
        if not r:
            return 0
        return int(r["base_prize"]) + int(round(int(r["sales"]) * float(r["pot_share"])))

    # ── cards ─────────────────────────────────────────────────────────

    def cards_for(self, round_id: int, user_uuid: str) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM cards WHERE round_id=? AND user_uuid=? ORDER BY seq",
                (int(round_id), user_uuid)).fetchall()
        return [self._card(r) for r in rows]

    def all_cards(self, round_id: int) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM cards WHERE round_id=? ORDER BY id", (int(round_id),)).fetchall()
        return [self._card(r) for r in rows]

    def add_card(self, round_id: int, user_uuid: str, login: Optional[str], display: Optional[str],
                 seq: int, price: int, squares: Sequence[str], called: Iterable[str],
                 now: Optional[float] = None) -> dict:
        """Insert a card; squares already called this round start marked
        (a late joiner is not punished for the round's history)."""
        now = time.time() if now is None else now
        marks = sorted(marked_indexes(list(squares), called))
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO cards (round_id, user_uuid, login, display, seq, price, squares, marks, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (int(round_id), user_uuid, (login or "").lower() or None, display or login or user_uuid,
                 int(seq), int(price), json.dumps(list(squares)), json.dumps(marks), now))
            if price > 0:
                self.conn.execute("UPDATE rounds SET sales = sales + ? WHERE id=?", (int(price), int(round_id)))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM cards WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._card(row)

    def card_count(self, round_id: int) -> Tuple[int, int]:
        """(cards, distinct players)"""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT user_uuid) FROM cards WHERE round_id=?",
                (int(round_id),)).fetchone()
        return int(row[0]), int(row[1])

    @staticmethod
    def _card(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["squares"] = json.loads(d["squares"])
        d["marks"] = json.loads(d["marks"] or "[]")
        d["to_bingo"] = squares_to_bingo(set(d["marks"]))
        d["has_line"] = d["bingo_at"] is not None
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
        "winners" = cards that just completed a line (bingo_at set). They
        have NOT won yet: the viewer must claim (check_claim) first.
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
                "SELECT * FROM cards WHERE round_id=? ORDER BY id", (int(round_id),)).fetchall()
            for row in rows:
                squares = json.loads(row["squares"])
                marks: Set[int] = set(json.loads(row["marks"] or "[]"))
                hits = {i for i, sid in enumerate(squares) if sid == event_id}
                if not hits or hits <= marks:
                    continue
                marks |= hits
                bingo = has_bingo(marks)
                new_line = bingo and row["bingo_at"] is None
                self.conn.execute("UPDATE cards SET marks=?, bingo_at=? WHERE id=?",
                                  (json.dumps(sorted(marks)), (row["bingo_at"] or now) if bingo else None, row["id"]))
                card = self._card(self.conn.execute("SELECT * FROM cards WHERE id=?", (row["id"],)).fetchone())
                result["changed"].append(card)
                if new_line:
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

    # ── prefs + the claim ─────────────────────────────────────────────

    def on_stream(self, user_uuid: str) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT on_stream FROM prefs WHERE user_uuid=?", (user_uuid,)).fetchone()
        return bool(row and row[0])

    def set_on_stream(self, user_uuid: str, on: bool, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            self.conn.execute(
                "INSERT INTO prefs (user_uuid, on_stream, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(user_uuid) DO UPDATE SET on_stream=excluded.on_stream, updated_at=excluded.updated_at",
                (user_uuid, int(bool(on)), now))
            self.conn.commit()
        return bool(on)

    def cards_on_stream(self, round_id: int) -> List[dict]:
        """Cards of viewers who opted in, closest to bingo first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.* FROM cards c JOIN prefs p ON p.user_uuid = c.user_uuid"
                " WHERE c.round_id=? AND p.on_stream=1 ORDER BY c.id", (int(round_id),)).fetchall()
        cards = [self._card(r) for r in rows]
        return sorted(cards, key=lambda c: (c["to_bingo"], c["created_at"]))

    def check_claim(self, round_id: int, card_id: int, user_uuid: str) -> dict:
        """Is this a valid Bingo! press? The line is recomputed from the
        round's calls, never trusted from the card row.
        -> {"ok": True, "card": card, "line": (i..)} or {"ok": False, "error"}"""
        with self._lock:
            row = self.conn.execute("SELECT * FROM cards WHERE id=?", (int(card_id),)).fetchone()
            called = {r[0] for r in self.conn.execute(
                "SELECT DISTINCT event_id FROM calls WHERE round_id=?", (int(round_id),)).fetchall()}
        if row is None or int(row["round_id"]) != int(round_id):
            return {"ok": False, "error": "That card is not in this round."}
        if row["user_uuid"] != user_uuid:
            return {"ok": False, "error": "That is not your card."}
        card = self._card(row)
        marks = marked_indexes(card["squares"], called)
        from .pool import winning_lines
        lines = winning_lines(marks)
        if not lines:
            return {"ok": False, "error": "Not a bingo yet: you need a full row, column, or diagonal."}
        return {"ok": True, "card": card, "line": lines[0]}

    # ── summaries ─────────────────────────────────────────────────────

    def rounds(self, limit: int = 20) -> List[dict]:
        """Most recent rounds first (open or closed)."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def card_list(self, round_id: int) -> List[dict]:
        """Compact view of every card in a round for the control page."""
        r = self.get_round(round_id) or {}
        return [{"id": c["id"], "user_uuid": c["user_uuid"], "login": c["login"], "display": c["display"],
                 "seq": c["seq"], "price": c["price"], "marked": len(c["marks"]), "to_bingo": c["to_bingo"],
                 "bingo": c["bingo_at"] is not None, "claimed": r.get("winner_card") == c["id"],
                 "on_stream": self.on_stream(c["user_uuid"]), "created_at": c["created_at"]}
                for c in self.all_cards(round_id)]

    def leaders(self, round_id: int, limit: int = 5) -> List[dict]:
        """Closest cards to bingo (fewest squares missing), one per player."""
        best: Dict[str, dict] = {}
        for c in self.all_cards(round_id):
            cur = best.get(c["user_uuid"])
            if cur is None or c["to_bingo"] < cur["to_bingo"]:
                best[c["user_uuid"]] = c
        rows = sorted(best.values(), key=lambda c: (c["to_bingo"], c["created_at"]))[:limit]
        return [{"user_uuid": c["user_uuid"], "login": c["login"], "display": c["display"],
                 "to_bingo": c["to_bingo"], "cards": len(self.cards_for(round_id, c["user_uuid"]))}
                for c in rows]

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
            "winner": {"user_uuid": r.get("winner_uuid"), "login": r.get("winner_login"),
                       "display": r.get("winner_name"), "prize": r.get("prize_paid")}
                      if r.get("winner_uuid") else None,
        }
