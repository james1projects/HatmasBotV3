"""
YouTube Schema
==============
**Single source of truth** for the YouTube portfolio system's
CREATE TABLE statements. Every consumer of these tables imports from
this module — the bot's economy plugin (plugins/economy/db.py runs
YOUTUBE_SCHEMA_SQL inside its _init_schema callback), plugins/youtube_rewards.py
(defense-in-depth in case EconomyPlugin is disabled), and standalone
CLI tools like tools/mark_youtube_video.py that connect to the DB
without going through the bot at all.

Standalone tools call ensure_youtube_schema() to make sure the tables
exist before they query them — this lets the CLI run BEFORE the bot
has ever been launched against the new schema.

All CREATE statements use IF NOT EXISTS, so calling this function on a
DB where the tables already exist is a no-op.

To change the schema, edit this file. If the change is a new column on
an existing table, add a corresponding idempotent migration in
plugins/economy/db.py:_migrate_god_prices_kda_columns() so existing
databases pick up the column too — the IF NOT EXISTS CREATE here only
runs on fresh databases.

The FOREIGN KEY clauses on youtube_holdings.god_name and
youtube_video_gods.god_name referencing god_prices(god_name) are
intentionally omitted — the standalone CLIs may run before god_prices
has been created, and FK enforcement on those columns was advisory
only (god_prices rows are never deleted and every YouTube-side insert
in the economy plugin path is preceded by _ensure_god_exists).
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

    See the module docstring for the rationale on the omitted FOREIGN KEY
    clauses on youtube_holdings.god_name and youtube_video_gods.god_name.
    """
    await db.executescript(YOUTUBE_SCHEMA_SQL)
    await db.commit()
