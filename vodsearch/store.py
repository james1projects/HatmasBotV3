"""
SQLite store for vodsearch: recordings, transcript segments (FTS5),
detector events, and the search / browse / stats queries the CLI and
the website share.

Synchronous `sqlite3` on purpose. Every query here is a few
milliseconds against a local file; the aiohttp server wraps calls in
`asyncio.to_thread` so the event loop never blocks, and the indexer is
a plain script. One `Store` = one connection; open it per request on
the server (cheap) and once for the whole run in the indexer.

Moment shape (what search/browse return, and what the page renders):
    {
      "segment_id": int | None,       # transcript hit (None for pure events)
      "recording_id": int,
      "god": str | None,
      "recorded_at": "2026-07-15T17:10:48",
      "start_s": float, "end_s": float,
      "speaker": "hatmaster",
      "text": str,                    # full segment text
      "snippet": str,                 # FTS snippet with <mark> tags (search only)
      "events": [{"kind": "kill", "type": "kill", "note": "double kill",
                  "ts_s": 1309.1}],   # detector events within EVENT_WINDOW_S
      "duration_s": float,            # recording length, for clip clamping
    }
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

EVENT_WINDOW_S = 20.0          # events this close to a segment count as "at" it
MULTIKILL_WORDS = ("double", "triple", "quadra", "penta")
TIER_WORDS = {"double": 2, "triple": 3, "quadra": 4, "penta": 5}
TIER_LABELS = {1: "Kill", 2: "Double kill", 3: "Triple kill", 4: "Quadra kill", 5: "Penta kill"}
MULTIKILL_WINDOW_S = 10.0      # Smite 2 streak window, same constant the detectors use

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    folder       TEXT,
    stem         TEXT,
    god          TEXT,
    gods_seen    TEXT,              -- JSON list
    duration_s   REAL DEFAULT 0,
    size_bytes   INTEGER DEFAULT 0,
    mtime        REAL DEFAULT 0,
    recorded_at  TEXT,              -- ISO-8601, local time of the recording
    indexed_at   TEXT,
    model        TEXT,
    status       TEXT DEFAULT 'pending',   -- pending | indexing | done | error | skipped
    error        TEXT,
    visibility   TEXT DEFAULT 'private'    -- private | public (public site only ever shows 'public')
);
CREATE INDEX IF NOT EXISTS idx_recordings_god ON recordings(god);
CREATE INDEX IF NOT EXISTS idx_recordings_recorded ON recordings(recorded_at);

CREATE TABLE IF NOT EXISTS tracks (
    recording_id INTEGER NOT NULL,
    track_index  INTEGER NOT NULL,
    speaker      TEXT,
    rms_db       REAL,
    speech_frac  REAL,
    transcribed  INTEGER DEFAULT 0,
    segments     INTEGER DEFAULT 0,
    PRIMARY KEY (recording_id, track_index)
);

CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY,
    recording_id   INTEGER NOT NULL,
    track_index    INTEGER,
    speaker        TEXT,
    start_s        REAL NOT NULL,
    end_s          REAL NOT NULL,
    text           TEXT NOT NULL,
    no_speech_prob REAL,
    avg_logprob    REAL,
    hidden         INTEGER DEFAULT 0      -- 1 = redacted: never shown to visitors
);
CREATE INDEX IF NOT EXISTS idx_segments_rec ON segments(recording_id, start_s);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text,
    content='segments', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL,
    ts_s         REAL NOT NULL,
    type         TEXT NOT NULL,     -- raw sidecar type: kill/death/assist/game_start/game_end
    kind         TEXT NOT NULL,     -- kill | multikill | death | assist | game_start | game_end
    note         TEXT,
    pre_s        REAL DEFAULT 0,
    post_s       REAL DEFAULT 0,
    god          TEXT,
    tier         INTEGER DEFAULT 0  -- kills: 1 single .. 5 penta; 0 for non-kills
);
CREATE INDEX IF NOT EXISTS idx_events_rec ON events(recording_id, ts_s);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS embeddings (
    segment_id INTEGER PRIMARY KEY,   -- rows vanish with their segment (see replace_segments)
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL          -- float32, unit-normalized
);
"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _word_tier(text: str) -> int:
    t = 0
    low = (text or "").lower()
    for w, v in TIER_WORDS.items():
        if w in low:
            t = max(t, v)
    return t


def _chain_tier(ts: Optional[float], merged: Optional[Sequence[dict]]) -> int:
    """Longest run of kills with consecutive gaps <= MULTIKILL_WINDOW_S,
    from the anchor timestamp plus the merged kills' timestamps. Only
    used when the detector left no label."""
    times: List[float] = []
    if ts is not None:
        times.append(float(ts))
    for m in merged or ():
        if (m.get("type") or "").lower() != "kill":
            continue
        try:
            times.append(float(m.get("timestamp_sec", m.get("ts_s"))))
        except (TypeError, ValueError):
            continue
    if len(times) < 2:
        return min(len(times), 1)
    times.sort()
    best = run = 1
    for a, b in zip(times, times[1:]):
        run = run + 1 if (b - a) <= MULTIKILL_WINDOW_S else 1
        best = max(best, run)
    return best


def event_tier(ev_type: str, note: str = "", merged: Optional[Sequence[dict]] = None,
               ts: Optional[float] = None) -> int:
    """Kill streak size for a sidecar event: 1 single .. 5 penta, 0 for
    non-kills. The detectors already judge streaks with the 10 s rule and
    write the label on the kill that completed it ("double kill",
    "triple kill"), which after overlap-merging sits in `merged`. So the
    tier is the biggest label in the group. The "kill + kill + kill" note
    text is the merger listing overlapping clip windows, NOT a streak
    (2026-09-04: counting it produced phantom quadras). With no label at
    all, fall back to the 10 s rule on the timestamps."""
    if (ev_type or "").lower() != "kill":
        return 0
    tier = _word_tier(note)
    for m in merged or ():
        tier = max(tier, _word_tier(m.get("note") or ""))
    if tier == 0:
        tier = _chain_tier(ts, merged)
    return max(1, min(tier, 5))


def tier_label(tier: int) -> str:
    return TIER_LABELS.get(int(tier or 0), "Multikill" if tier and tier > 5 else "")


def event_kind(ev_type: str, note: str = "", merged: Optional[Sequence[dict]] = None,
               ts: Optional[float] = None) -> str:
    """Classify a sidecar event: 'multikill' for any kill streak of two
    or more, else the raw type lowercased."""
    t = (ev_type or "").lower()
    if t != "kill":
        return t or "unknown"
    return "multikill" if event_tier(t, note, merged, ts) >= 2 else "kill"


_TOKEN_RE = re.compile(r'"[^"]*"|[^\s"]+')
_DEDUPE_RE = re.compile(r"[^a-z0-9]+")
DEDUPE_WINDOW_S = 6.0


def dedupe_moments(moments: List[dict], window_s: float = DEDUPE_WINDOW_S) -> List[dict]:
    """Collapse the same words spoken at the same time in the same
    recording. The Discord/"friends" track often carries a bleed of the
    streamer's own voice, so one sentence can be transcribed twice a few
    hundred ms apart; showing both is noise. Keeps the first (best
    ranked) occurrence."""
    out: List[dict] = []
    seen: List[tuple] = []
    for m in moments:
        key_text = _DEDUPE_RE.sub("", (m.get("text") or "").lower())
        rec = m.get("recording_id")
        t = float(m.get("start_s") or 0.0)
        dup = False
        for r2, k2, t2 in seen:
            if r2 == rec and k2 == key_text and key_text and abs(t2 - t) <= window_s:
                dup = True
                break
        if dup:
            continue
        seen.append((rec, key_text, t))
        out.append(m)
    return out
_WORD_RE = re.compile(r"[^\w']+", re.UNICODE)


def build_match(query: str, mode: str = "and") -> str:
    """Turn free text into a safe FTS5 MATCH expression. Quoted phrases
    stay phrases, a trailing * becomes a prefix query, everything else
    is a bare quoted term. Returns '' when nothing survives."""
    parts: List[str] = []
    for tok in _TOKEN_RE.findall(query or ""):
        if tok.startswith('"') and tok.endswith('"') and len(tok) > 2:
            inner = tok[1:-1].replace('"', " ").strip()
            if inner:
                parts.append('"' + inner + '"')
            continue
        prefix = tok.endswith("*")
        word = _WORD_RE.sub(" ", tok.rstrip("*")).strip()
        if not word:
            continue
        # a token like "don't" tokenizes to don + t under unicode61; FTS
        # treats a quoted multi-token string as a phrase, which is right.
        parts.append('"' + word + '"' + ("*" if prefix else ""))
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join(parts)


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an older index up to the current schema. Cheap enough
        to run on every open (the site opens a Store per request)."""
        scols = {r[1] for r in self.conn.execute("PRAGMA table_info(segments)")}
        if "hidden" not in scols:
            self.conn.execute("ALTER TABLE segments ADD COLUMN hidden INTEGER DEFAULT 0")
            self.conn.commit()
        rcols = {r[1] for r in self.conn.execute("PRAGMA table_info(recordings)")}
        if "visibility" not in rcols:
            self.conn.execute("ALTER TABLE recordings ADD COLUMN visibility TEXT DEFAULT 'private'")
            self.conn.commit()
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        if "tier" not in cols:
            self.conn.execute("ALTER TABLE events ADD COLUMN tier INTEGER DEFAULT 0")
        # index lives here, not in SCHEMA, so it never runs before the column exists
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_tier ON events(tier)")
        self.conn.commit()
        # Rows written without a tier (older code, or an indexer that was
        # already running when this column landed) get one from their note.
        rows = self.conn.execute(
            "SELECT id, type, note FROM events WHERE type='kill' AND (tier IS NULL OR tier < 1)"
        ).fetchall()
        if rows:
            updates = []
            for r in rows:
                t = event_tier(r["type"], r["note"])
                updates.append((t, "multikill" if t >= 2 else "kill", r["id"]))
            self.conn.executemany("UPDATE events SET tier=?, kind=? WHERE id=?", updates)
            self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── writes ────────────────────────────────────────────────────────

    def upsert_recording(self, path: Path | str, **meta: Any) -> int:
        """Insert or update a recording row keyed on its absolute path.
        Returns the row id. Unknown meta keys are ignored."""
        cols = {"folder", "stem", "god", "gods_seen", "duration_s", "size_bytes",
                "mtime", "recorded_at", "indexed_at", "model", "status", "error"}
        # visibility is deliberately NOT here: it is set only through
        # set_visibility() so a re-index can never publish or unpublish.
        data = {k: v for k, v in meta.items() if k in cols}
        if isinstance(data.get("gods_seen"), (list, tuple)):
            data["gods_seen"] = json.dumps(list(data["gods_seen"]))
        path = str(path)
        row = self.conn.execute("SELECT id FROM recordings WHERE path=?", (path,)).fetchone()
        if row is None:
            keys = ["path"] + list(data)
            self.conn.execute(
                f"INSERT INTO recordings ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                [path] + [data[k] for k in data])
            rec_id = int(self.conn.execute("SELECT id FROM recordings WHERE path=?", (path,)).fetchone()[0])
        else:
            rec_id = int(row[0])
            if data:
                sets = ", ".join(f"{k}=?" for k in data)
                self.conn.execute(f"UPDATE recordings SET {sets} WHERE id=?",
                                  [data[k] for k in data] + [rec_id])
        self.conn.commit()
        return rec_id

    def set_status(self, rec_id: int, status: str, error: Optional[str] = None) -> None:
        self.conn.execute("UPDATE recordings SET status=?, error=? WHERE id=?",
                          (status, error, rec_id))
        self.conn.commit()

    def set_visibility(self, rec_ids: Iterable[int], visibility: str) -> int:
        """Publish or unpublish recordings. Returns rows changed."""
        if visibility not in ("private", "public"):
            raise ValueError("visibility must be 'private' or 'public'")
        ids = [int(i) for i in rec_ids]
        if not ids:
            return 0
        cur = self.conn.execute(
            f"UPDATE recordings SET visibility=? WHERE id IN ({','.join('?' * len(ids))})",
            [visibility] + ids)
        self.conn.commit()
        return int(cur.rowcount or 0)

    def set_hidden(self, seg_ids: Iterable[int], hidden: bool) -> int:
        """Redact (or restore) transcript lines. Hidden lines stay
        searchable locally but never reach visitors, in any view."""
        ids = [int(i) for i in seg_ids]
        if not ids:
            return 0
        cur = self.conn.execute(
            f"UPDATE segments SET hidden=? WHERE id IN ({','.join('?' * len(ids))})",
            [1 if hidden else 0] + ids)
        self.conn.commit()
        return int(cur.rowcount or 0)

    def recording_ids(self, god: Optional[str] = None, folder: Optional[str] = None,
                      status: Optional[str] = "done") -> List[int]:
        where, params = [], []
        if god:
            where.append("god = ?"); params.append(god)
        if folder:
            where.append("folder = ?"); params.append(folder)
        if status:
            where.append("status = ?"); params.append(status)
        sql = "SELECT id FROM recordings" + (" WHERE " + " AND ".join(where) if where else "")
        return [int(r[0]) for r in self.conn.execute(sql, params)]

    def review_list(self) -> List[dict]:
        """Every indexed recording with the counts the review page shows."""
        rows = self.conn.execute(
            "SELECT r.id, r.stem, r.folder, r.god, r.recorded_at, r.duration_s, r.status,"
            " r.visibility,"
            " (SELECT COUNT(*) FROM segments s WHERE s.recording_id = r.id) AS segments,"
            " (SELECT COUNT(*) FROM segments s WHERE s.recording_id = r.id AND s.speaker != 'hatmaster') AS friend_segments,"
            " (SELECT COUNT(*) FROM events e WHERE e.recording_id = r.id AND e.kind IN ('kill','multikill')) AS kills,"
            " (SELECT COUNT(*) FROM events e WHERE e.recording_id = r.id AND e.kind = 'death') AS deaths,"
            " (SELECT MAX(e.tier) FROM events e WHERE e.recording_id = r.id) AS best_tier,"
            " (SELECT e.id FROM events e WHERE e.recording_id = r.id AND e.kind IN ('kill','multikill')"
            "   ORDER BY e.tier DESC, e.ts_s LIMIT 1) AS top_event_id,"
            " (SELECT s.id FROM segments s WHERE s.recording_id = r.id ORDER BY s.start_s LIMIT 1) AS first_segment_id"
            " FROM recordings r ORDER BY r.recorded_at DESC, r.id DESC").fetchall()
        return [dict(r) for r in rows]

    def replace_tracks(self, rec_id: int, tracks: Iterable[dict]) -> None:
        self.conn.execute("DELETE FROM tracks WHERE recording_id=?", (rec_id,))
        self.conn.executemany(
            "INSERT INTO tracks (recording_id, track_index, speaker, rms_db, speech_frac, transcribed, segments)"
            " VALUES (?,?,?,?,?,?,?)",
            [(rec_id, int(t["track_index"]), t.get("speaker"), t.get("rms_db"),
              t.get("speech_frac"), int(bool(t.get("transcribed"))), int(t.get("segments") or 0))
             for t in tracks])
        self.conn.commit()

    def replace_segments(self, rec_id: int, segments: Iterable[dict],
                         track_index: Optional[int] = None) -> int:
        """Replace this recording's segments (all of them, or just one
        track's when `track_index` is given). Returns the count inserted."""
        if track_index is None:
            self.conn.execute("DELETE FROM embeddings WHERE segment_id IN"
                              " (SELECT id FROM segments WHERE recording_id=?)", (rec_id,))
            self.conn.execute("DELETE FROM segments WHERE recording_id=?", (rec_id,))
        else:
            self.conn.execute("DELETE FROM embeddings WHERE segment_id IN"
                              " (SELECT id FROM segments WHERE recording_id=? AND track_index=?)",
                              (rec_id, track_index))
            self.conn.execute("DELETE FROM segments WHERE recording_id=? AND track_index=?",
                              (rec_id, track_index))
        rows = [(rec_id, s.get("track_index", track_index), s.get("speaker"),
                 float(s["start_s"]), float(s["end_s"]), str(s["text"]).strip(),
                 s.get("no_speech_prob"), s.get("avg_logprob"))
                for s in segments if str(s.get("text", "")).strip()]
        self.conn.executemany(
            "INSERT INTO segments (recording_id, track_index, speaker, start_s, end_s, text, no_speech_prob, avg_logprob)"
            " VALUES (?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def replace_events(self, rec_id: int, events: Iterable[dict], god: Optional[str] = None) -> int:
        self.conn.execute("DELETE FROM events WHERE recording_id=?", (rec_id,))
        rows = []
        for e in events:
            try:
                ts = float(e.get("timestamp_sec", e.get("ts_s")))
            except (TypeError, ValueError):
                continue
            ev_type = str(e.get("type") or "").lower()
            if not ev_type:
                continue
            note = str(e.get("note") or "")
            merged = e.get("merged")
            tier = event_tier(ev_type, note, merged, ts)
            rows.append((rec_id, ts, ev_type,
                         (("multikill" if tier >= 2 else "kill") if ev_type == "kill"
                          else event_kind(ev_type, note, merged, ts)),
                         note, float(e.get("pre_sec", e.get("pre_s", 0)) or 0),
                         float(e.get("post_sec", e.get("post_s", 0)) or 0),
                         e.get("god") or god, tier))
        self.conn.executemany(
            "INSERT INTO events (recording_id, ts_s, type, kind, note, pre_s, post_s, god, tier)"
            " VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def delete_recording(self, rec_id: int) -> None:
        self.conn.execute("DELETE FROM embeddings WHERE segment_id IN"
                          " (SELECT id FROM segments WHERE recording_id=?)", (rec_id,))
        for table in ("segments", "events", "tracks"):
            self.conn.execute(f"DELETE FROM {table} WHERE recording_id=?", (rec_id,))
        self.conn.execute("DELETE FROM recordings WHERE id=?", (rec_id,))
        self.conn.commit()

    # ── reads ─────────────────────────────────────────────────────────

    def find_recording(self, path: Path | str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM recordings WHERE path=?", (str(path),)).fetchone()
        return dict(row) if row else None

    def get_recording(self, rec_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM recordings WHERE id=?", (int(rec_id),)).fetchone()
        return dict(row) if row else None

    def is_current(self, path: Path | str, size_bytes: int, mtime: float, model: str) -> bool:
        """True when this file was fully indexed with the same model and
        hasn't changed on disk since (size + mtime, 1s tolerance)."""
        rec = self.find_recording(path)
        if not rec or rec.get("status") != "done":
            return False
        if rec.get("model") != model:
            return False
        if int(rec.get("size_bytes") or 0) != int(size_bytes):
            return False
        return abs(float(rec.get("mtime") or 0) - float(mtime)) < 1.0

    def list_recordings(self, status: Optional[str] = None) -> List[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM recordings WHERE status=? ORDER BY recorded_at DESC", (status,))
        else:
            rows = self.conn.execute("SELECT * FROM recordings ORDER BY recorded_at DESC")
        return [dict(r) for r in rows]

    def get_segment(self, seg_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT s.*, r.god AS god, r.recorded_at AS recorded_at, r.duration_s AS duration_s,"
            " r.path AS path, r.visibility AS visibility"
            " FROM segments s JOIN recordings r ON r.id = s.recording_id"
            " WHERE s.id=?", (int(seg_id),)).fetchone()
        return dict(row) if row else None

    def get_event(self, ev_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT e.*, r.god AS rec_god, r.recorded_at AS recorded_at, r.duration_s AS duration_s,"
            " r.path AS path, r.visibility AS visibility"
            " FROM events e JOIN recordings r ON r.id = e.recording_id"
            " WHERE e.id=?", (int(ev_id),)).fetchone()
        return dict(row) if row else None

    def events_near(self, rec_id: int, t_s: float, window_s: float = EVENT_WINDOW_S) -> List[dict]:
        rows = self.conn.execute(
            "SELECT id, ts_s, type, kind, note, tier FROM events WHERE recording_id=?"
            " AND ts_s BETWEEN ? AND ? AND kind NOT IN ('game_start','game_end')"
            " ORDER BY ABS(ts_s - ?)",
            (rec_id, t_s - window_s, t_s + window_s, t_s)).fetchall()
        return [self._event_dict(r) for r in rows]

    @staticmethod
    def _event_dict(row) -> dict:
        d = {k: row[k] for k in ("id", "ts_s", "type", "kind", "note", "tier")}
        d["tier"] = int(d.get("tier") or 0)
        d["label"] = tier_label(d["tier"]) if d["kind"] in ("kill", "multikill") else (
            {"death": "Death", "assist": "Assist"}.get(d["kind"], d["kind"].title()))
        return d

    def segments_near(self, rec_id: int, t_s: float, window_s: float = 15.0,
                      public_only: bool = False) -> List[dict]:
        rows = self.conn.execute(
            "SELECT id, start_s, end_s, speaker, text, hidden FROM segments s WHERE recording_id=?"
            " AND end_s >= ? AND start_s <= ?" + self._hidden_sql(public_only) + " ORDER BY start_s",
            (rec_id, t_s - window_s, t_s + window_s)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _vis_sql(public_only: bool, alias: str = "r") -> str:
        return f" AND {alias}.visibility = 'public'" if public_only else ""

    @staticmethod
    def _hidden_sql(public_only: bool, alias: str = "s") -> str:
        return f" AND {alias}.hidden = 0" if public_only else ""

    def gods(self, public_only: bool = False) -> List[dict]:
        rows = self.conn.execute(
            "SELECT god, COUNT(*) AS recordings, ROUND(SUM(duration_s)/3600.0, 1) AS hours"
            " FROM recordings r WHERE status='done' AND god IS NOT NULL AND god != ''"
            + self._vis_sql(public_only) +
            " GROUP BY god ORDER BY recordings DESC, god").fetchall()
        return [dict(r) for r in rows]

    def stats(self, public_only: bool = False) -> dict:
        c = self.conn
        vis = self._vis_sql(public_only)
        rec = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(duration_s),0) AS secs"
                        " FROM recordings r WHERE status='done'" + vis).fetchone()
        segs = c.execute("SELECT COUNT(*) FROM segments s JOIN recordings r ON r.id = s.recording_id"
                         " WHERE 1=1" + vis + self._hidden_sql(public_only)).fetchone()[0]
        words = c.execute("SELECT COALESCE(SUM(LENGTH(s.text) - LENGTH(REPLACE(s.text,' ',''))+1),0)"
                          " FROM segments s JOIN recordings r ON r.id = s.recording_id WHERE 1=1" + vis).fetchone()[0]
        kinds = {r[0]: r[1] for r in c.execute(
            "SELECT e.kind, COUNT(*) FROM events e JOIN recordings r ON r.id = e.recording_id"
            " WHERE 1=1" + vis + " GROUP BY e.kind").fetchall()}
        by_status = {r[0]: r[1] for r in c.execute(
            "SELECT status, COUNT(*) FROM recordings GROUP BY status").fetchall()}
        by_vis = {r[0]: r[1] for r in c.execute(
            "SELECT visibility, COUNT(*) FROM recordings WHERE status='done' GROUP BY visibility").fetchall()}
        newest = c.execute("SELECT MAX(recorded_at) FROM recordings r WHERE status='done'" + vis).fetchone()[0]
        oldest = c.execute("SELECT MIN(recorded_at) FROM recordings r WHERE status='done'" + vis).fetchone()[0]
        return {
            "recordings": int(rec["n"]),
            "hours": round(float(rec["secs"]) / 3600.0, 1),
            "segments": int(segs),
            "words": int(words),
            "events": kinds,
            "by_status": by_status,
            "by_visibility": by_vis,
            "public_only": bool(public_only),
            "oldest": oldest,
            "newest": newest,
            "gods": self.gods(public_only),
        }

    # ── embeddings (semantic search) ──────────────────────────────────

    def segments_without_embeddings(self, model: str, limit: int = 5000) -> List[dict]:
        rows = self.conn.execute(
            "SELECT s.id, s.text FROM segments s LEFT JOIN embeddings e"
            " ON e.segment_id = s.id AND e.model = ? WHERE e.segment_id IS NULL"
            " ORDER BY s.id LIMIT ?", (model, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def put_embeddings(self, model: str, rows: Iterable[Tuple[int, np.ndarray]]) -> int:
        data = []
        for seg_id, vec in rows:
            vec = np.asarray(vec, dtype=np.float32)
            data.append((int(seg_id), model, int(vec.shape[-1]), vec.tobytes()))
        if not data:
            return 0
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings (segment_id, model, dim, vec) VALUES (?,?,?,?)", data)
        self.conn.commit()
        return len(data)

    def embedding_count(self, model: Optional[str] = None) -> int:
        if model:
            return int(self.conn.execute("SELECT COUNT(*) FROM embeddings WHERE model=?", (model,)).fetchone()[0])
        return int(self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])

    def embedding_version(self, model: str) -> Tuple[int, int]:
        """(count, max segment id) - cheap change detector for caches."""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(segment_id), 0) FROM embeddings WHERE model=?", (model,)).fetchone()
        return int(row[0]), int(row[1])

    def load_embeddings(self, model: str) -> Tuple[np.ndarray, np.ndarray]:
        """-> (ids int64[n], matrix float32[n, dim]) for the whole index.
        Visibility/god/event filtering happens afterwards in SQL on the
        candidate ids, so this stays a plain cacheable matrix."""
        rows = self.conn.execute(
            "SELECT segment_id, dim, vec FROM embeddings WHERE model=? ORDER BY segment_id",
            (model,)).fetchall()
        if not rows:
            return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
        dim = int(rows[0]["dim"])
        ids = np.fromiter((r["segment_id"] for r in rows), dtype=np.int64, count=len(rows))
        mat = np.empty((len(rows), dim), dtype=np.float32)
        for i, r in enumerate(rows):
            mat[i] = np.frombuffer(r["vec"], dtype=np.float32, count=dim)
        return ids, mat

    def moments_for_segments(self, scored: Sequence[Tuple[int, float]], god: Optional[str] = None,
                             event: Optional[str] = None, speaker: Optional[str] = None,
                             public_only: bool = False, limit: int = 20) -> List[dict]:
        """Turn (segment_id, score) candidates into moments, applying the
        same god/event/speaker/visibility filters as search(). Keeps the
        candidates' order."""
        if not scored:
            return []
        ids = [int(i) for i, _ in scored]
        score_of = {int(i): float(sc) for i, sc in scored}
        where = (f" WHERE s.id IN ({','.join('?' * len(ids))})" + self._vis_sql(public_only)
                 + self._hidden_sql(public_only))
        params: List[Any] = list(ids)
        if god:
            where += " AND r.god = ?"; params.append(god)
        if speaker:
            where += " AND s.speaker = ?"; params.append(speaker)
        where += self._event_filter_sql(event)
        rows = self.conn.execute(
            "SELECT s.*, r.god AS god, r.recorded_at AS recorded_at, r.duration_s AS duration_s,"
            " r.visibility AS visibility FROM segments s JOIN recordings r ON r.id = s.recording_id"
            + where, params).fetchall()
        by_id = {int(r["id"]): r for r in rows}
        out = []
        for seg_id in ids:
            r = by_id.get(seg_id)
            if r is None:
                continue
            m = self._moment_from_segment_row(r, None)
            m["score"] = round(score_of.get(seg_id, 0.0), 3)
            m["via"] = "meaning"
            out.append(m)
        return dedupe_moments(out)[:limit]

    # ── search ────────────────────────────────────────────────────────

    @staticmethod
    def _event_filter_sql(event: Optional[str]) -> str:
        """EXISTS clause requiring a detector event of the given kind
        within EVENT_WINDOW_S of the segment. 'kill' includes multikills."""
        if not event or event == "any_or_none":
            return ""
        if event == "any":
            kinds = "('kill','multikill','death','assist')"
        elif event == "kill":
            kinds = "('kill','multikill')"
        elif event in ("multikill", "death", "assist"):
            kinds = f"('{event}')"
        elif event in TIER_WORDS:
            kinds = "('multikill')"
        else:
            return ""
        tier_sql = f" AND e.tier = {TIER_WORDS[event]}" if event in TIER_WORDS else ""
        return (f" AND EXISTS (SELECT 1 FROM events e WHERE e.recording_id = s.recording_id"
                f" AND e.kind IN {kinds}{tier_sql} AND e.ts_s BETWEEN s.start_s - {EVENT_WINDOW_S}"
                f" AND s.end_s + {EVENT_WINDOW_S})")

    @staticmethod
    def _tier_order_sql(event: Optional[str]) -> str:
        """Multikill views sort biggest streak first; the subquery is the
        best tier within the segment's event window."""
        if event not in ("multikill",) and event not in TIER_WORDS:
            return ""
        return (f" (SELECT MAX(e.tier) FROM events e WHERE e.recording_id = s.recording_id"
                f" AND e.kind = 'multikill' AND e.ts_s BETWEEN s.start_s - {EVENT_WINDOW_S}"
                f" AND s.end_s + {EVENT_WINDOW_S}) DESC,")

    def _moment_from_segment_row(self, row: sqlite3.Row, snippet: Optional[str] = None) -> dict:
        d = dict(row)
        rec_id = int(d["recording_id"])
        mid = (float(d["start_s"]) + float(d["end_s"])) / 2.0
        return {
            "segment_id": int(d["id"]),
            "recording_id": rec_id,
            "god": d.get("god"),
            "recorded_at": d.get("recorded_at"),
            "start_s": float(d["start_s"]),
            "end_s": float(d["end_s"]),
            "speaker": d.get("speaker"),
            "text": d.get("text"),
            "snippet": snippet if snippet is not None else d.get("text"),
            "events": self.events_near(rec_id, mid),
            "duration_s": float(d.get("duration_s") or 0),
            "visibility": d.get("visibility") or "private",
            "hidden": bool(d.get("hidden") or 0),
        }

    def search(self, query: str, god: Optional[str] = None, event: Optional[str] = None,
               speaker: Optional[str] = None, limit: int = 20, offset: int = 0,
               public_only: bool = False) -> dict:
        """Full-text search over transcript segments. Tries an AND query
        first; if nothing matches, falls back to OR so a typo in one word
        doesn't return an empty page. Returns {"mode", "total", "moments"}."""
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        for mode in ("and", "or"):
            match = build_match(query, mode)
            if not match:
                return {"mode": mode, "total": 0, "moments": []}
            where = " WHERE segments_fts MATCH ?" + self._vis_sql(public_only) + self._hidden_sql(public_only)
            params: List[Any] = [match]
            if god:
                where += " AND r.god = ?"
                params.append(god)
            if speaker:
                where += " AND s.speaker = ?"
                params.append(speaker)
            where += self._event_filter_sql(event)
            base = (" FROM segments_fts f JOIN segments s ON s.id = f.rowid"
                    " JOIN recordings r ON r.id = s.recording_id" + where)
            try:
                total = int(self.conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0])
            except sqlite3.OperationalError:
                # malformed MATCH despite sanitizing; treat as no results
                return {"mode": mode, "total": 0, "moments": []}
            if total == 0 and mode == "and":
                continue
            rows = self.conn.execute(
                "SELECT s.*, r.god AS god, r.recorded_at AS recorded_at, r.duration_s AS duration_s,"
                " r.visibility AS visibility,"
                " snippet(segments_fts, 0, '<mark>', '</mark>', '…', 28) AS snip,"
                " bm25(segments_fts) AS rank" + base +
                " ORDER BY" + self._tier_order_sql(event) + " rank, r.recorded_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
            moments = dedupe_moments([self._moment_from_segment_row(r, r["snip"]) for r in rows])
            for m in moments:
                m["via"] = "keyword"
            return {"mode": mode, "total": total, "moments": moments}
        return {"mode": "or", "total": 0, "moments": []}

    def browse(self, god: Optional[str] = None, event: Optional[str] = None,
               limit: int = 20, offset: int = 0, public_only: bool = False) -> dict:
        """No-query view: newest detector events (kills by default, or
        the requested kind) with the nearest transcript line attached."""
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        event = event or "any"
        kinds = {"kill": "('kill','multikill')", "multikill": "('multikill')",
                 "death": "('death')", "assist": "('assist')",
                 "any": "('kill','multikill','death','assist')"}.get(
            event, "('multikill')" if event in TIER_WORDS else "('kill','multikill','death','assist')")
        where = f" WHERE e.kind IN {kinds} AND r.status='done'" + self._vis_sql(public_only)
        params: List[Any] = []
        if event in TIER_WORDS:
            where += " AND e.tier = ?"
            params.append(TIER_WORDS[event])
        if god:
            where += " AND r.god = ?"
            params.append(god)
        base = " FROM events e JOIN recordings r ON r.id = e.recording_id" + where
        total = int(self.conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0])
        order = (" ORDER BY e.tier DESC, r.recorded_at DESC, e.ts_s DESC"
                 if event == "multikill" or event in TIER_WORDS
                 else " ORDER BY r.recorded_at DESC, e.ts_s DESC")
        rows = self.conn.execute(
            "SELECT e.*, r.god AS rec_god, r.recorded_at AS recorded_at, r.duration_s AS duration_s,"
            " r.visibility AS visibility"
            + base + order + " LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        moments = []
        for r in rows:
            d = dict(r)
            rec_id = int(d["recording_id"])
            ts = float(d["ts_s"])
            near = self.segments_near(rec_id, ts, window_s=12.0, public_only=public_only)
            text = " ".join(s["text"] for s in near)
            seg_id = near[0]["id"] if near else None
            moments.append({
                "segment_id": seg_id,
                "event_id": int(d["id"]),
                "recording_id": rec_id,
                "god": d.get("god") or d.get("rec_god"),
                "recorded_at": d.get("recorded_at"),
                "start_s": max(0.0, ts - float(d.get("pre_s") or 0)),
                "end_s": ts + float(d.get("post_s") or 0),
                "ts_s": ts,
                "speaker": near[0]["speaker"] if near else None,
                "text": text,
                "snippet": text,
                "events": [self._event_dict(r)],
                "duration_s": float(d.get("duration_s") or 0),
                "visibility": d.get("visibility") or "private",
            })
        return {"mode": "browse", "total": total, "moments": moments}
