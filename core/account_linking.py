"""
core/account_linking.py — YouTube channel ↔ Twitch login linking
================================================================

Answers the cross-platform merge question WEBSITE_TRADING_DESIGN.md §9
deferred: a viewer who proves ownership of BOTH identities (Twitch
session cookie + Google OAuth in the same browser) gets their YouTube
shares folded into their Twitch portfolio, one time, permanently.

Why one-way migrate instead of keep-both-display-combined: hats live
in MixItUp keyed to Twitch users, so YouTube-side holdings can never
earn hat dividends — they compound as bonus shares instead
(plugins/economy/dividends.py). After migration the shares sit in
`portfolios` where dividends pay real hats, trades work, and the
leaderboard sees one person once.

Ownership proof is the caller's job (core/public_webserver.py's
/auth/google/callback link mode). This module owns the data moves:

  * account_links table — the durable record. A YouTube channel links
    to exactly one Twitch login (PK); one Twitch login may link
    several channels (people have alt channels).
  * link_and_migrate() — writes the link, then moves every
    youtube_holdings row into portfolios with weighted-average cost
    basis, ledgering both sides ('yt_merge_in' in transactions,
    'merged_to_twitch' in youtube_transactions).
  * grant_target() — after linking, comment-share grants for that
    channel route to the Twitch portfolio (plugins/youtube_rewards.py
    checks this before writing youtube_holdings).

Everything here runs on the shared economy.db connection (core/db.py).
No config, no network — unit-tests in isolation against in-memory
aiosqlite (tests/test_account_linking.py).
"""

from __future__ import annotations

from typing import List, Optional

ACCOUNT_LINKS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS account_links (
        yt_channel_id  TEXT PRIMARY KEY,
        twitch_login   TEXT NOT NULL,
        linked_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_account_links_twitch
        ON account_links(twitch_login);
"""


async def ensure_schema(db) -> None:
    """Create the link table if missing. Idempotent."""
    await db.executescript(ACCOUNT_LINKS_SCHEMA_SQL)
    await db.commit()


async def get_link(db, yt_channel_id: str) -> Optional[str]:
    """Twitch login this channel is linked to, or None."""
    async with db.execute(
            "SELECT twitch_login FROM account_links "
            "WHERE yt_channel_id = ?", (yt_channel_id,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def get_links_for_twitch(db, twitch_login: str) -> List[str]:
    """All YouTube channel ids linked to this Twitch login."""
    out = []
    async with db.execute(
            "SELECT yt_channel_id FROM account_links "
            "WHERE twitch_login = ?",
            ((twitch_login or "").lower(),)) as cur:
        async for row in cur:
            out.append(row[0])
    return out


async def grant_target(db, yt_channel_id: str) -> Optional[str]:
    """Where a share grant for this channel should land: the linked
    Twitch login, or None (= the normal youtube_holdings path)."""
    return await get_link(db, yt_channel_id)


async def grant_to_twitch(db, twitch_login: str, god: str,
                          shares: float, price: float,
                          txn_type: str = "yt_comment_share") -> None:
    """Add shares to a Twitch portfolio with weighted-average cost
    basis + a transactions ledger row. Mirrors the math of
    youtube_rewards._grant_shares, aimed at the Twitch tables. The
    caller commits (keeps grant + processed-comment marking in the
    same commit)."""
    login = (twitch_login or "").lower()
    async with db.execute(
            "SELECT shares, avg_cost FROM portfolios "
            "WHERE username = ? AND god_name = ?",
            (login, god)) as cur:
        row = await cur.fetchone()
    old_shares = float(row[0]) if row else 0.0
    old_avg = float(row[1]) if row else 0.0

    new_shares = old_shares + shares
    new_avg = (((old_shares * old_avg) + (shares * price)) / new_shares
               if new_shares > 0 else 0.0)

    await db.execute("""
        INSERT INTO portfolios (username, god_name, shares, avg_cost)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(username, god_name) DO UPDATE SET
            shares   = excluded.shares,
            avg_cost = excluded.avg_cost
    """, (login, god, new_shares, new_avg))
    await db.execute("""
        INSERT INTO transactions
            (username, god_name, type, shares, price, total, fee)
        VALUES (?, ?, ?, ?, ?, ?, 0)
    """, (login, god, txn_type, shares, price, shares * price))


async def link_and_migrate(db, yt_channel_id: str,
                           twitch_login: str) -> dict:
    """Link a proven YouTube channel to a proven Twitch login and
    fold its YouTube holdings into the Twitch portfolio.

    Idempotent + conflict-safe:
      already linked to this login    -> {"ok": True, "already": True}
      linked to a DIFFERENT login     -> {"ok": False,
                                          "reason": "linked_elsewhere"}
      fresh link                      -> {"ok": True, "migrated":
                                          [(god, shares), ...]}

    Cost basis carries over: the incoming shares keep their YouTube
    avg_cost and merge into any existing Twitch position as a
    weighted average — linking is a transfer, not a taxable event.
    Both ledgers record the move so replay_economy-style audits see
    shares leave one side and arrive on the other.
    """
    login = (twitch_login or "").lower()
    channel = (yt_channel_id or "").strip()
    if not login or not channel:
        return {"ok": False, "reason": "bad_args"}

    existing = await get_link(db, channel)
    if existing == login:
        return {"ok": True, "already": True, "migrated": []}
    if existing is not None:
        return {"ok": False, "reason": "linked_elsewhere"}

    await db.execute(
        "INSERT INTO account_links (yt_channel_id, twitch_login) "
        "VALUES (?, ?)", (channel, login))

    # Move every holding. Read first, then write — the row set is
    # tiny (a viewer holds a handful of gods at most).
    holdings = []
    async with db.execute(
            "SELECT god_name, shares, avg_cost FROM youtube_holdings "
            "WHERE yt_channel_id = ? AND shares > 0",
            (channel,)) as cur:
        async for row in cur:
            holdings.append((row[0], float(row[1]), float(row[2])))

    migrated = []
    for god, shares, avg_cost in holdings:
        await grant_to_twitch(db, login, god, shares, avg_cost,
                              txn_type="yt_merge_in")
        await db.execute("""
            INSERT INTO youtube_transactions
                (yt_channel_id, god_name, type, shares, price)
            VALUES (?, ?, 'merged_to_twitch', ?, ?)
        """, (channel, god, -shares, avg_cost))
        migrated.append((god, shares))

    # Clear the YouTube side so dividends/leaderboards can't count
    # the position twice. The youtube_portfolios row stays — it's
    # harmless metadata and the link table marks the merge.
    await db.execute(
        "DELETE FROM youtube_holdings WHERE yt_channel_id = ?",
        (channel,))
    await db.commit()

    return {"ok": True, "already": False, "migrated": migrated}
