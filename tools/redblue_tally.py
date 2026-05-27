"""
tools/redblue_tally.py
======================

Standalone CLI for tallying "Red" / "Blue" votes from a YouTube video's
comments. Built for the Red/Blue Button TikTok-trend video — a single
hypothetical-question video where viewers vote in the comments by
typing one of the two colors. Whichever color appears first in their
comment counts as their vote; first vote per channel wins.

This tool is intentionally stand-alone — it does not attach to the bot
runtime and does not touch `economy.db`. Storage lives in its own
SQLite file at `data/redblue.db` so the trend video's data is fully
isolated from the rest of HatmasBot.

Reuses HatmasBot infrastructure:
    - `core.config.YOUTUBE_API_KEY` for the read-only Data API key.
    - The same `commentThreads` polling shape that
      `plugins/youtube_rewards.py` already uses.

Vote semantics (live-reconciled with YouTube on every scan):
    - Search a comment's text for the first whole-word occurrence of
      "red" or "blue" (case-insensitive). That's the comment's vote.
        * "Red obviously, blue is suicide" -> red
        * "I'd press blue but red is safer" -> blue
        * "blueberry pie" -> NO vote (whole-word boundary skips it)
        * "lol" -> NO vote
    - A channel's CURRENT vote is the parsed result of their EARLIEST
      non-deleted comment that has a vote keyword. Channels with no
      vote-bearing comment contribute nothing to the tally.
    - Edits update the vote: if a viewer edits "lol" to "Red", the
      next scan picks up the edit (via the API's `updatedAt` field)
      and re-parses, registering their vote. Editing "Red" to "Blue"
      flips the channel's current vote to blue.
    - Deletions revoke the vote: if a viewer deletes the comment that
      sourced their vote, the next scan notices its absence in the API
      response, marks it deleted locally, and the tally re-derives
      from whatever vote-bearing comments they have left (or removes
      them entirely if none remain).
    - Multiple comments per viewer: their EARLIEST vote-bearing
      comment is the one that counts. Posting "lol" then "Red" still
      counts as red. Posting "Red" then "Blue" counts as red.

Subcommands:
    scan <video_id>           Fetch new comments, parse votes, store them.
    watch <video_id>          Loop scan on an interval (default 15 min).
    tally <video_id>          Print current red/blue counts.
    voters <video_id>         List every voter (filter with --vote).
    export <video_id>         Dump votes to CSV.
    forget <video_id>         Delete all stored data for a video.
    parse-test "<text>"       Sanity-check the parser on an arbitrary string.

Quota:
    `commentThreads.list` is 1 unit per page (100 comments / page). A
    1,000-comment video costs 10 units. The default 10,000-unit daily
    quota is fine for repeatedly polling a single video.

Setup:
    `YOUTUBE_API_KEY` must be set in core/config.py (or
    core/config_local.py). No OAuth required — read-only API key.

Usage examples:
    python tools/redblue_tally.py scan abc123XYZ
    python tools/redblue_tally.py tally abc123XYZ
    python tools/redblue_tally.py voters abc123XYZ --vote red
    python tools/redblue_tally.py watch abc123XYZ --interval 600
    python tools/redblue_tally.py export abc123XYZ --out data/redblue.csv
    python tools/redblue_tally.py parse-test "I'd go red, blue is too risky"
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Tuple


# ============================================================
# PATHS / CONFIG
# ============================================================

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Force unbuffered stdout so .bat wrappers (or `watch`) show progress live.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

DB_PATH = REPO_ROOT / "data" / "redblue.db"
YT_API = "https://www.googleapis.com/youtube/v3"

# Pulled from the bot's config so we don't duplicate the API key.
try:
    from core import config as bot_config
    YOUTUBE_API_KEY = getattr(bot_config, "YOUTUBE_API_KEY", "") or ""
except Exception:
    YOUTUBE_API_KEY = ""


# Match "red" or "blue" as a whole word, case-insensitive. We grab the
# FIRST occurrence so the parser respects "what they said first".
VOTE_RE = re.compile(r"\b(red|blue)\b", re.IGNORECASE)


# ============================================================
# DB SETUP
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS redblue_comments (
    yt_video_id   TEXT NOT NULL,
    comment_id    TEXT NOT NULL,
    yt_channel_id TEXT NOT NULL,
    display_name  TEXT,
    comment_text  TEXT,
    published_at  TEXT,           -- never changes
    updated_at    TEXT,           -- changes on edit; we reparse when this differs
    parsed_vote   TEXT,           -- 'red' / 'blue' / NULL (no vote keyword)
    last_seen_at  TEXT NOT NULL,  -- most recent scan that saw this comment in the API response
    deleted_at    TEXT,           -- NULL while visible; ISO timestamp once the API stops returning it
    PRIMARY KEY (yt_video_id, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_redblue_comments_video_channel
    ON redblue_comments(yt_video_id, yt_channel_id);

CREATE INDEX IF NOT EXISTS idx_redblue_comments_video_published
    ON redblue_comments(yt_video_id, yt_channel_id, published_at);

-- Legacy tables from the v1 schema. Kept so existing DBs don't see
-- "no such table" errors during cmd_forget. New code never writes here.
CREATE TABLE IF NOT EXISTS redblue_votes (
    yt_video_id   TEXT NOT NULL,
    yt_channel_id TEXT NOT NULL,
    vote          TEXT NOT NULL CHECK(vote IN ('red', 'blue')),
    comment_id    TEXT NOT NULL,
    display_name  TEXT,
    comment_text  TEXT,
    published_at  TEXT,
    voted_at      TEXT NOT NULL,
    PRIMARY KEY (yt_video_id, yt_channel_id)
);

CREATE TABLE IF NOT EXISTS redblue_processed (
    yt_video_id TEXT NOT NULL,
    comment_id  TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (yt_video_id, comment_id)
);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


# ============================================================
# YOUTUBE API
# ============================================================

def _require_api_key():
    if not YOUTUBE_API_KEY:
        print(
            "[!] YOUTUBE_API_KEY is empty. Set it in core/config.py "
            "(or core/config_local.py).",
            file=sys.stderr,
        )
        sys.exit(2)


def _http_get_json(url: str) -> dict:
    """Tiny wrapper around urllib so we don't add a `requests` dep."""
    req = urllib.request.Request(url, headers={"User-Agent": "redblue-tally/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        # Re-raise with the body attached so callers can inspect.
        raise urllib.error.HTTPError(
            e.url, e.code, f"{e.reason}: {body[:300]}", e.headers, None
        )


def fetch_all_comments(video_id: str) -> Iterator[dict]:
    """
    Yield every top-level comment thread on `video_id`. Walks every
    page of `commentThreads.list`. Each yielded dict has:
        comment_id, channel_id, display_name, text, published_at
    Replies to top-level comments are intentionally NOT walked — most
    votes will be top-level, and replies would double-cost quota.
    """
    _require_api_key()
    page_token: Optional[str] = None
    while True:
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": "100",
            "textFormat": "plainText",
            "key": YOUTUBE_API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{YT_API}/commentThreads?{urllib.parse.urlencode(params)}"

        try:
            data = _http_get_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 403 and "commentsDisabled" in str(e):
                # Comments turned off on this video — nothing to scan.
                return
            if e.code == 404:
                raise RuntimeError(
                    f"YouTube 404 for video {video_id} — wrong ID?"
                ) from e
            raise

        for thread in data.get("items", []):
            top = thread.get("snippet", {}).get("topLevelComment", {})
            sn = top.get("snippet", {})
            cid = top.get("id")
            channel_id = (sn.get("authorChannelId") or {}).get("value")
            if not cid or not channel_id:
                # Anonymous / hidden author — skip; we can't dedupe them.
                continue
            yield {
                "comment_id": cid,
                "channel_id": channel_id,
                "display_name": sn.get("authorDisplayName", "") or "",
                # `textOriginal` is the raw text the author typed.
                # `textDisplay` may include HTML from auto-linkification.
                "text": sn.get("textOriginal") or sn.get("textDisplay") or "",
                "published_at": sn.get("publishedAt", "") or "",
                # `updatedAt` differs from `publishedAt` after an edit.
                # We use it to detect edits between scans.
                "updated_at": sn.get("updatedAt", "") or "",
            }

        page_token = data.get("nextPageToken")
        if not page_token:
            return


# ============================================================
# VOTE PARSING
# ============================================================

def parse_vote(text: str) -> Optional[str]:
    """
    Return 'red', 'blue', or None for the FIRST whole-word occurrence
    of either keyword in `text`. Case-insensitive.
    """
    m = VOTE_RE.search(text or "")
    if not m:
        return None
    return m.group(1).lower()


# ============================================================
# QUERY HELPERS
# ============================================================

_CURRENT_VOTE_PER_CHANNEL_SQL = """
WITH ranked AS (
    SELECT
        yt_channel_id,
        parsed_vote,
        comment_id,
        display_name,
        comment_text,
        published_at,
        updated_at,
        last_seen_at,
        ROW_NUMBER() OVER (
            PARTITION BY yt_channel_id
            ORDER BY published_at ASC
        ) AS rn
    FROM redblue_comments
    WHERE yt_video_id = ?
      AND deleted_at IS NULL
      AND parsed_vote IS NOT NULL
)
SELECT yt_channel_id, parsed_vote, comment_id, display_name,
       comment_text, published_at, updated_at, last_seen_at
FROM ranked
WHERE rn = 1
"""


def current_tally(conn: sqlite3.Connection, video_id: str) -> Tuple[int, int]:
    """
    Return (red_count, blue_count) for the given video.

    For each channel that has any active (non-deleted) vote-bearing
    comment on this video, count their EARLIEST one. Channels with no
    vote-bearing comment contribute nothing.
    """
    rows = conn.execute(
        f"SELECT parsed_vote, COUNT(*) FROM ({_CURRENT_VOTE_PER_CHANNEL_SQL}) "
        f"GROUP BY parsed_vote",
        (video_id,),
    ).fetchall()
    counts = dict(rows)
    return counts.get("red", 0), counts.get("blue", 0)


# ============================================================
# COMMANDS
# ============================================================

def cmd_scan(args):
    """
    Reconcile the local comment cache with YouTube's current state.

    Each scan walks every page of the video's top-level comments and:
      * UPSERTs every comment we see (new ones get inserted, already-
        seen ones get refreshed with current text + updated_at + a
        re-parsed vote keyword).
      * Marks any comment that USED to be visible but isn't in this
        response as deleted (deleted_at = now).
      * Restores any comment whose deleted_at was set in a prior scan
        but is back in the API response (rare: moderation hold/release).

    Votes are not stored separately — `current_tally()` derives them
    from this table by picking each channel's earliest active
    vote-bearing comment.

    Quota note: this walks every page on every scan because YouTube has
    no "modified since" filter on commentThreads. Cost is 1 unit per
    100 comments per scan. For a 5,000-comment video at 15-min intervals
    that's ~4,800 units/day — well under the 10k default.
    """
    conn = open_db()
    video_id = args.video_id
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Snapshot of what we currently consider "active" so we can detect
    # deletions by absence after the fetch.
    previously_active = {
        cid for (cid,) in conn.execute(
            "SELECT comment_id FROM redblue_comments "
            "WHERE yt_video_id = ? AND deleted_at IS NULL",
            (video_id,),
        )
    }
    # Cache of stored (comment_id -> updated_at) so we can detect edits
    # without doing a SELECT-per-comment inside the loop.
    stored_updated_at = {
        cid: ua for (cid, ua) in conn.execute(
            "SELECT comment_id, updated_at FROM redblue_comments "
            "WHERE yt_video_id = ?",
            (video_id,),
        )
    }

    print(f"[i] Scanning video {video_id}")
    print(
        f"[i] Local cache: {len(stored_updated_at)} comment(s), "
        f"{len(previously_active)} currently active"
    )

    api_comments = list(fetch_all_comments(video_id))
    print(f"[i] Fetched {len(api_comments)} top-level comments from YouTube")

    new_count = 0
    edit_count = 0
    unchanged_count = 0
    restored_count = 0
    no_vote_count = 0
    current_ids = set()

    for c in api_comments:
        cid = c["comment_id"]
        current_ids.add(cid)
        parsed = parse_vote(c["text"])
        if parsed is None:
            no_vote_count += 1

        prior_updated = stored_updated_at.get(cid)
        if prior_updated is None:
            new_count += 1
        elif prior_updated != c["updated_at"]:
            edit_count += 1
        else:
            unchanged_count += 1

        # If this comment was previously marked deleted but is back in
        # the response, count that as a restore for the log.
        if cid not in previously_active and prior_updated is not None:
            restored_count += 1

        # UPSERT — set deleted_at = NULL on every write since the API
        # is currently returning this comment (so by definition it's
        # not deleted right now).
        conn.execute(
            """
            INSERT INTO redblue_comments
                (yt_video_id, comment_id, yt_channel_id, display_name,
                 comment_text, published_at, updated_at, parsed_vote,
                 last_seen_at, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(yt_video_id, comment_id) DO UPDATE SET
                yt_channel_id = excluded.yt_channel_id,
                display_name  = excluded.display_name,
                comment_text  = excluded.comment_text,
                updated_at    = excluded.updated_at,
                parsed_vote   = excluded.parsed_vote,
                last_seen_at  = excluded.last_seen_at,
                deleted_at    = NULL
            """,
            (
                video_id, cid, c["channel_id"], c["display_name"],
                c["text"], c["published_at"], c["updated_at"], parsed,
                now_iso,
            ),
        )

    # Deletion detection: anything that was active before this scan but
    # didn't come back in the response.
    missing = previously_active - current_ids
    for cid in missing:
        conn.execute(
            "UPDATE redblue_comments SET deleted_at = ? "
            "WHERE yt_video_id = ? AND comment_id = ? AND deleted_at IS NULL",
            (now_iso, video_id, cid),
        )

    conn.commit()

    red_total, blue_total = current_tally(conn, video_id)
    total = red_total + blue_total

    print(
        f"[i] Comments — new: {new_count}, edited: {edit_count}, "
        f"unchanged: {unchanged_count}, restored: {restored_count}, "
        f"deleted: {len(missing)} (no-vote text: {no_vote_count})"
    )
    if total:
        rp = 100 * red_total / total
        bp = 100 * blue_total / total
        print(
            f"[+] Tally for {video_id}:  RED {red_total} ({rp:.1f}%)  |  "
            f"BLUE {blue_total} ({bp:.1f}%)  |  TOTAL {total}"
        )
    else:
        print(f"[+] Tally for {video_id}: no votes yet.")

    conn.close()


def cmd_watch(args):
    interval = max(60, args.interval)
    print(f"[i] Watching {args.video_id} every {interval}s (Ctrl+C to stop).")
    try:
        while True:
            try:
                cmd_scan(argparse.Namespace(video_id=args.video_id))
            except Exception as e:
                print(f"[!] scan failed: {e}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[i] Watch loop stopped.")


def cmd_tally(args):
    conn = open_db()
    red, blue = current_tally(conn, args.video_id)
    total = red + blue
    if total == 0:
        print(f"No votes yet for {args.video_id}.")
        if args.json:
            print(json.dumps({
                "video_id": args.video_id,
                "red": 0, "blue": 0, "total": 0,
                "red_pct": 0.0, "blue_pct": 0.0,
            }))
        return
    rp = 100 * red / total
    bp = 100 * blue / total
    print(f"Video: {args.video_id}")
    print(f"  RED  : {red:>5}  ({rp:5.1f}%)")
    print(f"  BLUE : {blue:>5}  ({bp:5.1f}%)")
    print(f"  TOTAL: {total:>5}")
    if args.json:
        print(json.dumps({
            "video_id": args.video_id,
            "red": red,
            "blue": blue,
            "total": total,
            "red_pct": rp,
            "blue_pct": bp,
        }))


def cmd_voters(args):
    conn = open_db()

    sql = (
        f"SELECT yt_channel_id, parsed_vote, comment_id, display_name, "
        f"       comment_text, published_at, updated_at, last_seen_at "
        f"FROM ({_CURRENT_VOTE_PER_CHANNEL_SQL})"
    )
    params: list = [args.video_id]
    if args.vote:
        sql += " WHERE parsed_vote = ?"
        params.append(args.vote)
    sql += " ORDER BY published_at ASC"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No matching voters.")
        return

    for chid, vote, _cid, name, text, _pub, _upd, _seen in rows:
        snippet = (text or "").replace("\n", " ").strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        print(f"  [{vote.upper():4}] {name}  ({chid})  — {snippet}")

    print(f"\n[i] {len(rows)} voter(s) shown.")


def cmd_export(args):
    conn = open_db()
    rows = conn.execute(
        f"SELECT yt_channel_id, display_name, parsed_vote, comment_id, "
        f"       comment_text, published_at, updated_at, last_seen_at "
        f"FROM ({_CURRENT_VOTE_PER_CHANNEL_SQL}) "
        f"ORDER BY published_at ASC",
        (args.video_id,),
    ).fetchall()

    out_path = (
        Path(args.out)
        if args.out
        else REPO_ROOT / "data" / f"redblue_{args.video_id}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "channel_id", "display_name", "vote", "comment_id",
            "comment_text", "published_at", "updated_at", "last_seen_at",
        ])
        for row in rows:
            w.writerow(row)

    print(f"[+] Exported {len(rows)} votes -> {out_path}")


def cmd_forget(args):
    conn = open_db()
    if not args.yes:
        confirm = input(f"Delete all data for video {args.video_id}? (y/N) ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return
    conn.execute(
        "DELETE FROM redblue_comments WHERE yt_video_id = ?", (args.video_id,)
    )
    # Sweep the legacy v1 tables too — harmless on fresh DBs (the rows
    # don't exist) and useful if upgrading from a v1 install.
    conn.execute(
        "DELETE FROM redblue_votes WHERE yt_video_id = ?", (args.video_id,)
    )
    conn.execute(
        "DELETE FROM redblue_processed WHERE yt_video_id = ?", (args.video_id,)
    )
    conn.commit()
    print(f"[+] Forgot all stored data for {args.video_id}.")


def cmd_parse_test(args):
    v = parse_vote(args.text)
    print(f"vote: {v if v else 'NONE'}")


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Tally Red/Blue votes from a YouTube video's comments.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="Fetch new comments and tally votes.")
    sp.add_argument("video_id")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("watch", help="Loop scan on an interval.")
    sp.add_argument("video_id")
    sp.add_argument(
        "--interval", type=int, default=900,
        help="Seconds between scans (default 900 = 15 min, min 60).",
    )
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("tally", help="Print current red/blue counts.")
    sp.add_argument("video_id")
    sp.add_argument(
        "--json", action="store_true",
        help="Also print a machine-readable JSON line.",
    )
    sp.set_defaults(func=cmd_tally)

    sp = sub.add_parser("voters", help="List every voter on a video.")
    sp.add_argument("video_id")
    sp.add_argument(
        "--vote", choices=["red", "blue"],
        help="Filter to only red or only blue voters.",
    )
    sp.set_defaults(func=cmd_voters)

    sp = sub.add_parser("export", help="Export votes to CSV.")
    sp.add_argument("video_id")
    sp.add_argument(
        "--out",
        help="Output CSV path. Defaults to data/redblue_<video>.csv.",
    )
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("forget", help="Delete all stored data for a video.")
    sp.add_argument("video_id")
    sp.add_argument(
        "--yes", action="store_true",
        help="Skip confirmation prompt.",
    )
    sp.set_defaults(func=cmd_forget)

    sp = sub.add_parser(
        "parse-test", help="Test the vote parser on an arbitrary string."
    )
    sp.add_argument("text")
    sp.set_defaults(func=cmd_parse_test)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
