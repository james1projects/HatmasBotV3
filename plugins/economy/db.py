"""
plugins/economy/db.py
=====================
Schema ownership + price-cache management for the economy plugin.

This mixin owns:
  * `_init_schema(conn)` — registered with `core.db.register_schema()`
    in plugin.py:setup(). Runs exactly once before any plugin's
    on_ready, receives the shared aiosqlite.Connection, and creates
    every economy-side table (god_prices, price_history, portfolios,
    transactions, dividends, processed_matches + the youtube_*
    cluster).
  * `_migrate_god_prices_kda_columns()` — idempotent column-add
    migrations driven by `PRAGMA table_info`.
  * `_load_prices()` / `_get_recent_prices()` / `_update_price()` /
    `_ensure_god_exists()` — the in-memory price cache that mirrors
    the god_prices table for fast read access during live ticks.

The shared aiosqlite connection lifecycle is owned by `core/db.py`
and `main.py`. This mixin uses `self._db` (set by `_init_schema`) but
NEVER calls `self._db.close()`.
"""

from __future__ import annotations

from typing import List

from core.config import ECONOMY_PRICE_FLOOR, ECONOMY_STARTING_PRICE
from core.youtube_schema import YOUTUBE_SCHEMA_SQL


# Number of price points kept in the in-memory sparkline cache. Used by
# both the cache writer (_update_price truncates to this length) and
# the reader (_get_recent_prices defaults its LIMIT to this).
SPARKLINE_LENGTH = 20


class _DBMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self._db                 (shared aiosqlite.Connection)
      self._prices             dict[god_name -> current_price]
      self._games_played       dict[god_name -> total_games]
      self._price_history      dict[god_name -> [recent_prices]]
      self._session_changes    dict[god_name -> session_pct_change]
    All set up in EconomyPlugin.__init__.
    """

    async def _init_schema(self, conn):
        """
        Schema callback registered with core.db. Runs exactly once,
        receives the shared aiosqlite.Connection, and creates / migrates
        every table the economy plugin owns.

        Stores the connection on self._db so the rest of the plugin can
        keep using self._db (avoids a sweeping rename across 30+ call
        sites). The connection's lifecycle is owned by core.db /
        main.py — DO NOT close it from anywhere in this file.
        """
        self._db = conn

        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS god_prices (
                god_name     TEXT PRIMARY KEY,
                price        REAL NOT NULL DEFAULT 100.0,
                games_played INTEGER NOT NULL DEFAULT 0,
                total_wins   INTEGER NOT NULL DEFAULT 0,
                total_losses INTEGER NOT NULL DEFAULT 0,
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                god_name  TEXT NOT NULL,
                price     REAL NOT NULL,
                event     TEXT NOT NULL DEFAULT 'update',
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (god_name) REFERENCES god_prices(god_name)
            );

            CREATE INDEX IF NOT EXISTS idx_price_history_god
                ON price_history(god_name, timestamp DESC);

            CREATE TABLE IF NOT EXISTS portfolios (
                username  TEXT NOT NULL,
                god_name  TEXT NOT NULL,
                shares    REAL NOT NULL DEFAULT 0,
                avg_cost  REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (username, god_name),
                FOREIGN KEY (god_name) REFERENCES god_prices(god_name)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                username  TEXT NOT NULL,
                god_name  TEXT NOT NULL,
                type      TEXT NOT NULL,  -- 'buy', 'sell', 'dividend', 'free_share'
                shares    REAL NOT NULL,
                price     REAL NOT NULL,
                total     REAL NOT NULL,
                fee       REAL NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_user
                ON transactions(username, timestamp DESC);

            CREATE TABLE IF NOT EXISTS dividends (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                god_name    TEXT NOT NULL,
                rate        REAL NOT NULL,
                price       REAL NOT NULL,
                total_hats  REAL NOT NULL,
                holders     INTEGER NOT NULL,
                match_id    TEXT DEFAULT NULL,  -- tracker.gg match this dividend is for; used by backfill to avoid double-paying
                timestamp   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_dividends_match
                ON dividends(match_id);

            -- ─── Match-settlement dedup ────────────────────────────────────
            -- Every match that has had its win/loss + KDA settlement
            -- applied to god prices lands here, keyed on tracker.gg's
            -- match_id. Used by settle_match() to prevent double-counting
            -- and by the on-launch backfill to know what's already done.
            -- `source` is 'live' (settled while bot was running through
            -- the prediction-resolve callback) or 'backfill' (settled on
            -- a later bot launch from tracker.gg history) — same price
            -- math either way, just lets us trace where each entry came
            -- from for debugging.
            CREATE TABLE IF NOT EXISTS processed_matches (
                match_id           TEXT PRIMARY KEY,
                god_name           TEXT NOT NULL,
                outcome            TEXT NOT NULL,
                kills              INTEGER NOT NULL DEFAULT 0,
                deaths             INTEGER NOT NULL DEFAULT 0,
                assists            INTEGER NOT NULL DEFAULT 0,
                price_change       REAL NOT NULL DEFAULT 0,
                source             TEXT NOT NULL DEFAULT 'live',
                was_live_at_settle INTEGER NOT NULL DEFAULT 0,  -- 1 if broadcaster was live on Twitch when this row was written; used for audit / dashboard
                processed_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_processed_matches_when
                ON processed_matches(processed_at DESC);
        """)

        # ─── YouTube commenter portfolio system ────────────────────────
        # The YouTube tables (youtube_portfolios, youtube_holdings,
        # youtube_video_gods, youtube_processed_comments,
        # youtube_transactions + their two indexes) live in
        # core/youtube_schema.py as the single source of truth. Standalone
        # CLI tools (tools/mark_youtube_video.py) also import that module
        # so the bot and the CLIs can never drift apart. We just run the
        # shared DDL here after our own tables exist.
        #
        # The shared DDL intentionally omits the FOREIGN KEY clauses on
        # youtube_holdings.god_name and youtube_video_gods.god_name that
        # used to live in this file — the CLIs may run before god_prices
        # exists, and FK enforcement on those columns was advisory only
        # (god_prices rows are never deleted, and every YouTube-side
        # insert is preceded by _ensure_god_exists).
        await self._db.executescript(YOUTUBE_SCHEMA_SQL)

        # Idempotent column-add migrations. Runs AFTER both executescripts
        # so the ALTER TABLE statements never hit a "no such table" error
        # on a fresh DB. Each ALTER is guarded by PRAGMA table_info, so it
        # is also a no-op on a DB that already has every column. Order
        # within the migration body doesn't matter — every step is
        # independent.
        await self._migrate_god_prices_kda_columns()
        await self._migrate_transactions_channel_column()

        await self._db.commit()
        print("[Economy] Database initialized")

    async def _migrate_god_prices_kda_columns(self):
        """
        Idempotent migration: add total_kills/deaths/assists columns to
        god_prices if they don't already exist. Old rows get 0; the
        replay tool can repopulate. New gods write straight to the
        columns from settle_match.

        Also adds leaderboard_opt_out to portfolios + youtube_portfolios
        so the god-page top-holders panel can filter by user privacy
        choice. Default is 0 (visible) — users can flip it via the
        chat command !hideme on Twitch or by commenting "!hideme" on
        any of Hatmaster's tagged YouTube videos.

        Adds dividends.match_id (for the backfill-catch-up dividend
        dedup added in the airtight-economy pass) and
        processed_matches.was_live_at_settle (audit column noting
        whether the broadcaster was live when the match was settled).
        Both default to NULL/0 for historical rows, which is the safe
        interpretation.
        """
        # god_prices KDA columns
        existing: set = set()
        async with self._db.execute(
                "PRAGMA table_info(god_prices)") as cur:
            async for row in cur:
                # row format: (cid, name, type, notnull, dflt_value, pk)
                existing.add(row[1])

        for col in ("total_kills", "total_deaths", "total_assists"):
            if col not in existing:
                await self._db.execute(
                    f"ALTER TABLE god_prices ADD COLUMN {col} "
                    f"INTEGER NOT NULL DEFAULT 0")
                print(f"[Economy] Migration: added god_prices.{col}")

        # portfolios opt-out
        portfolio_cols: set = set()
        async with self._db.execute(
                "PRAGMA table_info(portfolios)") as cur:
            async for row in cur:
                portfolio_cols.add(row[1])
        if "leaderboard_opt_out" not in portfolio_cols:
            await self._db.execute(
                "ALTER TABLE portfolios ADD COLUMN "
                "leaderboard_opt_out INTEGER NOT NULL DEFAULT 0")
            print("[Economy] Migration: added portfolios.leaderboard_opt_out")

        # youtube_portfolios opt-out
        yt_cols: set = set()
        async with self._db.execute(
                "PRAGMA table_info(youtube_portfolios)") as cur:
            async for row in cur:
                yt_cols.add(row[1])
        if "leaderboard_opt_out" not in yt_cols:
            await self._db.execute(
                "ALTER TABLE youtube_portfolios ADD COLUMN "
                "leaderboard_opt_out INTEGER NOT NULL DEFAULT 0")
            print("[Economy] Migration: added youtube_portfolios.leaderboard_opt_out")

        # dividends.match_id — added in the airtight-economy pass so
        # backfill can dedup dividends per tracker.gg match. Historical
        # rows keep NULL; the catch-up SELECT keys on a specific
        # match_id so NULL rows are invisible to it.
        dividend_cols: set = set()
        async with self._db.execute(
                "PRAGMA table_info(dividends)") as cur:
            async for row in cur:
                dividend_cols.add(row[1])
        if "match_id" not in dividend_cols:
            await self._db.execute(
                "ALTER TABLE dividends ADD COLUMN match_id TEXT DEFAULT NULL")
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_dividends_match "
                "ON dividends(match_id)")
            print("[Economy] Migration: added dividends.match_id (+ index)")

        # processed_matches.was_live_at_settle — audit column noting
        # whether the broadcaster was live when this match was settled.
        # Historical rows default to 0 (treated as "we don't know").
        proc_cols: set = set()
        async with self._db.execute(
                "PRAGMA table_info(processed_matches)") as cur:
            async for row in cur:
                proc_cols.add(row[1])
        if "was_live_at_settle" not in proc_cols:
            await self._db.execute(
                "ALTER TABLE processed_matches ADD COLUMN "
                "was_live_at_settle INTEGER NOT NULL DEFAULT 0")
            print("[Economy] Migration: added processed_matches.was_live_at_settle")

        await self._db.commit()

    async def _migrate_transactions_channel_column(self):
        """
        Idempotent migration: add transactions.channel ('chat' | 'web')
        so trades record which front door they came through. Historical
        rows default to 'chat' — the only channel that existed before
        website trading (WEBSITE_TRADING_DESIGN.md §0.3). Dividend and
        free_share rows also carry 'chat'; they're bot-initiated and
        the column is for debugging/stats, not money math.
        """
        cols: set = set()
        async with self._db.execute(
                "PRAGMA table_info(transactions)") as cur:
            async for row in cur:
                cols.add(row[1])
        if "channel" not in cols:
            await self._db.execute(
                "ALTER TABLE transactions ADD COLUMN channel "
                "TEXT NOT NULL DEFAULT 'chat'")
            print("[Economy] Migration: added transactions.channel")

    async def _load_prices(self):
        """Load all god prices into memory cache."""
        async with self._db.execute(
            "SELECT god_name, price, games_played FROM god_prices"
        ) as cursor:
            async for row in cursor:
                god_name, price, games = row
                self._prices[god_name] = price
                self._games_played[god_name] = games

        # Load recent price history for sparklines
        for god_name in self._prices:
            self._price_history[god_name] = await self._get_recent_prices(god_name)

    async def _get_recent_prices(self, god_name: str, limit: int = SPARKLINE_LENGTH) -> List[float]:
        """Get recent price points for sparklines."""
        prices = []
        async with self._db.execute(
            "SELECT price FROM price_history WHERE god_name = ? ORDER BY timestamp DESC LIMIT ?",
            (god_name, limit)
        ) as cursor:
            async for row in cursor:
                prices.append(row[0])
        prices.reverse()  # Chronological order
        return prices

    async def _update_price(self, god_name: str, new_price: float,
                            event: str = "update", *, commit: bool = True):
        """Update god price in DB and memory cache.

        commit=False lets settle_match defer the commit to the end of
        the whole settlement, so the aggregate update + price move +
        processed_matches claim land atomically. A mid-settlement
        commit here used to open a crash window where the aggregates
        were persisted without the dedup claim - a retry would then
        double-count the match into god_prices."""
        new_price = max(new_price, ECONOMY_PRICE_FLOOR)
        old_price = self._prices.get(god_name, ECONOMY_STARTING_PRICE)

        self._prices[god_name] = new_price

        # Track price history for sparklines
        if god_name not in self._price_history:
            self._price_history[god_name] = []
        self._price_history[god_name].append(new_price)
        if len(self._price_history[god_name]) > SPARKLINE_LENGTH:
            self._price_history[god_name] = self._price_history[god_name][-SPARKLINE_LENGTH:]

        # Track session change
        if god_name not in self._session_changes:
            self._session_changes[god_name] = 0.0
        if old_price > 0:
            pct = ((new_price - old_price) / old_price) * 100
            self._session_changes[god_name] += pct

        # Persist to DB
        await self._db.execute("""
            INSERT INTO god_prices (god_name, price, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(god_name) DO UPDATE SET
                price = excluded.price,
                updated_at = excluded.updated_at
        """, (god_name, new_price))

        await self._db.execute(
            "INSERT INTO price_history (god_name, price, event) VALUES (?, ?, ?)",
            (god_name, new_price, event)
        )
        if commit:
            await self._db.commit()

        return old_price, new_price

    async def _ensure_god_exists(self, god_name: str):
        """Create a god price entry if it doesn't exist."""
        if god_name not in self._prices:
            self._prices[god_name] = ECONOMY_STARTING_PRICE
            self._games_played[god_name] = 0
            self._price_history[god_name] = [ECONOMY_STARTING_PRICE]
            await self._db.execute(
                "INSERT OR IGNORE INTO god_prices (god_name, price) VALUES (?, ?)",
                (god_name, ECONOMY_STARTING_PRICE)
            )
            await self._db.execute(
                "INSERT INTO price_history (god_name, price, event) VALUES (?, ?, 'ipo')",
                (god_name, ECONOMY_STARTING_PRICE)
            )
            await self._db.commit()
