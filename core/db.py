"""
core/db.py — shared aiosqlite connection for HatmasBot's economy database.

Why this exists
---------------
Originally each consumer of `data/economy.db` opened its own
`aiosqlite.Connection`:
  - plugins/economy.py            (owns the schema)
  - plugins/god_pool.py
  - plugins/youtube_rewards.py
  - core/public_webserver.py

This works under SQLite's WAL mode (concurrent readers, serialized
writers) but it has real downsides:
  - Schema ownership is implicit. economy.py creates every table; the
    others just hope the schema is current. If a future consumer ever
    runs first, queries silently fail.
  - Plugin registration order in main.py becomes load-bearing —
    youtube_rewards must boot AFTER economy or its tables don't exist.
  - 4 statement caches, 4 PRAGMA setups, 4 places to update if we
    ever want to change connection-wide settings.
  - Migrations only run in economy.py; if a column is added in a
    second consumer's plugin, the others won't see it without a
    cross-plugin coordinated migration.

This module centralizes the connection. Plugins call `get_db()` to
receive the singleton `aiosqlite.Connection`, and register their
table-creation logic via `register_schema(callback)`. main.py opens
the connection once before plugin setup, closes it once at shutdown.

Plugins MUST NOT call `close()` on the connection — main.py owns the
lifecycle.

Concurrency note
----------------
aiosqlite serializes all operations through a single worker thread per
connection. Multiple coroutines using this shared connection therefore
queue safely; their statements run sequentially and the implicit
transaction model behaves the same as single-consumer code. We keep
the default isolation_level — every existing `await db.commit()` call
in plugins continues to mean exactly what it did before.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, List, Optional, TYPE_CHECKING

try:
    import aiosqlite
except ImportError:
    aiosqlite = None  # type: ignore

from core.config import ECONOMY_DB_PATH

if TYPE_CHECKING:
    SchemaCallback = Callable[["aiosqlite.Connection"], Awaitable[None]]


# ── Module-level state ─────────────────────────────────────────────────────
# Singleton connection + lazy init lock. The lock protects against the
# (currently impossible but cheap to guard) case of two coroutines
# calling init_db simultaneously before the singleton has been opened.

_db: "Optional[aiosqlite.Connection]" = None
_init_lock = asyncio.Lock()
_schema_callbacks: "List[SchemaCallback]" = []
_schema_run = False


def register_schema(callback) -> None:
    """
    Register an async callback that creates/migrates a plugin's tables.

    The callback receives the shared aiosqlite.Connection and should be
    idempotent (use ``CREATE TABLE IF NOT EXISTS`` and PRAGMA-driven
    column-add migrations). It's invoked exactly once, after the
    connection is opened.

    Safe to call before init_db(). Callbacks accumulate until init_db
    runs, then fire in registration order. After init_db has already
    run, calling register_schema queues the callback for the NEXT
    init_db invocation; in practice that means it's a no-op for normal
    bot operation. If you need to add tables at runtime, call your
    callback yourself with the result of get_db().
    """
    if callback not in _schema_callbacks:
        _schema_callbacks.append(callback)


async def init_db() -> "Optional[aiosqlite.Connection]":
    """
    Open the shared connection if it isn't already open, set PRAGMAs,
    and run any registered schema callbacks (once).

    Idempotent. Safe to call multiple times — only the first call
    actually opens the connection and runs callbacks.

    Returns the connection, or None if aiosqlite isn't installed (in
    which case the caller should disable economy/db-dependent features).
    """
    global _db, _schema_run

    if aiosqlite is None:
        return None

    async with _init_lock:
        if _db is None:
            _db = await aiosqlite.connect(str(ECONOMY_DB_PATH))
            await _db.execute("PRAGMA journal_mode=WAL")
            await _db.execute("PRAGMA foreign_keys=ON")
            print(f"[DB] Opened shared connection to {ECONOMY_DB_PATH.name}")

        if not _schema_run:
            _schema_run = True
            for cb in _schema_callbacks:
                name = getattr(cb, "__qualname__", repr(cb))
                try:
                    await cb(_db)
                except Exception as e:
                    print(f"[DB] Schema callback {name!r} failed: {e}")
                    # Do NOT re-raise — one plugin's failed migration
                    # shouldn't take down the whole bot. The plugin
                    # itself will surface the error when it tries to
                    # query its tables.

        return _db


async def get_db() -> "Optional[aiosqlite.Connection]":
    """
    Return the shared aiosqlite connection.

    Returns None if init_db() hasn't been called yet, or if aiosqlite
    isn't installed. Plugins should treat None as "DB unavailable" and
    fall back gracefully (skip features, log a warning, etc.) — this
    matches the existing per-plugin handling for missing aiosqlite.
    """
    return _db


async def close_db() -> None:
    """
    Close the shared connection. Called once by main.py during the
    shutdown sequence. Plugins MUST NOT call this themselves — it
    would leave other plugins with a dead connection.

    Idempotent: closing an already-closed connection is a no-op.
    """
    global _db, _schema_run
    if _db is not None:
        try:
            await _db.close()
        except Exception as e:
            print(f"[DB] Close error (non-fatal): {e}")
        _db = None
        # Reset the schema-run guard so a hypothetical re-init in the
        # same process (e.g., test harness, future hot-reload) works.
        _schema_run = False
        print("[DB] Closed shared connection")


def is_available() -> bool:
    """True if aiosqlite is importable. Useful for plugin setup checks."""
    return aiosqlite is not None
