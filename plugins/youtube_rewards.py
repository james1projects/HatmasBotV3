"""
YouTube Rewards Plugin
======================
Periodic scanner that awards stock-market shares to YouTube commenters.

Big-picture behavior:
    1. On bot startup and then on a periodic timer, hit the YouTube Data
       API and walk the most recent N uploads from the configured channel.
    2. For each video that doesn't yet have a god mapping, run the title
       parser ("Full Gameplay: X vs Y" -> "X") and store an auto-tag
       in `youtube_video_gods`. Manual tags from the CLI always win.
    3. For each video that DOES have a god, list its comments. For every
       new commenter not yet rewarded for that video, grant them
       YOUTUBE_FREE_SHARE_COUNT shares of that video's god — credited
       to their `youtube_holdings` row, keyed on YouTube channel ID.
    4. One reward per (video, channel) pair. Re-commenting on the same
       video after the first reward is silently a no-op.

Why a separate plugin (not folded into economy.py):
    Keeps the YouTube I/O loop and quota concerns isolated from the live
    Twitch-side trading code. Economy.py is responsible for all match
    lifecycle hooks (dividends, price ticks, settlement) and just gets
    extended (in plugins/economy.py) to also walk youtube_holdings when
    paying dividends. That's the only point of contact.

Dependencies:
    pip install aiohttp aiosqlite

Config:
    YOUTUBE_API_KEY            — Data API v3 key (read-only, no OAuth)
    YOUTUBE_CHANNEL_ID         — your channel ID (UCxxxx...)
    YOUTUBE_POLL_INTERVAL      — seconds between scans (default 3600)
    YOUTUBE_VIDEOS_PER_SCAN    — how many recent uploads to look at
    YOUTUBE_FREE_SHARE_COUNT   — shares per (commenter, video)

The plugin is a no-op if either YOUTUBE_API_KEY or YOUTUBE_CHANNEL_ID
is empty — drop those into config_local.py to enable.
"""

import asyncio
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:
    aiohttp = None
    print("[YouTubeRewards] WARNING: aiohttp not installed.")

try:
    import aiosqlite
except ImportError:
    aiosqlite = None
    print("[YouTubeRewards] WARNING: aiosqlite not installed.")

from core import db as _shared_db
from core.config import (
    BASE_DIR,
    ECONOMY_DB_PATH,
    ECONOMY_STARTING_PRICE,
    YOUTUBE_API_KEY,
    YOUTUBE_CHANNEL_ID,
    YOUTUBE_POLL_INTERVAL,
    YOUTUBE_VIDEOS_PER_SCAN,
    YOUTUBE_FREE_SHARE_COUNT,
    YOUTUBE_DEEP_SCAN_INTERVAL,
    YOUTUBE_DEEP_SCAN_VIDEOS,
)
from core.youtube_parser import parse_my_god, load_known_gods
from core.youtube_schema import ensure_youtube_schema


YT_API = "https://www.googleapis.com/youtube/v3"


class YouTubeRewardsPlugin:
    """Periodic scanner that awards shares to new YouTube commenters."""

    def __init__(self):
        self.bot = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._db: Optional[aiosqlite.Connection] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._known_gods: List[str] = []
        self._god_regex: Optional[re.Pattern] = None
        self._uploads_playlist_id: Optional[str] = None
        self._enabled = False
        # Epoch timestamp of last deep scan. Boot scan is always deep
        # so a long offline gap gets caught up immediately.
        self._last_deep_scan: float = 0.0

    # ──────────────────────────────────────────────────────────────────
    #   PLUGIN LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    def setup(self, bot):
        self.bot = bot
        # No chat commands — this plugin is a passive scanner.

    def _feature_enabled(self) -> bool:
        """Honor the dashboard youtube_rewards feature toggle. Defaults
        to True if no bot is attached or the toggle isn't registered."""
        if self.bot is None:
            return True
        return self.bot.is_feature_enabled("youtube_rewards")

    async def on_ready(self):
        if aiohttp is None:
            print("[YouTubeRewards] Disabled — aiohttp missing.")
            return
        if not _shared_db.is_available():
            print("[YouTubeRewards] Disabled — aiosqlite missing.")
            return
        if not YOUTUBE_API_KEY:
            print("[YouTubeRewards] Disabled — YOUTUBE_API_KEY not set in "
                  "config_local.py.")
            return
        if not YOUTUBE_CHANNEL_ID:
            print("[YouTubeRewards] Disabled — YOUTUBE_CHANNEL_ID not set "
                  "in config_local.py.")
            return

        self._enabled = True
        self.session = aiohttp.ClientSession()
        # Use the shared connection. Economy plugin owns the youtube_*
        # tables (creates them in its schema callback) but we still call
        # ensure_youtube_schema as a defense in depth — if the bot ever
        # boots with EconomyPlugin disabled but YouTubeRewards enabled,
        # we still create what we need. The CREATE IF NOT EXISTS pattern
        # makes it idempotent on the normal path.
        self._db = await _shared_db.get_db()
        if self._db is None:
            print("[YouTubeRewards] DB unavailable — init_db may not have run")
            self._enabled = False
            return
        await ensure_youtube_schema(self._db)
        await self._ensure_pending_nominations_schema()

        self._known_gods = load_known_gods(BASE_DIR)
        # Precompile one big regex that catches any canonical god name on
        # word boundaries. Building it once here (rather than once per
        # comment) keeps the scan loop cheap even on busy videos.
        self._god_regex = self._build_god_regex(self._known_gods)
        if not self._known_gods:
            print("[YouTubeRewards] WARNING: no gods loaded from "
                  "data/god_icons/. Title parsing will always fail.")

        print(f"[YouTubeRewards] Ready — {len(self._known_gods)} gods, "
              f"polling every {YOUTUBE_POLL_INTERVAL}s")

        # Boot scan + repeating poller, both in one background task.
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def cleanup(self):
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        # Shared connection is closed by main.py / core.db at shutdown.
        # Just clear our reference.
        self._db = None
        if self.session:
            await self.session.close()
            self.session = None
        if self._enabled:
            print("[YouTubeRewards] Cleaned up")

    # ──────────────────────────────────────────────────────────────────
    #   POLL LOOP
    # ──────────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        """Boot scan (always deep), then alternating regular / deep cycles.

        A 'deep' scan walks YOUTUBE_DEEP_SCAN_VIDEOS uploads (default
        250) so older videos with new comments still get processed.
        Regular scans only walk YOUTUBE_VIDEOS_PER_SCAN uploads (25)
        for cheap, frequent pickups.
        """
        # Boot scan is always deep so any backlog (e.g., bot was offline
        # overnight) gets fully caught up immediately. Skipped if the
        # youtube_rewards feature is disabled at boot — we'll pick up
        # any backlog the next time the toggle is flipped on (the
        # processed-comments dedup table makes it idempotent).
        if self._feature_enabled():
            try:
                await self._run_scan(deep=True)
                self._last_deep_scan = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[YouTubeRewards] Boot scan failed: {e}")

        while True:
            try:
                await asyncio.sleep(YOUTUBE_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise

            # Honor the dashboard youtube_rewards feature toggle. Skip
            # the scan entirely if disabled. The poll loop keeps running
            # so flipping the toggle back on resumes within
            # YOUTUBE_POLL_INTERVAL without needing a bot restart.
            if not self._feature_enabled():
                continue

            # Decide whether THIS cycle should be a deep scan.
            now = time.time()
            do_deep = (now - self._last_deep_scan) >= YOUTUBE_DEEP_SCAN_INTERVAL
            if do_deep:
                self._last_deep_scan = now

            try:
                await self._run_scan(deep=do_deep)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Don't let one bad scan kill the whole poller.
                print(f"[YouTubeRewards] Scan error: {e}")

    async def _run_scan(self, deep: bool = False):
        """One full pass: list uploads, auto-tag, scan comments, grant.

        Args:
          deep: if True, walks YOUTUBE_DEEP_SCAN_VIDEOS uploads
                (paginated). Otherwise walks YOUTUBE_VIDEOS_PER_SCAN.
        """
        if not self._enabled:
            return

        # Resolve the uploads playlist ID once and cache it.
        if self._uploads_playlist_id is None:
            try:
                self._uploads_playlist_id = await self._yt_uploads_playlist()
            except Exception as e:
                print(f"[YouTubeRewards] Could not resolve uploads "
                      f"playlist: {e}")
                return

        limit = (YOUTUBE_DEEP_SCAN_VIDEOS if deep
                 else YOUTUBE_VIDEOS_PER_SCAN)
        try:
            videos = await self._yt_recent_uploads(
                self._uploads_playlist_id, limit)
        except Exception as e:
            print(f"[YouTubeRewards] Failed to list uploads: {e}")
            return

        if deep:
            print(f"[YouTubeRewards] Deep scan: walking {len(videos)} "
                  f"videos (covers comments on older uploads)")

        # Step 1: ensure each video has a god mapping (auto-tag if needed).
        for video_id, title, _ in videos:
            await self._ensure_video_god(video_id, title)

        # Step 2: for each video that has a god, scan comments and grant.
        total_granted = 0
        for video_id, title, _ in videos:
            god = await self._get_video_god(video_id)
            if god is None:
                continue  # title couldn't auto-parse; awaits manual tag
            try:
                granted = await self._scan_video_comments(video_id, god)
                total_granted += granted
            except Exception as e:
                print(f"[YouTubeRewards] Comment scan failed for "
                      f"{video_id}: {e}")

        if total_granted:
            tag = "deep " if deep else ""
            print(f"[YouTubeRewards] {tag}Scan complete — granted "
                  f"{total_granted} new share(s) across {len(videos)} "
                  f"videos")

    # ──────────────────────────────────────────────────────────────────
    #   YOUTUBE API CALLS
    # ──────────────────────────────────────────────────────────────────

    async def _yt_uploads_playlist(self) -> str:
        url = (f"{YT_API}/channels?part=contentDetails"
               f"&id={YOUTUBE_CHANNEL_ID}&key={YOUTUBE_API_KEY}")
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            items = data.get("items", [])
            if not items:
                raise RuntimeError(
                    f"No channel found for YOUTUBE_CHANNEL_ID="
                    f"{YOUTUBE_CHANNEL_ID}")
            return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    async def _yt_recent_uploads(self, playlist_id: str, limit: int
                                 ) -> List[Tuple[str, str, str]]:
        """
        Return [(video_id, title, published_at), ...] for up to `limit`
        most recent uploads (newest first). Paginates through the
        uploads playlist with pageToken when limit > 50, so a deep
        scan covering hundreds of videos works the same as a quick
        25-video scan. Stops when the playlist runs out or the limit
        is reached.
        """
        out: List[Tuple[str, str, str]] = []
        page_token: Optional[str] = None
        while len(out) < limit:
            url = (f"{YT_API}/playlistItems?part=snippet"
                   f"&playlistId={playlist_id}"
                   f"&maxResults=50"
                   f"&key={YOUTUBE_API_KEY}")
            if page_token:
                url += f"&pageToken={page_token}"
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()

            for item in data.get("items", []):
                if len(out) >= limit:
                    break
                snippet = item.get("snippet", {})
                vid = snippet.get("resourceId", {}).get("videoId")
                title = snippet.get("title", "")
                published = snippet.get("publishedAt", "")
                if vid:
                    out.append((vid, title, published))

            page_token = data.get("nextPageToken")
            if not page_token:
                break  # ran out of videos in the playlist
        return out

    async def _yt_list_comments(self, video_id: str
                                ) -> List[Tuple[str, str, str, str]]:
        """
        Return [(comment_id, channel_id, display_name, text), ...] for
        every top-level comment on the video. We walk every page so a
        popular video doesn't lose comments off the back end.

        `text` is the plain-text body of the comment (textOriginal),
        used by the god-name scanner to queue pending nominations. The
        legacy share-grant flow ignores it.

        Some videos disable comments (HTTP 403 with reason
        commentsDisabled). We swallow that and return an empty list.
        """
        out: List[Tuple[str, str, str, str]] = []
        page_token: Optional[str] = None
        while True:
            url = (f"{YT_API}/commentThreads?part=snippet"
                   f"&videoId={video_id}&maxResults=100"
                   f"&textFormat=plainText"
                   f"&key={YOUTUBE_API_KEY}")
            if page_token:
                url += f"&pageToken={page_token}"
            async with self.session.get(url) as resp:
                if resp.status == 403:
                    # commentsDisabled or quotaExceeded — surface and stop.
                    body = await resp.text()
                    if "commentsDisabled" in body:
                        return out
                    raise RuntimeError(f"YouTube 403: {body[:200]}")
                resp.raise_for_status()
                data = await resp.json()

            for thread in data.get("items", []):
                snippet = (thread.get("snippet", {})
                                  .get("topLevelComment", {})
                                  .get("snippet", {}))
                comment_id = (thread.get("snippet", {})
                                    .get("topLevelComment", {})
                                    .get("id"))
                channel_id = snippet.get("authorChannelId", {}).get("value")
                display = snippet.get("authorDisplayName", "")
                text = snippet.get("textOriginal") or snippet.get("textDisplay", "")
                if comment_id and channel_id:
                    out.append((comment_id, channel_id, display, text))

            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out

    # ──────────────────────────────────────────────────────────────────
    #   DB: VIDEO -> GOD MAPPING
    # ──────────────────────────────────────────────────────────────────

    async def _get_video_god(self, video_id: str) -> Optional[str]:
        async with self._db.execute(
                "SELECT god_name FROM youtube_video_gods "
                "WHERE yt_video_id = ?", (video_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def _ensure_video_god(self, video_id: str, title: str):
        """
        If `video_id` has no god mapping yet, try to parse one from the
        title. Always preserves any existing entry (manual tags from the
        CLI must win). Auto-fills with set_by='auto'.
        """
        existing = await self._get_video_god(video_id)
        if existing is not None:
            return  # already mapped; don't touch

        god = parse_my_god(title, self._known_gods)
        if god is None:
            return  # parser couldn't resolve; awaits manual tag

        # INSERT-only with ON CONFLICT DO NOTHING for safety against a
        # race where another writer (e.g. CLI) just inserted a manual tag.
        await self._db.execute("""
            INSERT INTO youtube_video_gods
                (yt_video_id, god_name, title, set_at, set_by)
            VALUES (?, ?, ?, datetime('now'), 'auto')
            ON CONFLICT(yt_video_id) DO NOTHING
        """, (video_id, god, title))
        await self._db.commit()
        print(f"[YouTubeRewards] auto-tagged {video_id} -> {god}")

    # ──────────────────────────────────────────────────────────────────
    #   DB: GRANT SHARES TO NEW COMMENTERS
    # ──────────────────────────────────────────────────────────────────

    async def _scan_video_comments(self, video_id: str, god: str) -> int:
        """
        Walk every comment on `video_id`. For each (video_id, channel_id)
        pair not yet in `youtube_processed_comments`, grant
        YOUTUBE_FREE_SHARE_COUNT shares of `god` and mark the pair
        processed. Returns the count of NEW grants made.
        """
        # Pull existing processed channel_ids in one query so we can
        # filter without per-row round-trips.
        already: set = set()
        async with self._db.execute(
                "SELECT yt_channel_id FROM youtube_processed_comments "
                "WHERE yt_video_id = ?", (video_id,)) as cur:
            async for row in cur:
                already.add(row[0])

        comments = await self._yt_list_comments(video_id)
        if not comments:
            return 0

        # Look up the current price once for cost-basis recording. If the
        # god has never been priced (new), use the starting price.
        price = await self._get_god_price(god)

        granted = 0
        for comment_id, channel_id, display_name, text in comments:
            # Scan EVERY comment's text for god-name mentions (independent
            # of whether this commenter has already received shares for
            # this video). A regular who comments on every video might
            # mention different gods each time — each is a candidate
            # nomination. INSERT OR IGNORE dedups via the UNIQUE index.
            await self._maybe_queue_pending_nomination(
                video_id, comment_id, channel_id, display_name, text)

            if channel_id in already:
                continue

            # Insert/update portfolio metadata first (display name).
            await self._upsert_portfolio(channel_id, display_name)

            # Grant the shares (idempotent — uses INSERT...ON CONFLICT
            # to add to existing holdings).
            await self._grant_shares(
                channel_id, god, YOUTUBE_FREE_SHARE_COUNT, price,
                video_id=video_id, txn_type="comment_share")

            # Mark this (video, channel) pair processed.
            await self._db.execute("""
                INSERT INTO youtube_processed_comments
                    (yt_video_id, yt_channel_id, comment_id, granted_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(yt_video_id, yt_channel_id) DO NOTHING
            """, (video_id, channel_id, comment_id))

            granted += 1
            already.add(channel_id)  # in-memory dedup for this scan

        await self._db.commit()
        return granted

    # ──────────────────────────────────────────────────────────────────
    #   PENDING NOMINATIONS (god-name scan from comments)
    # ──────────────────────────────────────────────────────────────────
    #
    # Side channel to the share-rewards flow. Whenever the scanner walks
    # a comment, we also fuzzy-match its plain-text body against the
    # canonical god name list. Any hit becomes a pending row that
    # Hatmaster can approve from /community — approval routes through
    # GodPool's vote schema (god_pool + god_pool_votes) so the YT
    # commenter becomes a "voter" in the same pool viewers feed via
    # !nominate. Strict canonical-name matching for now; an alias map
    # can layer on top later without changing this code path.

    async def _ensure_pending_nominations_schema(self):
        """Create the pending_yt_nominations table on first launch.
        Idempotent via CREATE IF NOT EXISTS."""
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS pending_yt_nominations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                yt_video_id     TEXT    NOT NULL,
                yt_comment_id   TEXT    NOT NULL,
                yt_channel_id   TEXT    NOT NULL,
                yt_display_name TEXT    NOT NULL,
                god_name        TEXT    NOT NULL,
                comment_snippet TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                decided_at      TEXT,
                UNIQUE (yt_channel_id, god_name, yt_video_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pending_yt_status
                ON pending_yt_nominations (status, created_at DESC);
        """)
        await self._db.commit()

    @staticmethod
    def _build_god_regex(gods: List[str]) -> Optional[re.Pattern]:
        """
        Compile a case-insensitive regex matching ANY canonical god name
        on word boundaries. Returns None if `gods` is empty.

        We sort by length DESC so multi-word names like 'Ah Muzen Cab'
        win over their shorter substrings ('Cab' alone is never a god,
        but it's a useful precaution for future name additions). The
        re.escape handles apostrophes ('Chang'e') and spaces correctly.
        """
        if not gods:
            return None
        sorted_gods = sorted(gods, key=len, reverse=True)
        alt = "|".join(re.escape(g) for g in sorted_gods)
        # \b is word-char/non-word boundary. Since god names end in
        # letters (word chars), trailing \b works. For names ending in
        # apostrophe-letter ('Chang'e'), the last char 'e' is a word
        # char so \b still anchors cleanly.
        return re.compile(r"\b(" + alt + r")\b", re.IGNORECASE)

    def _scan_comment_for_god(self, text: str) -> Optional[str]:
        """Return the canonical-cased god name found in `text`, or None.

        Only the FIRST hit is returned — comments that mention multiple
        gods generate one pending row for the first match. (We could
        generate one per match later; for now this keeps the approval
        queue from getting flooded by a single chatty comment.)
        """
        if not text or self._god_regex is None:
            return None
        m = self._god_regex.search(text)
        if not m:
            return None
        # Normalize back to canonical casing from the known-gods list.
        hit_lower = m.group(1).lower()
        for g in self._known_gods:
            if g.lower() == hit_lower:
                return g
        return None

    async def _maybe_queue_pending_nomination(
            self, video_id: str, comment_id: str,
            channel_id: str, display_name: str, text: str):
        """If `text` mentions a known god, queue a pending nomination.

        Dedup is enforced by the UNIQUE(yt_channel_id, god_name,
        yt_video_id) index — same commenter saying the same god on
        the same video twice is a no-op. Same commenter saying the
        same god on a DIFFERENT video produces a new row (different
        approval opportunity).
        """
        god = self._scan_comment_for_god(text)
        if not god:
            return

        # Snippet stored for the approval UI — capped so a wall-of-text
        # comment doesn't bloat the DB. We trim to ~280 chars (tweet-
        # length) which is plenty of context for the moderator.
        snippet = (text or "").strip().replace("\r", " ").replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."

        await self._db.execute("""
            INSERT INTO pending_yt_nominations
                (yt_video_id, yt_comment_id, yt_channel_id,
                 yt_display_name, god_name, comment_snippet, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(yt_channel_id, god_name, yt_video_id) DO NOTHING
        """, (video_id, comment_id, channel_id,
              display_name, god, snippet))

    # ──────────────────────────────────────────────────────────────────
    #   ECONOMY HOOKS (price lookup, portfolio metadata, grants)
    # ──────────────────────────────────────────────────────────────────

    async def _get_god_price(self, god: str) -> float:
        """
        Return the current price for `god`, creating the god_prices row
        on first reference. The economy plugin only creates that row
        when YOU play the god on stream (via _ensure_god_exists). But
        a YouTube viewer might earn a share of a god you've never
        played — we still want their portfolio to render with a real
        price, so we INSERT OR IGNORE here.

        Also seeds price_history with an 'ipo' entry on first creation,
        matching economy.py's _ensure_god_exists shape so any later
        sparkline lookup has a valid first data point.
        """
        cur = await self._db.execute("""
            INSERT OR IGNORE INTO god_prices (god_name, price)
            VALUES (?, ?)
        """, (god, ECONOMY_STARTING_PRICE))
        if cur.rowcount > 0:
            await self._db.execute("""
                INSERT INTO price_history (god_name, price, event)
                VALUES (?, ?, 'ipo')
            """, (god, ECONOMY_STARTING_PRICE))

        async with self._db.execute(
                "SELECT price FROM god_prices WHERE god_name = ?",
                (god,)) as cur2:
            row = await cur2.fetchone()
        return float(row[0]) if row else float(ECONOMY_STARTING_PRICE)

    async def _upsert_portfolio(self, channel_id: str, display_name: str):
        await self._db.execute("""
            INSERT INTO youtube_portfolios
                (yt_channel_id, yt_display_name, first_seen_at, last_seen_at)
            VALUES (?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(yt_channel_id) DO UPDATE SET
                yt_display_name = excluded.yt_display_name,
                last_seen_at    = excluded.last_seen_at
        """, (channel_id, display_name))

    async def _grant_shares(self, channel_id: str, god: str,
                            shares: float, price: float,
                            video_id: Optional[str] = None,
                            txn_type: str = "comment_share"):
        """
        Add `shares` of `god` to the channel's holdings, updating
        avg_cost as a weighted average. Records a row in
        `youtube_transactions` for history.
        """
        # Read current position to compute new avg_cost.
        async with self._db.execute("""
            SELECT shares, avg_cost FROM youtube_holdings
             WHERE yt_channel_id = ? AND god_name = ?
        """, (channel_id, god)) as cur:
            row = await cur.fetchone()

        if row is None:
            old_shares, old_avg = 0.0, 0.0
        else:
            old_shares, old_avg = float(row[0]), float(row[1])

        new_shares = old_shares + shares
        if new_shares > 0:
            # Weighted-average cost basis.
            new_avg = ((old_shares * old_avg) + (shares * price)) / new_shares
        else:
            new_avg = 0.0

        await self._db.execute("""
            INSERT INTO youtube_holdings
                (yt_channel_id, god_name, shares, avg_cost)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(yt_channel_id, god_name) DO UPDATE SET
                shares   = excluded.shares,
                avg_cost = excluded.avg_cost
        """, (channel_id, god, new_shares, new_avg))

        await self._db.execute("""
            INSERT INTO youtube_transactions
                (yt_channel_id, god_name, type, shares, price, yt_video_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (channel_id, god, txn_type, shares, price, video_id))
