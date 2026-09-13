"""
core/wallet.py — Hats and God Tokens, local and ledgered
=========================================================

The wallet from docs/WALLET_PLAN.md: the bot is the source of truth for
viewer money (MixItUp used to be). Every viewer is a `user_uuid`
(core/users.py); every asset is a whole number.

    wallet_balances     current amount per (user_uuid, asset) — a cache
    wallet_ledger       append-only history; SUM(delta) == the balance
    wallet_earn_ticks   one row per passive-earning pass
    wallet_imports      one row per MixItUp import run
    wallet_import_rows  what MixItUp said about each user, for reconciling

Principles
  * balances never go negative: every debit is a single conditional
    UPDATE, so two concurrent spends cannot both succeed;
  * every change has a reason from a fixed list, and (reason, ref) is
    unique when ref is given — sub awards, dividends and the import are
    safe to replay (a duplicate is a no-op that returns None);
  * balance rows are written in the same transaction as the ledger row.

All functions take the shared aiosqlite connection (core/db.py) and
commit unless told not to.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ASSETS = ("hats", "god_token")

REASONS = (
    "buy", "sell", "refund", "dividend",
    "gamble_win", "gamble_loss",
    "bingo_card", "bingo_prize", "priority_sr", "burn",
    "godreq_spend", "sub_award", "donation_award",
    "watch", "chat_bonus", "sub_bonus", "raid_bonus", "bits_bonus",
    "first_msg_bonus",
    "mod_grant", "mod_take",
    "link_merge", "migration", "adjust",
)

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS wallet_balances (
    user_uuid      TEXT NOT NULL,
    asset          TEXT NOT NULL CHECK (asset IN ('hats', 'god_token')),
    amount         INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_earned_at TEXT,
    PRIMARY KEY (user_uuid, asset)
);
CREATE INDEX IF NOT EXISTS idx_wallet_balances_top
    ON wallet_balances(asset, amount DESC);

CREATE TABLE IF NOT EXISTS wallet_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_uuid     TEXT NOT NULL,
    asset         TEXT NOT NULL,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason        TEXT NOT NULL CHECK (reason IN ({", ".join("'" + r + "'" for r in REASONS)})),
    ref           TEXT,
    actor         TEXT NOT NULL DEFAULT 'system',
    channel       TEXT NOT NULL DEFAULT 'chat',
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wallet_ledger_user
    ON wallet_ledger(user_uuid, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_ledger_reason
    ON wallet_ledger(reason, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_ledger_ref
    ON wallet_ledger(reason, ref) WHERE ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS wallet_earn_ticks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticked_at  TEXT NOT NULL DEFAULT (datetime('now')),
    was_live   INTEGER NOT NULL,
    chatters   INTEGER NOT NULL,
    credited   INTEGER NOT NULL,
    skipped    INTEGER NOT NULL,
    hats_total INTEGER NOT NULL,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS wallet_imports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    dry_run        INTEGER NOT NULL,
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    users_seen     INTEGER NOT NULL DEFAULT 0,
    users_imported INTEGER NOT NULL DEFAULT 0,
    hats_total     INTEGER NOT NULL DEFAULT 0,
    tokens_total   INTEGER NOT NULL DEFAULT 0,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS wallet_import_rows (
    import_id   INTEGER NOT NULL,
    miu_user_id TEXT NOT NULL,
    platform    TEXT NOT NULL,
    username    TEXT NOT NULL,
    user_uuid   TEXT,
    miu_hats    INTEGER NOT NULL DEFAULT 0,
    miu_tokens  INTEGER NOT NULL DEFAULT 0,
    miu_minutes INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    detail      TEXT,
    PRIMARY KEY (import_id, miu_user_id)
);
"""


async def ensure_schema(db) -> None:
    """Create the wallet tables. Idempotent; registered with
    core.db.register_schema() by main.py right after core.users."""
    await db.executescript(SCHEMA_SQL)
    cols = set()
    async with db.execute("PRAGMA table_info(wallet_import_rows)") as cur:
        async for r in cur:
            cols.add(r[1])
    if "miu_minutes" not in cols:
        await db.execute("ALTER TABLE wallet_import_rows ADD COLUMN "
                         "miu_minutes INTEGER NOT NULL DEFAULT 0")
    await db.commit()


def _check(asset: str, reason: str, amount: int) -> int:
    if asset not in ASSETS:
        raise ValueError(f"unknown asset {asset!r}")
    if reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    amount = int(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    return amount


# ── reads ──────────────────────────────────────────────────────────────

async def get(db, user_uuid: Optional[str], asset: str = "hats") -> int:
    """Current balance; 0 for a user with no row."""
    if not user_uuid:
        return 0
    async with db.execute(
            "SELECT amount FROM wallet_balances WHERE user_uuid = ? "
            "AND asset = ?", (user_uuid, asset)) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def get_all(db, user_uuid: Optional[str]) -> Dict[str, int]:
    out = {a: 0 for a in ASSETS}
    if not user_uuid:
        return out
    async with db.execute(
            "SELECT asset, amount FROM wallet_balances WHERE user_uuid = ?",
            (user_uuid,)) as cur:
        async for asset, amount in cur:
            out[asset] = int(amount)
    return out


async def leaderboard(db, asset: str = "hats", limit: int = 10,
                      excluded: Optional[Set[str]] = None
                      ) -> List[Dict[str, Any]]:
    """Top balances with display names and watch time, skipping
    excluded uuids and viewers who opted out of leaderboards."""
    excluded = excluded or set()
    ph = ",".join("?" for _ in excluded)
    not_excl = f" AND b.user_uuid NOT IN ({ph})" if excluded else ""
    rows = []
    async with db.execute(f"""
        SELECT b.user_uuid, COALESCE(u.display_name, b.user_uuid), b.amount,
               COALESCE(u.watch_minutes, 0)
          FROM wallet_balances b
          LEFT JOIN users u ON u.uuid = b.user_uuid
         WHERE b.asset = ? AND b.amount > 0
           AND COALESCE(u.leaderboard_opt_out, 0) = 0{not_excl}
         ORDER BY b.amount DESC, b.user_uuid LIMIT ?
    """, (asset,) + tuple(sorted(excluded)) + (int(limit),)) as cur:
        async for uuid_, name, amount, watched in cur:
            rows.append({"rank": len(rows) + 1, "user_uuid": uuid_,
                         "display_name": name, "amount": int(amount),
                         "watch_minutes": int(watched or 0)})
    return rows


async def holder_count(db, asset: str = "hats",
                       excluded: Optional[Set[str]] = None) -> int:
    """How many viewers hold a positive balance (leaderboard denominator);
    excluded uuids and opt-outs are not counted."""
    excluded = excluded or set()
    ph = ",".join("?" for _ in excluded)
    not_excl = f" AND b.user_uuid NOT IN ({ph})" if excluded else ""
    async with db.execute(f"""
        SELECT COUNT(*) FROM wallet_balances b
          LEFT JOIN users u ON u.uuid = b.user_uuid
         WHERE b.asset = ? AND b.amount > 0
           AND COALESCE(u.leaderboard_opt_out, 0) = 0{not_excl}
    """, (asset,) + tuple(sorted(excluded))) as cur:
        return int((await cur.fetchone())[0])


async def history(db, user_uuid: str, limit: int = 20,
                  asset: Optional[str] = None) -> List[Dict[str, Any]]:
    where = "WHERE user_uuid = ?"
    params: tuple = (user_uuid,)
    if asset:
        where += " AND asset = ?"
        params += (asset,)
    out = []
    async with db.execute(
            f"SELECT id, asset, delta, balance_after, reason, ref, actor, "
            f"channel, note, created_at FROM wallet_ledger {where} "
            f"ORDER BY id DESC LIMIT ?", params + (int(limit),)) as cur:
        async for r in cur:
            out.append({"id": r[0], "asset": r[1], "delta": r[2],
                        "balance_after": r[3], "reason": r[4], "ref": r[5],
                        "actor": r[6], "channel": r[7], "note": r[8],
                        "created_at": r[9]})
    return out


# ── writes ─────────────────────────────────────────────────────────────

async def _ledger(db, user_uuid: str, asset: str, delta: int,
                  balance_after: int, reason: str, ref: Optional[str],
                  actor: str, channel: str, note: Optional[str]) -> bool:
    """Insert the ledger row. False when (reason, ref) already exists."""
    try:
        await db.execute(
            "INSERT INTO wallet_ledger (user_uuid, asset, delta, balance_after, "
            "reason, ref, actor, channel, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_uuid, asset, delta, balance_after, reason, ref, actor,
             channel, note))
        return True
    except sqlite3.IntegrityError:
        return False


async def _ref_exists(db, reason: str, ref: Optional[str]) -> bool:
    if ref is None:
        return False
    async with db.execute(
            "SELECT 1 FROM wallet_ledger WHERE reason = ? AND ref = ? LIMIT 1",
            (reason, ref)) as cur:
        return await cur.fetchone() is not None


async def credit(db, user_uuid: str, asset: str, amount: int, reason: str,
                 ref: Optional[str] = None, actor: str = "system",
                 channel: str = "chat", note: Optional[str] = None,
                 commit: bool = True) -> Optional[int]:
    """Add `amount`. Returns the new balance, or None when this
    (reason, ref) was already applied (nothing changes)."""
    amount = _check(asset, reason, amount)
    if not user_uuid:
        raise ValueError("no user")
    if await _ref_exists(db, reason, ref):
        return None
    await db.execute(
        "INSERT INTO wallet_balances (user_uuid, asset, amount) VALUES (?, ?, 0) "
        "ON CONFLICT(user_uuid, asset) DO NOTHING", (user_uuid, asset))
    await db.execute(
        "UPDATE wallet_balances SET amount = amount + ?, "
        "updated_at = datetime('now') WHERE user_uuid = ? AND asset = ?",
        (amount, user_uuid, asset))
    after = await get(db, user_uuid, asset)
    ok = await _ledger(db, user_uuid, asset, amount, after, reason, ref,
                       actor, channel, note)
    if not ok:  # lost a race on the same ref: undo the balance move
        await db.execute(
            "UPDATE wallet_balances SET amount = amount - ? "
            "WHERE user_uuid = ? AND asset = ?", (amount, user_uuid, asset))
        if commit:
            await db.commit()
        return None
    if commit:
        await db.commit()
    return after


async def debit(db, user_uuid: str, asset: str, amount: int, reason: str,
                ref: Optional[str] = None, actor: str = "system",
                channel: str = "chat", note: Optional[str] = None,
                commit: bool = True) -> Optional[int]:
    """Take `amount`. Returns the new balance, or None when the balance
    is insufficient or this (reason, ref) was already applied."""
    amount = _check(asset, reason, amount)
    if not user_uuid:
        raise ValueError("no user")
    if await _ref_exists(db, reason, ref):
        return None
    cur = await db.execute(
        "UPDATE wallet_balances SET amount = amount - ?, "
        "updated_at = datetime('now') "
        "WHERE user_uuid = ? AND asset = ? AND amount >= ?",
        (amount, user_uuid, asset, amount))
    if cur.rowcount != 1:
        if commit:
            await db.commit()
        return None
    after = await get(db, user_uuid, asset)
    ok = await _ledger(db, user_uuid, asset, -amount, after, reason, ref,
                       actor, channel, note)
    if not ok:
        await db.execute(
            "UPDATE wallet_balances SET amount = amount + ? "
            "WHERE user_uuid = ? AND asset = ?", (amount, user_uuid, asset))
        if commit:
            await db.commit()
        return None
    if commit:
        await db.commit()
    return after


async def adjust(db, user_uuid: str, asset: str, delta: int, reason: str,
                 **kw) -> Optional[int]:
    """credit() for positive deltas, debit() for negative ones."""
    delta = int(delta)
    if delta == 0:
        return await get(db, user_uuid, asset)
    if delta > 0:
        return await credit(db, user_uuid, asset, delta, reason, **kw)
    return await debit(db, user_uuid, asset, -delta, reason, **kw)


async def mark_earned(db, user_uuid: str, when: Optional[str] = None,
                      commit: bool = False) -> None:
    await db.execute(
        "UPDATE wallet_balances SET last_earned_at = COALESCE(?, datetime('now')) "
        "WHERE user_uuid = ? AND asset = 'hats'", (when, user_uuid))
    if commit:
        await db.commit()


async def last_earned(db, user_uuids: Iterable[str]) -> Dict[str, Optional[str]]:
    ids = [u for u in set(user_uuids) if u]
    out: Dict[str, Optional[str]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        async with db.execute(
                f"SELECT user_uuid, last_earned_at FROM wallet_balances "
                f"WHERE asset = 'hats' AND user_uuid IN ({ph})",
                tuple(chunk)) as cur:
            async for uu, ts in cur:
                out[uu] = ts
    return out


async def record_tick(db, was_live: bool, chatters: int, credited: int,
                      skipped: int, hats_total: int,
                      error: Optional[str] = None, commit: bool = True) -> int:
    cur = await db.execute(
        "INSERT INTO wallet_earn_ticks (was_live, chatters, credited, skipped, "
        "hats_total, error) VALUES (?, ?, ?, ?, ?, ?)",
        (1 if was_live else 0, int(chatters), int(credited), int(skipped),
         int(hats_total), error))
    if commit:
        await db.commit()
    return int(cur.lastrowid)


async def recent_ticks(db, limit: int = 20) -> List[Dict[str, Any]]:
    out = []
    async with db.execute(
            "SELECT id, ticked_at, was_live, chatters, credited, skipped, "
            "hats_total, error FROM wallet_earn_ticks ORDER BY id DESC LIMIT ?",
            (int(limit),)) as cur:
        async for r in cur:
            out.append({"id": r[0], "ticked_at": r[1], "was_live": bool(r[2]),
                        "chatters": r[3], "credited": r[4], "skipped": r[5],
                        "hats_total": r[6], "error": r[7]})
    return out


# ── audit ──────────────────────────────────────────────────────────────

async def audit(db) -> List[Tuple[str, str, int, int]]:
    """(user_uuid, asset, balance, ledger_sum) for every mismatch."""
    out = []
    async with db.execute("""
        SELECT b.user_uuid, b.asset, b.amount, COALESCE(l.total, 0)
          FROM wallet_balances b
          LEFT JOIN (SELECT user_uuid, asset, SUM(delta) AS total
                       FROM wallet_ledger GROUP BY user_uuid, asset) l
            ON l.user_uuid = b.user_uuid AND l.asset = b.asset
         WHERE b.amount <> COALESCE(l.total, 0)
    """) as cur:
        async for r in cur:
            out.append((r[0], r[1], int(r[2]), int(r[3])))
    # ledger rows for users with no balance row at all
    async with db.execute("""
        SELECT l.user_uuid, l.asset, SUM(l.delta)
          FROM wallet_ledger l
          LEFT JOIN wallet_balances b
            ON b.user_uuid = l.user_uuid AND b.asset = l.asset
         WHERE b.user_uuid IS NULL
         GROUP BY l.user_uuid, l.asset
    """) as cur:
        async for r in cur:
            out.append((r[0], r[1], 0, int(r[2])))
    return out


async def fix_balance(db, user_uuid: str, asset: str, actor: str,
                      commit: bool = True) -> Optional[int]:
    """Rewrite the cached balance from the ledger sum and record an
    'adjust' row explaining the correction. Returns the delta."""
    async with db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM wallet_ledger "
            "WHERE user_uuid = ? AND asset = ?", (user_uuid, asset)) as cur:
        total = int((await cur.fetchone())[0])
    current = await get(db, user_uuid, asset)
    if total == current:
        return 0
    target = max(total, 0)
    await db.execute(
        "INSERT INTO wallet_balances (user_uuid, asset, amount) VALUES (?, ?, ?) "
        "ON CONFLICT(user_uuid, asset) DO UPDATE SET amount = excluded.amount, "
        "updated_at = datetime('now')", (user_uuid, asset, target))
    await db.execute(
        "INSERT INTO wallet_ledger (user_uuid, asset, delta, balance_after, "
        "reason, actor, channel, note) VALUES (?, ?, ?, ?, 'adjust', ?, 'system', ?)",
        (user_uuid, asset, target - total, target, actor,
         f"audit: balance {current} vs ledger {total}"))
    if commit:
        await db.commit()
    return target - current
