"""
YouTube Schema
==============
Shared CREATE TABLE definitions for the YouTube portfolio system.

The same schema is also embedded inside plugins/economy.py's _init_db()
so the bot can boot from a fresh `economy.db`. Standalone tools
(tools/mark_youtube_video.py) that connect to the DB without going
through the EconomyPlugin call ensure_youtube_schema() to make sure the
tables exist before they query them — this lets the CLI run BEFORE
the bot has ever been launched against the new schema.

All CREATE statements use IF NOT EXISTS, so calling this function on a
DB where the tables already exist is a no-op.

If you change the schema, change it in BOTH places (here and economy.py)
to keep them in sync. A schema mismatch will manifest as either missing
columns at runtime or sqlite refusing to use a column the test suite
expected.
"""

import aiosqlite


YOUTUBE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS youtube_portfolios (
        yt_channel_id    TEXT PRIMARY KEY,
        yt_display_name  TEXT NOT NULL,
        first_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
        leaderboard_opt_out INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS youtube_holdings (
        yt_channel_id  TEXT NOT NULL,
        god_name       TEXT NOT NULL,
        shares         REAL NOT NULL DEFAULT 0,
        avg_cost       REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (yt_channel_id, god_name)
    );

    CREATE TABLE IF NOT EXISTS youtube_video_gods (
        yt_video_id  TEXT PRIMARY KEY,
        god_name     TEXT NOT NULL,
        title        TEXT,
        set_at       TEXT NOT NULL DEFAULT (datetime('now')),
        set_by       TEXT NOT NULL DEFAULT 'auto'
    );

    CREATE TABLE IF NOT EXISTS youtube_processed_comments (
        yt_video_id    TEXT NOT NULL,
        yt_channel_id  TEXT NOT NULL,
        comment_id     TEXT NOT NULL,
        granted_at     TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (yt_video_id, yt_channel_id)
    );

    CREATE TABLE IF NOT EXISTS youtube_transactions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        yt_channel_id  TEXT NOT NULL,
        god_name       TEXT NOT NULL,
        type           TEXT NOT NULL,
        shares         REAL NOT NULL,
        price          REAL NOT NULL,
        yt_video_id    TEXT,
        timestamp      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_yt_holdings_god
        ON youtube_holdings(god_name);
    CREATE INDEX IF NOT EXISTS idx_yt_transactions_user
        ON youtube_transactions(yt_channel_id, timestamp DESC);
"""


async def ensure_youtube_schema(db: "aiosqlite.Connection") -> None:
    """
    Create the YouTube tables if they don't exist. Safe to call
    repeatedly (every CREATE has IF NOT EXISTS). Cheap on a hot DB.

    Note: the FOREIGN KEY references to god_prices that economy.py uses
    are intentionally omitted here — the CLI has to be runnable before
    god_prices has any rows, and the FK enforcement (with foreign_keys
    pragma off by default in aiosqlite) is essentially advisory anyway.
    """
    await db.executescript(YOUTUBE_SCHEMA_SQL)
    await db.commit()
