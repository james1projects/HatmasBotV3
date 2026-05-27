"""
mark_youtube_video.py — manage YouTube → god mappings for HatmasBot
====================================================================

Three modes:

    set <video_id> <god>      Manually tag a video with a god. Always
                              wins over auto-tags. Use this for mixed
                              sessions or when the title parser fails.

    --auto-scan               Fetch every upload from your channel,
                              parse titles via the standard "Full
                              Gameplay: X vs Y" convention, and fill
                              `youtube_video_gods` for any video the
                              parser can confidently resolve. Existing
                              entries are NOT overwritten unless
                              --overwrite is also passed.

    --list-untagged           List every YouTube upload that has no
                              entry in `youtube_video_gods` yet — i.e.
                              videos awaiting a manual tag because the
                              title parser couldn't auto-fill them.

    --list-tagged             Dump everything currently in the table,
                              for debugging.

Examples:

    python tools/mark_youtube_video.py set abc123 Ymir
    python tools/mark_youtube_video.py --auto-scan
    python tools/mark_youtube_video.py --auto-scan --overwrite
    python tools/mark_youtube_video.py --list-untagged

Requirements:
    pip install aiosqlite aiohttp
    Set YOUTUBE_API_KEY and YOUTUBE_CHANNEL_ID in core/config_local.py.

The api key only needs read-only access to public YouTube data, so a
plain API key (no OAuth) is enough. Default daily quota of 10,000 units
is far more than this CLI needs (a full --auto-scan costs maybe 5-10
units depending on upload count).
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Allow running this script from anywhere — sys.path inject the repo root
# so `from core...` imports resolve.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import aiohttp
    import aiosqlite
except ImportError as e:
    print(f"Missing dependency: {e}\n"
          f"Install with: pip install aiohttp aiosqlite")
    sys.exit(1)

from core.config import (
    BASE_DIR,
    ECONOMY_DB_PATH,
    YOUTUBE_API_KEY,
    YOUTUBE_CHANNEL_ID,
)
from core.youtube_parser import (
    parse_my_god,
    resolve_god,
    load_known_gods as load_known_gods_from_disk,
)
from core.youtube_schema import ensure_youtube_schema


# ─── YouTube API helpers ───────────────────────────────────────────────

YT_API = "https://www.googleapis.com/youtube/v3"


async def yt_get_uploads_playlist_id(session: aiohttp.ClientSession,
                                      channel_id: str) -> str:
    """Resolve a channel ID to its 'uploads' playlist ID. Costs 1 quota unit."""
    url = (f"{YT_API}/channels?part=contentDetails"
           f"&id={channel_id}&key={YOUTUBE_API_KEY}")
    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.json()
        items = data.get("items", [])
        if not items:
            raise RuntimeError(
                f"No channel found with ID {channel_id}. "
                f"Check YOUTUBE_CHANNEL_ID in config_local.py.")
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


async def yt_list_uploads(session: aiohttp.ClientSession,
                          playlist_id: str) -> List[Tuple[str, str]]:
    """
    Walk every page of the uploads playlist. Returns list of
    (video_id, title) tuples in upload order (newest first).
    Costs 1 quota unit per page (50 videos per page).
    """
    out: List[Tuple[str, str]] = []
    page_token: Optional[str] = None
    while True:
        url = (f"{YT_API}/playlistItems?part=snippet"
               f"&playlistId={playlist_id}&maxResults=50"
               f"&key={YOUTUBE_API_KEY}")
        if page_token:
            url += f"&pageToken={page_token}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            vid = snippet.get("resourceId", {}).get("videoId")
            title = snippet.get("title", "")
            if vid:
                out.append((vid, title))

        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out


# ─── God list discovery ────────────────────────────────────────────────

async def load_known_gods(db: Optional[aiosqlite.Connection] = None
                          ) -> List[str]:
    """
    Build the canonical list of proper-cased god names by combining:

      1. data/god_icons/   (tracker.gg CDN icons, clean kebab-case stems
                            converted to title case — "hou-yi" -> "Hou Yi")
      2. god_prices table  (any gods already encountered live, in case
                            data/god_icons/ is missing some).

    Pass `db` to merge in DB-known gods. Without `db`, returns just
    whatever's on disk.
    """
    gods = set(load_known_gods_from_disk(BASE_DIR))

    if db is not None:
        async with db.execute("SELECT god_name FROM god_prices") as cur:
            async for row in cur:
                gods.add(row[0])

    return sorted(gods)


# ─── DB helpers ────────────────────────────────────────────────────────

async def db_get_tagged_video_ids(db: aiosqlite.Connection) -> set:
    out = set()
    async with db.execute(
            "SELECT yt_video_id FROM youtube_video_gods") as cur:
        async for row in cur:
            out.add(row[0])
    return out


async def db_upsert_video_god(db: aiosqlite.Connection, video_id: str,
                              god: str, title: str, set_by: str,
                              overwrite: bool = False) -> bool:
    """
    Insert or update a (video_id -> god) mapping. Returns True if a row
    was actually written/updated, False otherwise.

    Precedence rules:
      * `set_by='manual'` writes always win (the `set` CLI subcommand uses
         this and always passes overwrite=True).
      * `set_by='auto'` writes from --auto-scan never clobber a manual
         entry — even with --overwrite, the WHERE clause protects them.
      * Without --overwrite, no conflict is ever updated (INSERT-only).
    """
    if set_by == "manual":
        # `set` CLI: always wins, even over an existing manual entry.
        await db.execute("""
            INSERT INTO youtube_video_gods
                (yt_video_id, god_name, title, set_at, set_by)
            VALUES (?, ?, ?, datetime('now'), 'manual')
            ON CONFLICT(yt_video_id) DO UPDATE SET
                god_name = excluded.god_name,
                title    = excluded.title,
                set_at   = excluded.set_at,
                set_by   = 'manual'
        """, (video_id, god, title))
        await db.commit()
        return True

    if overwrite:
        # --auto-scan --overwrite: replace existing AUTO entries only.
        # Manual entries are left untouched by the WHERE clause.
        cur = await db.execute("""
            INSERT INTO youtube_video_gods
                (yt_video_id, god_name, title, set_at, set_by)
            VALUES (?, ?, ?, datetime('now'), 'auto')
            ON CONFLICT(yt_video_id) DO UPDATE SET
                god_name = excluded.god_name,
                title    = excluded.title,
                set_at   = excluded.set_at,
                set_by   = 'auto'
            WHERE youtube_video_gods.set_by = 'auto'
        """, (video_id, god, title))
        await db.commit()
        return cur.rowcount > 0

    # --auto-scan (no overwrite): fill empty slots only.
    cur = await db.execute("""
        INSERT INTO youtube_video_gods
            (yt_video_id, god_name, title, set_at, set_by)
        VALUES (?, ?, ?, datetime('now'), 'auto')
        ON CONFLICT(yt_video_id) DO NOTHING
    """, (video_id, god, title))
    await db.commit()
    return cur.rowcount > 0


# ─── Subcommand: set ───────────────────────────────────────────────────

async def cmd_set(video_id: str, god: str) -> int:
    async with aiosqlite.connect(str(ECONOMY_DB_PATH)) as db:
        await ensure_youtube_schema(db)
        await db.execute("PRAGMA foreign_keys=ON")
        known = await load_known_gods(db)
        if not known:
            print("Could not load god list. Check that data/god_icons/ "
                  "exists or that god_prices has rows.")
            return 1

        resolved = resolve_god(god, known)
        if not resolved:
            print(f"Unknown god: '{god}'. Did you mean one of these?")
            lower = god.lower()
            suggestions = [g for g in known if lower in g.lower()][:5]
            if not suggestions:
                suggestions = known[:5]
            for s in suggestions:
                print(f"  - {s}")
            return 1

        wrote = await db_upsert_video_god(
            db, video_id, resolved, title="",
            set_by="manual", overwrite=True)
    print(f"{'Updated' if wrote else 'Set'} {video_id} -> {resolved}")
    return 0


# ─── Subcommand: --auto-scan ───────────────────────────────────────────

async def cmd_auto_scan(overwrite: bool) -> int:
    if not YOUTUBE_API_KEY:
        print("YOUTUBE_API_KEY is empty. Set it in config_local.py first.")
        return 1
    if not YOUTUBE_CHANNEL_ID:
        print("YOUTUBE_CHANNEL_ID is empty. Set it in config_local.py first.")
        return 1

    print(f"Fetching uploads for channel {YOUTUBE_CHANNEL_ID}...")
    async with aiohttp.ClientSession() as session:
        try:
            uploads_id = await yt_get_uploads_playlist_id(
                session, YOUTUBE_CHANNEL_ID)
            videos = await yt_list_uploads(session, uploads_id)
        except aiohttp.ClientResponseError as e:
            print(f"YouTube API error {e.status}: {e.message}")
            if e.status == 403:
                print("  Quota exceeded or API key invalid/restricted.")
            return 1

    print(f"Found {len(videos)} uploaded videos.")

    matched = 0
    skipped_existing = 0
    unmatched: List[Tuple[str, str]] = []

    async with aiosqlite.connect(str(ECONOMY_DB_PATH)) as db:
        await ensure_youtube_schema(db)
        await db.execute("PRAGMA foreign_keys=ON")
        known = await load_known_gods(db)
        if not known:
            print("Could not load god list. Check data/god_icons/.")
            return 1

        for video_id, title in videos:
            god = parse_my_god(title, known)
            if god is None:
                unmatched.append((video_id, title))
                continue
            wrote = await db_upsert_video_god(
                db, video_id, god, title=title,
                set_by="auto", overwrite=overwrite)
            if wrote:
                matched += 1
                print(f"  [tag] {video_id}  ->  {god}   ({title})")
            else:
                skipped_existing += 1

    print()
    print(f"Tagged:           {matched}")
    print(f"Skipped (existing): {skipped_existing}")
    print(f"Unmatched titles:  {len(unmatched)}")
    if unmatched:
        print()
        print("Run --list-untagged to see them, or use:")
        print("  python tools/mark_youtube_video.py set <video_id> <god>")
        print("for any you want to tag manually.")
    return 0


# ─── Subcommand: --list-untagged ───────────────────────────────────────

async def cmd_list_untagged() -> int:
    if not YOUTUBE_API_KEY or not YOUTUBE_CHANNEL_ID:
        print("Need YOUTUBE_API_KEY and YOUTUBE_CHANNEL_ID set in "
              "config_local.py to enumerate uploads.")
        return 1

    async with aiohttp.ClientSession() as session:
        try:
            uploads_id = await yt_get_uploads_playlist_id(
                session, YOUTUBE_CHANNEL_ID)
            videos = await yt_list_uploads(session, uploads_id)
        except aiohttp.ClientResponseError as e:
            print(f"YouTube API error {e.status}: {e.message}")
            return 1

    async with aiosqlite.connect(str(ECONOMY_DB_PATH)) as db:
        await ensure_youtube_schema(db)
        tagged = await db_get_tagged_video_ids(db)

    untagged = [(vid, title) for (vid, title) in videos if vid not in tagged]
    if not untagged:
        print("All uploads are tagged. Nothing to do.")
        return 0

    print(f"{len(untagged)} untagged uploads (newest first):")
    for vid, title in untagged:
        print(f"  {vid}   {title}")
    print()
    print("Tag any of these with:")
    print("  python tools/mark_youtube_video.py set <video_id> <god>")
    return 0


# ─── Subcommand: --list-tagged ─────────────────────────────────────────

async def cmd_list_tagged() -> int:
    rows = []
    async with aiosqlite.connect(str(ECONOMY_DB_PATH)) as db:
        await ensure_youtube_schema(db)
        async with db.execute(
                "SELECT yt_video_id, god_name, set_by, title "
                "FROM youtube_video_gods ORDER BY set_at DESC") as cur:
            async for row in cur:
                rows.append(row)
    if not rows:
        print("No videos tagged yet.")
        return 0
    print(f"{len(rows)} tagged videos:")
    for vid, god, set_by, title in rows:
        flag = "[manual]" if set_by == "manual" else "[auto]  "
        print(f"  {flag} {vid}   {god}   ({title or '?'})")
    return 0


# ─── Subcommand: --scan-comments ───────────────────────────────────────

async def cmd_scan_comments() -> int:
    """
    Run one comment-scan + share-grant pass without launching the bot.
    Reuses plugins/youtube_rewards.py's logic by instantiating the
    plugin, manually wiring up its DB and HTTP session, calling
    _run_scan(), then cleaning up. Skips the periodic poll loop —
    purely a one-shot on-demand operation.

    Useful when:
      * You want to award shares for new YouTube comments NOW without
        restarting the bot or waiting up to an hour for the next poll.
      * Sanity-checking the YouTube wiring on a fresh deploy.
    """
    if not YOUTUBE_API_KEY:
        print("YOUTUBE_API_KEY is empty. Set it in config_local.py first.")
        return 1
    if not YOUTUBE_CHANNEL_ID:
        print("YOUTUBE_CHANNEL_ID is empty. Set it in config_local.py first.")
        return 1

    # Import here so the rest of the CLI doesn't pay the import cost.
    from plugins.youtube_rewards import YouTubeRewardsPlugin

    plugin = YouTubeRewardsPlugin()

    # Manually init the bits on_ready() would set up — minus the poll
    # task, since we only want one scan.
    plugin._enabled = True
    plugin.session = aiohttp.ClientSession()
    plugin._db = await aiosqlite.connect(str(ECONOMY_DB_PATH))
    await plugin._db.execute("PRAGMA journal_mode=WAL")
    await ensure_youtube_schema(plugin._db)
    plugin._known_gods = load_known_gods_from_disk(BASE_DIR)

    if not plugin._known_gods:
        print("Warning: no gods loaded from data/god_icons/. "
              "Title parsing will always fail until you have icons.")

    print("Starting scan…")
    try:
        await plugin._run_scan()
        print("Scan complete. (Granted shares are printed above if any.)")
        return 0
    except Exception as e:
        print(f"Scan failed: {e}")
        return 1
    finally:
        if plugin._db:
            await plugin._db.close()
        if plugin.session:
            await plugin.session.close()


# ─── Subcommand: --stats ───────────────────────────────────────────────

async def cmd_stats() -> int:
    """
    Diagnostic dump of the YouTube portfolio system. Useful when a
    commenter you expected to see isn't showing up — quickly tells you
    whether (a) the video was tagged, (b) the comment was processed,
    (c) the portfolio row was created.
    """
    async with aiosqlite.connect(str(ECONOMY_DB_PATH)) as db:
        await ensure_youtube_schema(db)

        async def count(sql, *params):
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
            return row[0] if row else 0

        n_tagged = await count("SELECT COUNT(*) FROM youtube_video_gods")
        n_manual = await count(
            "SELECT COUNT(*) FROM youtube_video_gods WHERE set_by='manual'")
        n_portfolios = await count("SELECT COUNT(*) FROM youtube_portfolios")
        n_holdings = await count(
            "SELECT COUNT(*) FROM youtube_holdings WHERE shares > 0.001")
        n_processed = await count(
            "SELECT COUNT(*) FROM youtube_processed_comments")
        n_txns = await count("SELECT COUNT(*) FROM youtube_transactions")

        print("─── YouTube portfolio system stats ───")
        print(f"Videos tagged:      {n_tagged} ({n_manual} manual, "
              f"{n_tagged - n_manual} auto)")
        print(f"Portfolios:         {n_portfolios}")
        print(f"Active holdings:    {n_holdings}")
        print(f"Processed comments: {n_processed}")
        print(f"Total transactions: {n_txns}")
        print()

        if n_tagged > 0:
            print("Most recently tagged videos:")
            async with db.execute("""
                SELECT yt_video_id, god_name, set_by, title
                  FROM youtube_video_gods
                 ORDER BY set_at DESC LIMIT 10
            """) as cur:
                async for r in cur:
                    flag = "[manual]" if r[2] == "manual" else "[auto]  "
                    print(f"  {flag} {r[0]}   {r[1]}   ({r[3] or '?'})")
            print()

        if n_portfolios > 0:
            print("Most recent portfolios:")
            async with db.execute("""
                SELECT yt_channel_id, yt_display_name, last_seen_at
                  FROM youtube_portfolios
                 ORDER BY last_seen_at DESC LIMIT 10
            """) as cur:
                async for r in cur:
                    print(f"  {r[1]}  ({r[0]})  last seen {r[2]}")
            print()

        if n_txns > 0:
            print("Most recent share grants:")
            async with db.execute("""
                SELECT t.timestamp, p.yt_display_name, t.god_name,
                       t.type, t.shares, t.yt_video_id
                  FROM youtube_transactions t
                  LEFT JOIN youtube_portfolios p
                    ON p.yt_channel_id = t.yt_channel_id
                 ORDER BY t.timestamp DESC LIMIT 10
            """) as cur:
                async for r in cur:
                    name = r[1] or "?"
                    print(f"  {r[0]}  {name}  +{r[4]:.3f} {r[2]}  "
                          f"({r[3]}, video {r[5] or '-'})")
            print()

    return 0


# ─── Argparse + entrypoint ─────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Manage YouTube video -> god mappings for HatmasBot.")
    sub = p.add_subparsers(dest="cmd")

    p_set = sub.add_parser("set",
        help="Manually tag a video with a god (always wins over auto-tags).")
    p_set.add_argument("video_id", help="YouTube video ID (the v= part of a URL)")
    p_set.add_argument("god", help="Proper-cased god name, e.g. Ymir")

    p.add_argument("--auto-scan", action="store_true",
        help="Fetch every upload, parse titles, and fill the table.")
    p.add_argument("--overwrite", action="store_true",
        help="With --auto-scan: overwrite existing entries (incl. manual tags).")
    p.add_argument("--list-untagged", action="store_true",
        help="List YouTube uploads that aren't tagged yet.")
    p.add_argument("--list-tagged", action="store_true",
        help="List every video currently in the youtube_video_gods table.")
    p.add_argument("--scan-comments", action="store_true",
        help="Run one YouTube comment scan + share-grant pass right now "
             "(same logic as the bot's hourly poll, on-demand).")
    p.add_argument("--stats", action="store_true",
        help="Diagnostic: dump tagged-video, portfolio, holdings, and "
             "recent-grant counts to confirm what the system actually saw.")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == "set":
        return asyncio.run(cmd_set(args.video_id, args.god))
    if args.auto_scan:
        return asyncio.run(cmd_auto_scan(args.overwrite))
    if args.list_untagged:
        return asyncio.run(cmd_list_untagged())
    if args.list_tagged:
        return asyncio.run(cmd_list_tagged())
    if args.scan_comments:
        return asyncio.run(cmd_scan_comments())
    if args.stats:
        return asyncio.run(cmd_stats())

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
