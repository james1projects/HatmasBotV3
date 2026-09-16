"""
core/youtube_videos.py — the upload catalogue behind /admin/videos
==================================================================

Pure SQLite helpers shared by plugins/youtube_rewards.py (which upserts
every upload it walks) and core/youtube_admin_web.py (the page). No
YouTube I/O here — that stays in the plugin — so everything in this
module is testable against a throwaway aiosqlite database.

Tables (core/youtube_schema.py):
    youtube_videos              every upload seen: title, publish date,
                                skipped flag ("no share for this video")
    youtube_video_gods          the god a video pays out (existing)
    youtube_processed_comments  one row per (video, commenter) paid (existing)

A video's status, as the page shows it:
    skipped        youtube_videos.skipped = 1
    categorized    has a youtube_video_gods row
    uncategorized  neither
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

import aiosqlite


STATUS_UNCATEGORIZED = "uncategorized"
STATUS_CATEGORIZED = "categorized"
STATUS_SKIPPED = "skipped"


def thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


def valid_video_id(video_id: str) -> bool:
    return bool(video_id) and bool(_VIDEO_ID_RE.match(video_id))


# ── writes ─────────────────────────────────────────────────────────────

async def upsert_video(db: aiosqlite.Connection, video_id: str,
                       title: str, published_at: str) -> bool:
    """Record an upload. Returns True if it was new. Title and publish
    date are refreshed on every sighting (titles get edited)."""
    async with db.execute(
            "SELECT 1 FROM youtube_videos WHERE yt_video_id = ?",
            (video_id,)) as c:
        existed = await c.fetchone() is not None
    await db.execute("""
        INSERT INTO youtube_videos (yt_video_id, title, published_at)
        VALUES (?, ?, ?)
        ON CONFLICT(yt_video_id) DO UPDATE SET
            title        = excluded.title,
            published_at = excluded.published_at,
            last_seen_at = datetime('now')
    """, (video_id, title or "", published_at or ""))
    return not existed


async def is_skipped(db: aiosqlite.Connection, video_id: str) -> bool:
    async with db.execute(
            "SELECT skipped FROM youtube_videos WHERE yt_video_id = ?",
            (video_id,)) as cur:
        row = await cur.fetchone()
    return bool(row and row[0])


async def set_skipped(db: aiosqlite.Connection, video_id: str,
                      skipped: bool) -> None:
    """Skip = drop any god mapping and flag the row. Unskip just clears
    the flag; the scanner may auto-tag it again on its next pass."""
    if skipped:
        await db.execute(
            "DELETE FROM youtube_video_gods WHERE yt_video_id = ?",
            (video_id,))
        await db.execute("""
            INSERT INTO youtube_videos (yt_video_id, skipped, skipped_at)
            VALUES (?, 1, datetime('now'))
            ON CONFLICT(yt_video_id) DO UPDATE SET
                skipped = 1, skipped_at = datetime('now')
        """, (video_id,))
    else:
        await db.execute(
            "UPDATE youtube_videos SET skipped = 0, skipped_at = NULL "
            "WHERE yt_video_id = ?", (video_id,))
    await db.commit()


async def set_god(db: aiosqlite.Connection, video_id: str, god: str,
                  set_by: str, title: Optional[str] = None) -> None:
    """Write (or overwrite) the god a video pays out and clear the
    skipped flag. Callers validate `god` against the roster first and
    decide whether an overwrite is allowed (grant_count == 0)."""
    if title is None:
        async with db.execute(
                "SELECT title FROM youtube_videos WHERE yt_video_id = ?",
                (video_id,)) as cur:
            row = await cur.fetchone()
        title = row[0] if row else ""
    await db.execute("""
        INSERT INTO youtube_video_gods
            (yt_video_id, god_name, title, set_at, set_by)
        VALUES (?, ?, ?, datetime('now'), ?)
        ON CONFLICT(yt_video_id) DO UPDATE SET
            god_name = excluded.god_name,
            title    = excluded.title,
            set_at   = excluded.set_at,
            set_by   = excluded.set_by
    """, (video_id, god, title, set_by))
    await db.execute(
        "UPDATE youtube_videos SET skipped = 0, skipped_at = NULL "
        "WHERE yt_video_id = ?", (video_id,))
    await db.commit()


# ── reads ──────────────────────────────────────────────────────────────

async def get_god(db: aiosqlite.Connection, video_id: str) -> Optional[str]:
    async with db.execute(
            "SELECT god_name FROM youtube_video_gods WHERE yt_video_id = ?",
            (video_id,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def grant_count(db: aiosqlite.Connection, video_id: str) -> int:
    async with db.execute(
            "SELECT COUNT(*) FROM youtube_processed_comments "
            "WHERE yt_video_id = ?", (video_id,)) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def list_videos(db: aiosqlite.Connection,
                      known_gods: Iterable[str] = ()) -> List[dict]:
    """Every known upload, newest first, in the shape the page renders.

    Videos that only exist in youtube_video_gods (tagged by the CLI
    before youtube_videos existed) are included too, so nothing the
    old tool touched goes missing from the Categorized tab.
    """
    async with db.execute("""
        SELECT v.yt_video_id, v.title, v.published_at, v.skipped,
               g.god_name, g.set_by, g.set_at,
               (SELECT COUNT(*) FROM youtube_processed_comments p
                 WHERE p.yt_video_id = v.yt_video_id) AS grants
          FROM youtube_videos v
          LEFT JOIN youtube_video_gods g ON g.yt_video_id = v.yt_video_id
        UNION ALL
        SELECT g.yt_video_id, COALESCE(g.title, ''), '', 0,
               g.god_name, g.set_by, g.set_at,
               (SELECT COUNT(*) FROM youtube_processed_comments p
                 WHERE p.yt_video_id = g.yt_video_id)
          FROM youtube_video_gods g
         WHERE g.yt_video_id NOT IN (SELECT yt_video_id FROM youtube_videos)
         ORDER BY 3 DESC, 1
    """) as cur:
        rows = await cur.fetchall()

    gods = list(known_gods)
    regex = _god_regex(gods)
    out: List[dict] = []
    for (vid, title, published, skipped, god, set_by, set_at,
         grants) in rows:
        if skipped:
            status = STATUS_SKIPPED
        elif god:
            status = STATUS_CATEGORIZED
        else:
            status = STATUS_UNCATEGORIZED
        out.append({
            "video_id": vid,
            "title": title or "",
            "published_at": published or "",
            "thumb": thumbnail_url(vid),
            "url": watch_url(vid),
            "status": status,
            "god": god,
            "set_by": set_by,
            "set_at": set_at,
            "grants": int(grants or 0),
            "suggestion": (suggest_god(title or "", gods, regex)
                           if status == STATUS_UNCATEGORIZED else None),
        })
    return out


# ── suggestions ────────────────────────────────────────────────────────

def _god_regex(gods: List[str]) -> Optional[re.Pattern]:
    if not gods:
        return None
    alt = "|".join(re.escape(g) for g in sorted(gods, key=len, reverse=True))
    return re.compile(r"\b(" + alt + r")\b", re.IGNORECASE)


def suggest_god(title: str, known_gods: Iterable[str],
                regex: Optional[re.Pattern] = None) -> Optional[str]:
    """First god name found anywhere in `title`, canonical casing, or
    None. Looser than core.youtube_parser.parse_my_god (which needs the
    "Full Gameplay:" prefix) — it is a suggestion the broadcaster
    confirms, never an auto-tag."""
    gods = list(known_gods)
    if not title or not gods:
        return None
    regex = regex or _god_regex(gods)
    m = regex.search(title)
    if not m:
        return None
    hit = m.group(1).lower()
    for g in gods:
        if g.lower() == hit:
            return g
    return None


def resolve_god(name: str, known_gods: Iterable[str]) -> Optional[str]:
    """Case/whitespace-insensitive match of an operator-typed god name
    against the roster; returns the canonical spelling or None."""
    key = " ".join((name or "").split()).lower()
    if not key:
        return None
    for g in known_gods:
        if g.lower() == key:
            return g
    return None
