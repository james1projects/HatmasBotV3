"""
core/users.py — one UUID per viewer across Twitch and YouTube
==============================================================

The identity layer described in docs/USER_IDENTITY_PLAN.md.

    users            one row per person: uuid, display_name, avatar,
                     leaderboard_opt_out, merged_into
    user_identities  (provider, provider_id) -> user_uuid; a person
                     holds one or more, an identity belongs to one person
    user_merges      audit log of every merge (absorbed -> survivor)

Every viewer-keyed table in economy.db (portfolios, transactions,
god_pool_votes, priority_payments, pending_yt_nominations, ...) carries a
`user_uuid` column that points here. The column name is deliberately
`user_uuid`, never `user_id`, so a reader never mistakes it for a login.

Provider ids:
  * twitch  -> the numeric Twitch user id (stable across renames)
  * youtube -> the UC... channel id
  * twitch placeholder -> "login:<login>" for rows migrated from the old
    login-keyed tables before the person has been seen with an id. The
    first sighting with a real id upgrades the row in place
    (get_or_create_twitch), so the uuid never changes.

No FOREIGN KEY constraints point at users.uuid: the tests build tiny
schemas with arbitrary uuids, and the existing economy tables already
treat their god_name FKs as advisory. Integrity is enforced by the
helpers here, not by SQLite.

Everything is async over the shared aiosqlite connection (core/db.py).
The module keeps a per-connection cache of provider_id -> uuid and of
merged_into chains; merge() clears it.
"""

from __future__ import annotations

import json
import uuid as _uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

PROVIDERS = ("twitch", "youtube")
PLACEHOLDER_PREFIX = "login:"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    uuid                TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    avatar_url          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at        TEXT,
    merged_into         TEXT,
    leaderboard_opt_out INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_merged ON users(merged_into);
CREATE INDEX IF NOT EXISTS idx_users_display ON users(display_name);

CREATE TABLE IF NOT EXISTS user_identities (
    provider     TEXT NOT NULL CHECK (provider IN ('twitch', 'youtube')),
    provider_id  TEXT NOT NULL,
    user_uuid    TEXT NOT NULL,
    login        TEXT,
    display_name TEXT,
    linked_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT,
    PRIMARY KEY (provider, provider_id)
);
CREATE INDEX IF NOT EXISTS idx_user_identities_user
    ON user_identities(user_uuid);
CREATE INDEX IF NOT EXISTS idx_user_identities_login
    ON user_identities(provider, login);

CREATE TABLE IF NOT EXISTS user_merges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    absorbed_uuid TEXT NOT NULL,
    survivor_uuid TEXT NOT NULL,
    initiated_by  TEXT NOT NULL,
    merged_at     TEXT NOT NULL DEFAULT (datetime('now')),
    summary       TEXT NOT NULL
);
"""


# ── per-connection cache ───────────────────────────────────────────────

class _Cache:
    __slots__ = ("by_identity", "resolved")

    def __init__(self):
        self.by_identity: Dict[Tuple[str, str], str] = {}
        self.resolved: Dict[str, str] = {}


_caches: Dict[int, _Cache] = {}


def _cache(db) -> _Cache:
    c = _caches.get(id(db))
    if c is None:
        c = _caches[id(db)] = _Cache()
    return c


def clear_cache(db=None) -> None:
    """Drop cached lookups (all connections, or one)."""
    if db is None:
        _caches.clear()
    else:
        _caches.pop(id(db), None)


# ── merge hooks for tables that live in OTHER sqlite files ─────────────
# bingo.db and chat_log.db are keyed on user_uuid too but can't take
# part in economy.db's transaction. Their owners register an async
# hook(absorbed_uuid, survivor_uuid) that re-points their rows; merge()
# calls every hook after the economy.db commit.

MergeHook = Callable[[str, str], Awaitable[Any]]
_merge_hooks: List[MergeHook] = []


def register_merge_hook(fn: MergeHook) -> None:
    if fn not in _merge_hooks:
        _merge_hooks.append(fn)


# ── schema ─────────────────────────────────────────────────────────────

async def ensure_schema(db) -> None:
    """Create the identity tables. Idempotent. Registered with
    core.db.register_schema() by main.py BEFORE any plugin schema so the
    per-table backfills (attach_user_uuid) can run inside those."""
    await db.executescript(SCHEMA_SQL)
    await db.commit()


def new_uuid() -> str:
    return str(_uuid.uuid4())


# ── basic reads ────────────────────────────────────────────────────────

async def resolve(db, user_uuid: Optional[str]) -> Optional[str]:
    """Follow merged_into until a live row. None for unknown uuids."""
    if not user_uuid:
        return None
    c = _cache(db)
    hit = c.resolved.get(user_uuid)
    if hit:
        return hit
    cur_id = user_uuid
    for _ in range(16):  # chains are short; guard against loops anyway
        async with db.execute(
                "SELECT merged_into FROM users WHERE uuid = ?",
                (cur_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        if not row[0]:
            c.resolved[user_uuid] = cur_id
            return cur_id
        cur_id = row[0]
    return cur_id


async def get_user(db, user_uuid: Optional[str]) -> Optional[dict]:
    live = await resolve(db, user_uuid)
    if not live:
        return None
    async with db.execute(
            "SELECT uuid, display_name, avatar_url, created_at, "
            "last_seen_at, leaderboard_opt_out FROM users WHERE uuid = ?",
            (live,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {"uuid": row[0], "display_name": row[1], "avatar_url": row[2],
            "created_at": row[3], "last_seen_at": row[4],
            "leaderboard_opt_out": bool(row[5])}


async def identities(db, user_uuid: Optional[str]) -> List[dict]:
    live = await resolve(db, user_uuid)
    if not live:
        return []
    out = []
    async with db.execute(
            "SELECT provider, provider_id, login, display_name, linked_at, "
            "last_seen_at FROM user_identities WHERE user_uuid = ? "
            "ORDER BY linked_at", (live,)) as cur:
        async for r in cur:
            out.append({"provider": r[0], "provider_id": r[1],
                        "login": r[2], "display_name": r[3],
                        "linked_at": r[4], "last_seen_at": r[5]})
    return out


async def twitch_login_of(db, user_uuid: Optional[str]) -> Optional[str]:
    """The user's Twitch login (lowercase), or None for YouTube-only
    users. Used by the /twitch/<login> URLs and the god request queue."""
    live = await resolve(db, user_uuid)
    if not live:
        return None
    async with db.execute(
            "SELECT login FROM user_identities WHERE user_uuid = ? "
            "AND provider = 'twitch' AND login IS NOT NULL "
            "ORDER BY (provider_id LIKE 'login:%'), linked_at LIMIT 1",
            (live,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def youtube_channel_of(db, user_uuid: Optional[str]) -> Optional[str]:
    live = await resolve(db, user_uuid)
    if not live:
        return None
    async with db.execute(
            "SELECT provider_id FROM user_identities WHERE user_uuid = ? "
            "AND provider = 'youtube' ORDER BY linked_at LIMIT 1",
            (live,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def display_name_of(db, user_uuid: Optional[str]) -> str:
    u = await get_user(db, user_uuid)
    return (u or {}).get("display_name") or ""


async def public_ref(db, user_uuid: Optional[str]) -> Tuple[str, str]:
    """(platform, identifier) for building a public portfolio URL:
    ('twitch', login) when the user has a Twitch login, else
    ('youtube', channel_id), else ('user', uuid)."""
    login = await twitch_login_of(db, user_uuid)
    if login:
        return "twitch", login
    chan = await youtube_channel_of(db, user_uuid)
    if chan:
        return "youtube", chan
    return "user", user_uuid or ""


# ── find / create ──────────────────────────────────────────────────────

async def _touch(db, user_uuid: str, display_name: Optional[str],
                 avatar_url: Optional[str]) -> None:
    if display_name and avatar_url:
        await db.execute(
            "UPDATE users SET display_name = ?, avatar_url = ?, "
            "last_seen_at = datetime('now') WHERE uuid = ?",
            (display_name, avatar_url, user_uuid))
    elif display_name:
        await db.execute(
            "UPDATE users SET display_name = ?, "
            "last_seen_at = datetime('now') WHERE uuid = ?",
            (display_name, user_uuid))
    else:
        await db.execute(
            "UPDATE users SET last_seen_at = datetime('now') "
            "WHERE uuid = ?", (user_uuid,))


async def _create_user(db, display_name: str,
                       avatar_url: Optional[str] = None) -> str:
    uid = new_uuid()
    await db.execute(
        "INSERT INTO users (uuid, display_name, avatar_url, last_seen_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (uid, display_name or "viewer", avatar_url))
    return uid


async def _identity_uuid(db, provider: str, provider_id: str
                         ) -> Optional[str]:
    c = _cache(db)
    key = (provider, provider_id)
    hit = c.by_identity.get(key)
    if hit:
        return await resolve(db, hit)
    async with db.execute(
            "SELECT user_uuid FROM user_identities "
            "WHERE provider = ? AND provider_id = ?", key) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    c.by_identity[key] = row[0]
    return await resolve(db, row[0])


async def find_twitch_id(db, twitch_id: str) -> Optional[str]:
    return await _identity_uuid(db, "twitch", str(twitch_id))


async def find_twitch_login(db, login: Optional[str]) -> Optional[str]:
    """User holding a Twitch identity with this login (real id or
    placeholder). Never creates."""
    login = (login or "").lower().strip()
    if not login:
        return None
    async with db.execute(
            "SELECT user_uuid FROM user_identities "
            "WHERE provider = 'twitch' AND login = ? "
            "ORDER BY (provider_id LIKE 'login:%') LIMIT 1",
            (login,)) as cur:
        row = await cur.fetchone()
    return await resolve(db, row[0]) if row else None


async def find_youtube(db, channel_id: Optional[str]) -> Optional[str]:
    if not channel_id:
        return None
    return await _identity_uuid(db, "youtube", channel_id)


async def get_or_create_twitch(db, twitch_id: str, login: str,
                               display_name: Optional[str] = None,
                               avatar_url: Optional[str] = None,
                               commit: bool = True) -> str:
    """Resolve a Twitch chatter/login to a user uuid, creating the
    user on first sight. Upgrades a migration placeholder
    ("login:<login>") to the real id in place, and clears a stale
    login off any OTHER identity that still carries it (renames)."""
    twitch_id = str(twitch_id or "").strip()
    login = (login or "").lower().strip()
    if not twitch_id:
        return await get_or_create_twitch_login(
            db, login, display_name, commit=commit)

    uid = await _identity_uuid(db, "twitch", twitch_id)
    if uid is None and login:
        # Placeholder from the login-keyed era?
        placeholder = PLACEHOLDER_PREFIX + login
        uid = await _identity_uuid(db, "twitch", placeholder)
        if uid is not None:
            await db.execute(
                "UPDATE user_identities SET provider_id = ? "
                "WHERE provider = 'twitch' AND provider_id = ?",
                (twitch_id, placeholder))
            _cache(db).by_identity.pop(("twitch", placeholder), None)
    if uid is None:
        uid = await _create_user(db, display_name or login, avatar_url)
        await db.execute(
            "INSERT INTO user_identities (provider, provider_id, user_uuid, "
            "login, display_name, last_seen_at) "
            "VALUES ('twitch', ?, ?, ?, ?, datetime('now'))",
            (twitch_id, uid, login or None, display_name or login))
    else:
        await db.execute(
            "UPDATE user_identities SET login = COALESCE(?, login), "
            "display_name = COALESCE(?, display_name), "
            "last_seen_at = datetime('now') "
            "WHERE provider = 'twitch' AND provider_id = ?",
            (login or None, display_name, twitch_id))
        await _touch(db, uid, display_name, avatar_url)
    if login:
        # A rename: someone else used to be <login>. Their identity
        # keeps its id, loses the now-wrong login.
        await db.execute(
            "UPDATE user_identities SET login = NULL "
            "WHERE provider = 'twitch' AND login = ? AND provider_id <> ? "
            "AND provider_id NOT LIKE 'login:%'", (login, twitch_id))
    _cache(db).by_identity[("twitch", twitch_id)] = uid
    if commit:
        await db.commit()
    return uid


async def get_or_create_twitch_login(db, login: str,
                                     display_name: Optional[str] = None,
                                     commit: bool = True) -> str:
    """Login-only path (no Twitch id in hand): the migration, typed
    usernames on the priority form, mod grants. Reuses any identity
    with that login, else creates a placeholder identity that the
    first id-bearing sighting upgrades."""
    login = (login or "").lower().strip()
    if not login:
        raise ValueError("empty login")
    uid = await find_twitch_login(db, login)
    if uid is None:
        uid = await _create_user(db, display_name or login)
        await db.execute(
            "INSERT INTO user_identities (provider, provider_id, user_uuid, "
            "login, display_name) VALUES ('twitch', ?, ?, ?, ?)",
            (PLACEHOLDER_PREFIX + login, uid, login, display_name or login))
        _cache(db).by_identity[("twitch", PLACEHOLDER_PREFIX + login)] = uid
    elif display_name:
        await _touch(db, uid, display_name, None)
    if commit:
        await db.commit()
    return uid


async def get_or_create_youtube(db, channel_id: str,
                                display_name: Optional[str] = None,
                                avatar_url: Optional[str] = None,
                                commit: bool = True) -> str:
    channel_id = (channel_id or "").strip()
    if not channel_id:
        raise ValueError("empty channel id")
    uid = await _identity_uuid(db, "youtube", channel_id)
    if uid is None:
        uid = await _create_user(db, display_name or channel_id, avatar_url)
        await db.execute(
            "INSERT INTO user_identities (provider, provider_id, user_uuid, "
            "display_name, last_seen_at) "
            "VALUES ('youtube', ?, ?, ?, datetime('now'))",
            (channel_id, uid, display_name or channel_id))
        _cache(db).by_identity[("youtube", channel_id)] = uid
    else:
        await db.execute(
            "UPDATE user_identities SET display_name = COALESCE(?, "
            "display_name), last_seen_at = datetime('now') "
            "WHERE provider = 'youtube' AND provider_id = ?",
            (display_name, channel_id))
        await _touch(db, uid, display_name, avatar_url)
    if commit:
        await db.commit()
    return uid


# ── settings ───────────────────────────────────────────────────────────

async def get_leaderboard_opt_out(db, user_uuid: Optional[str]) -> bool:
    u = await get_user(db, user_uuid)
    return bool(u and u["leaderboard_opt_out"])


async def set_leaderboard_opt_out(db, user_uuid: str, hidden: bool,
                                  commit: bool = True) -> None:
    live = await resolve(db, user_uuid)
    if not live:
        return
    await db.execute(
        "UPDATE users SET leaderboard_opt_out = ? WHERE uuid = ?",
        (1 if hidden else 0, live))
    if commit:
        await db.commit()


async def excluded_uuids(db, logins) -> Set[str]:
    """uuids of every user whose Twitch login is on the exclusion list
    (bot accounts). Never creates users; bots that never appeared
    simply aren't in any table."""
    wanted = {(l or "").lower() for l in (logins or []) if l}
    if not wanted:
        return set()
    placeholders = ",".join("?" for _ in wanted)
    out: Set[str] = set()
    async with db.execute(
            f"SELECT user_uuid FROM user_identities WHERE provider = "
            f"'twitch' AND login IN ({placeholders})",
            tuple(wanted)) as cur:
        async for r in cur:
            live = await resolve(db, r[0])
            if live:
                out.add(live)
    return out


def sql_not_excluded(column: str, excluded: Set[str]) -> Tuple[str, tuple]:
    """('AND <column> NOT IN (?,?)', params) or ('', ()) when empty.
    Keeps the exclusion filter one-liner at every query site."""
    if not excluded:
        return "", ()
    ph = ",".join("?" for _ in excluded)
    return f" AND {column} NOT IN ({ph})", tuple(sorted(excluded))


# ── linking + merging ──────────────────────────────────────────────────

async def link_youtube(db, user_uuid: str, channel_id: str,
                       display_name: Optional[str] = None,
                       avatar_url: Optional[str] = None,
                       initiated_by: str = "user") -> dict:
    """Attach a proven YouTube channel to `user_uuid`.

      channel unknown                     -> new identity, no merge
      channel already on this user        -> {"ok": True, "already": True}
      channel on a user with NO Twitch id -> that user is absorbed into
                                             user_uuid (merge rules)
      channel on a user WITH a Twitch id  -> refused, "linked_elsewhere"
    """
    survivor = await resolve(db, user_uuid)
    if not survivor:
        return {"ok": False, "reason": "no_user"}
    channel_id = (channel_id or "").strip()
    if not channel_id:
        return {"ok": False, "reason": "bad_args"}

    owner = await find_youtube(db, channel_id)
    if owner == survivor:
        await get_or_create_youtube(db, channel_id, display_name, avatar_url)
        return {"ok": True, "already": True, "merged": None}
    if owner is None:
        await db.execute(
            "INSERT INTO user_identities (provider, provider_id, user_uuid, "
            "display_name, last_seen_at) "
            "VALUES ('youtube', ?, ?, ?, datetime('now'))",
            (channel_id, survivor, display_name or channel_id))
        _cache(db).by_identity[("youtube", channel_id)] = survivor
        await db.commit()
        return {"ok": True, "already": False, "merged": None}

    # Someone already holds this channel. Standalone YouTube user ->
    # merge into the caller (Twitch side survives). Anything else is
    # a different person's account.
    if await twitch_login_of(db, owner) or await _has_twitch(db, owner):
        return {"ok": False, "reason": "linked_elsewhere"}
    summary = await merge(db, owner, survivor, initiated_by=initiated_by)
    return {"ok": True, "already": False, "merged": summary}


async def _has_twitch(db, user_uuid: str) -> bool:
    async with db.execute(
            "SELECT 1 FROM user_identities WHERE user_uuid = ? "
            "AND provider = 'twitch' LIMIT 1", (user_uuid,)) as cur:
        return await cur.fetchone() is not None


async def pick_survivor(db, a: str, b: str) -> Tuple[str, str]:
    """Merge rule 1: the row holding a Twitch identity survives; if
    neither or both, the older created_at. Returns (survivor, absorbed)."""
    a_tw, b_tw = await _has_twitch(db, a), await _has_twitch(db, b)
    if a_tw != b_tw:
        return (a, b) if a_tw else (b, a)
    async with db.execute(
            "SELECT uuid FROM users WHERE uuid IN (?, ?) "
            "ORDER BY created_at, uuid LIMIT 1", (a, b)) as cur:
        row = await cur.fetchone()
    older = row[0] if row else a
    return (older, b if older == a else a)


async def _table_has(db, table: str, column: Optional[str] = None) -> bool:
    async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,)) as cur:
        if await cur.fetchone() is None:
            return False
    if column is None:
        return True
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        async for r in cur:
            if r[1] == column:
                return True
    return False


async def merge(db, absorbed_uuid: str, survivor_uuid: str,
                initiated_by: str = "user") -> dict:
    """Fold `absorbed` into `survivor` (docs/USER_IDENTITY_PLAN.md
    merge rules). Returns the summary that is also stored in
    user_merges. Raises ValueError on bad input. The absorbed row is
    kept with merged_into set; nothing is deleted except the absorbed
    side of per-user singletons."""
    absorbed = await resolve(db, absorbed_uuid)
    survivor = await resolve(db, survivor_uuid)
    if not absorbed or not survivor:
        raise ValueError("unknown user")
    if absorbed == survivor:
        return {"noop": True, "survivor": survivor}

    summary: Dict[str, Any] = {"absorbed": absorbed, "survivor": survivor,
                               "tables": {}, "positions": []}

    async def repoint(table: str, column: str = "user_uuid") -> None:
        if not await _table_has(db, table, column):
            return
        cur = await db.execute(
            f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
            (survivor, absorbed))
        summary["tables"][table] = cur.rowcount

    try:
        # identities follow the survivor
        cur = await db.execute(
            "UPDATE user_identities SET user_uuid = ? WHERE user_uuid = ?",
            (survivor, absorbed))
        summary["tables"]["user_identities"] = cur.rowcount

        # positions: weighted-average merge (rule 3)
        if await _table_has(db, "portfolios", "user_uuid"):
            rows = []
            async with db.execute(
                    "SELECT god_name, shares, avg_cost FROM portfolios "
                    "WHERE user_uuid = ?", (absorbed,)) as cur:
                async for r in cur:
                    rows.append((r[0], float(r[1] or 0), float(r[2] or 0)))
            for god, shares, avg_cost in rows:
                async with db.execute(
                        "SELECT shares, avg_cost FROM portfolios "
                        "WHERE user_uuid = ? AND god_name = ?",
                        (survivor, god)) as cur:
                    have = await cur.fetchone()
                if have:
                    old_s, old_a = float(have[0] or 0), float(have[1] or 0)
                    tot = old_s + shares
                    new_a = (((old_s * old_a) + (shares * avg_cost)) / tot
                             if tot > 0 else 0.0)
                    await db.execute(
                        "UPDATE portfolios SET shares = ?, avg_cost = ? "
                        "WHERE user_uuid = ? AND god_name = ?",
                        (tot, new_a, survivor, god))
                else:
                    await db.execute(
                        "INSERT INTO portfolios (user_uuid, god_name, "
                        "shares, avg_cost) VALUES (?, ?, ?, ?)",
                        (survivor, god, shares, avg_cost))
                await db.execute(
                    "DELETE FROM portfolios WHERE user_uuid = ? "
                    "AND god_name = ?", (absorbed, god))
                if shares > 0 and await _table_has(db, "transactions",
                                                   "user_uuid"):
                    await db.execute(
                        "INSERT INTO transactions (user_uuid, god_name, "
                        "type, shares, price, total, fee, channel, ref) "
                        "VALUES (?, ?, 'merge_in', ?, ?, ?, 0, 'system', ?)",
                        (survivor, god, shares, avg_cost,
                         shares * avg_cost, absorbed))
                summary["positions"].append(
                    {"god": god, "shares": shares, "avg_cost": avg_cost})

        # summable rows (rule 2)
        for table in ("transactions", "priority_payments",
                      "pending_yt_nominations", "wallet_ledger"):
            await repoint(table)
        await repoint("god_pool", "added_by_uuid")

        # singletons (rule 4): survivor's value wins
        if await _table_has(db, "god_pool_votes", "user_uuid"):
            cur = await db.execute(
                "UPDATE OR IGNORE god_pool_votes SET user_uuid = ? "
                "WHERE user_uuid = ?", (survivor, absorbed))
            moved = cur.rowcount
            cur = await db.execute(
                "DELETE FROM god_pool_votes WHERE user_uuid = ?",
                (absorbed,))
            summary["tables"]["god_pool_votes"] = {
                "moved": moved, "dropped": cur.rowcount}
        if await _table_has(db, "wallet_balances", "user_uuid"):
            # balances add (WALLET_PLAN.md); the ledger rows were
            # re-pointed above so SUM(delta) still matches.
            rows = []
            async with db.execute(
                    "SELECT asset, amount FROM wallet_balances "
                    "WHERE user_uuid = ?", (absorbed,)) as cur:
                async for r in cur:
                    rows.append((r[0], int(r[1] or 0)))
            for asset, amount in rows:
                await db.execute(
                    "INSERT INTO wallet_balances (user_uuid, asset, amount) "
                    "VALUES (?, ?, ?) ON CONFLICT(user_uuid, asset) DO UPDATE "
                    "SET amount = amount + excluded.amount, "
                    "updated_at = datetime('now')",
                    (survivor, asset, amount))
            await db.execute(
                "DELETE FROM wallet_balances WHERE user_uuid = ?",
                (absorbed,))
            summary["tables"]["wallet_balances"] = rows

        # the users rows (rule 5)
        async with db.execute(
                "SELECT display_name, avatar_url, last_seen_at FROM users "
                "WHERE uuid = ?", (absorbed,)) as cur:
            ab = await cur.fetchone()
        await db.execute(
            "UPDATE users SET display_name = CASE WHEN display_name = '' "
            "OR display_name IS NULL THEN ? ELSE display_name END, "
            "avatar_url = COALESCE(avatar_url, ?), "
            "last_seen_at = MAX(COALESCE(last_seen_at, ''), COALESCE(?, '')) "
            "WHERE uuid = ?",
            ((ab[0] if ab else None), (ab[1] if ab else None),
             (ab[2] if ab else None), survivor))
        await db.execute(
            "UPDATE users SET merged_into = ? WHERE uuid = ?",
            (survivor, absorbed))
        await db.execute(
            "UPDATE users SET merged_into = ? WHERE merged_into = ?",
            (survivor, absorbed))

        await db.execute(
            "INSERT INTO user_merges (absorbed_uuid, survivor_uuid, "
            "initiated_by, summary) VALUES (?, ?, ?, ?)",
            (absorbed, survivor, initiated_by,
             json.dumps(summary, default=str)))
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        clear_cache(db)

    # other sqlite files (bingo, chat log)
    hooks: Dict[str, Any] = {}
    for fn in list(_merge_hooks):
        name = getattr(fn, "__qualname__", repr(fn))
        try:
            hooks[name] = await fn(absorbed, survivor)
        except Exception as e:  # never let a side file break the merge
            hooks[name] = f"error: {e}"
            print(f"[Users] merge hook {name} failed: {e}")
    if hooks:
        summary["hooks"] = hooks
        await db.execute(
            "UPDATE user_merges SET summary = ? WHERE id = (SELECT MAX(id) "
            "FROM user_merges WHERE absorbed_uuid = ?)",
            (json.dumps(summary, default=str), absorbed))
        await db.commit()
    print(f"[Users] merged {absorbed} -> {survivor} "
          f"({len(summary['positions'])} positions, "
          f"tables={summary['tables']})")
    return summary


# ── migration helper for table owners ──────────────────────────────────

async def attach_user_uuid(db, table: str, legacy_column: str,
                           provider: str, uuid_column: str = "user_uuid",
                           display_column: Optional[str] = None,
                           index: bool = True) -> int:
    """Add `uuid_column` to `table` if missing and backfill it from the
    legacy login / channel-id column via placeholder identities.
    Idempotent: rows that already carry a uuid are left alone. Returns
    the number of rows backfilled. Caller commits."""
    if not await _table_has(db, table):
        return 0
    if not await _table_has(db, table, uuid_column):
        await db.execute(
            f"ALTER TABLE {table} ADD COLUMN {uuid_column} TEXT")
        print(f"[Users] Migration: added {table}.{uuid_column}")
    if index:
        await db.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_{uuid_column} "
            f"ON {table}({uuid_column})")

    disp = f", MAX({display_column})" if display_column else ""
    key_expr = (f"LOWER({legacy_column})" if provider == "twitch"
                else legacy_column)
    async with db.execute(
            f"SELECT {key_expr}{disp} FROM {table} "
            f"WHERE {uuid_column} IS NULL AND {legacy_column} IS NOT NULL "
            f"AND {legacy_column} <> '' GROUP BY {key_expr}") as cur:
        keys = await cur.fetchall()
    n = 0
    for row in keys:
        key = row[0]
        display = row[1] if display_column else None
        try:
            if provider == "twitch":
                uid = await get_or_create_twitch_login(
                    db, key, display, commit=False)
            else:
                uid = await get_or_create_youtube(
                    db, key, display, commit=False)
        except ValueError:
            continue
        cur = await db.execute(
            f"UPDATE {table} SET {uuid_column} = ? "
            f"WHERE {uuid_column} IS NULL AND {key_expr} = ?", (uid, key))
        n += cur.rowcount
    if n:
        print(f"[Users] Migration: backfilled {table}.{uuid_column} "
              f"on {n} rows ({len(keys)} {provider} identities)")
    return n
