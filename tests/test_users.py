"""
Tests for core/users.py (one uuid per viewer across Twitch and YouTube)
and the economy's login-era -> user_uuid migration.

Self-running script per house convention: exit 0 only on full pass.
Hermetic: in-memory / temp SQLite, no network, no bot, no live data.

    python tests/test_users.py            # all
    python tests/test_users.py merge      # name filter
"""

import asyncio
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aiosqlite  # noqa: E402

from core import users as U  # noqa: E402


ECON_TABLES = """
CREATE TABLE god_prices (god_name TEXT PRIMARY KEY, price REAL);
CREATE TABLE portfolios (user_uuid TEXT NOT NULL, god_name TEXT NOT NULL,
  shares REAL NOT NULL DEFAULT 0, avg_cost REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (user_uuid, god_name));
CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT, user_uuid TEXT, god_name TEXT NOT NULL, type TEXT NOT NULL,
  shares REAL NOT NULL, price REAL NOT NULL, total REAL NOT NULL,
  fee REAL NOT NULL DEFAULT 0, timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  channel TEXT NOT NULL DEFAULT 'chat', ref TEXT);
CREATE TABLE god_pool_votes (user_uuid TEXT NOT NULL, vote_date TEXT NOT NULL,
  god_name TEXT NOT NULL, voted_at TEXT, use_aspect INTEGER DEFAULT 0,
  voter_username TEXT, PRIMARY KEY (user_uuid, vote_date));
CREATE TABLE priority_payments (stripe_session_id TEXT PRIMARY KEY,
  twitch_username TEXT, god TEXT, user_uuid TEXT);
"""


async def make_db(econ=True):
    db = await aiosqlite.connect(":memory:")
    await U.ensure_schema(db)
    if econ:
        for stmt in ECON_TABLES.strip().split(";"):
            if stmt.strip():
                await db.execute(stmt)
        await db.commit()
    U.clear_cache(db)
    return db


async def holding(db, uid, god):
    async with db.execute(
            "SELECT shares, avg_cost FROM portfolios WHERE user_uuid = ? "
            "AND god_name = ?", (uid, god)) as cur:
        row = await cur.fetchone()
    return (round(row[0], 6), round(row[1], 6)) if row else None


# ── identities ─────────────────────────────────────────────────────────

async def test_twitch_create_placeholder_upgrade_and_rename():
    db = await make_db(econ=False)
    try:
        # login-only sighting (migration / typed username) -> placeholder
        a = await U.get_or_create_twitch_login(db, "Dyna", "Dyna")
        ids = await U.identities(db, a)
        assert ids[0]["provider_id"] == "login:dyna" and ids[0]["login"] == "dyna"
        assert await U.find_twitch_login(db, "DYNA") == a
        assert await U.find_twitch_id(db, "42") is None
        # first sighting with the real id upgrades the same row
        b = await U.get_or_create_twitch(db, "42", "dyna", "Dyna!", "img")
        assert b == a
        ids = await U.identities(db, a)
        assert len(ids) == 1 and ids[0]["provider_id"] == "42"
        assert (await U.get_user(db, a))["display_name"] == "Dyna!"
        assert (await U.get_user(db, a))["avatar_url"] == "img"
        assert await U.twitch_login_of(db, a) == "dyna"
        # a rename: same id, new login; the old login no longer resolves
        assert await U.get_or_create_twitch(db, "42", "dynamite") == a
        assert await U.twitch_login_of(db, a) == "dynamite"
        assert await U.find_twitch_login(db, "dyna") is None
        # someone else takes the old name -> a different person
        c = await U.get_or_create_twitch(db, "77", "dyna")
        assert c != a and await U.find_twitch_login(db, "dyna") == c
        # bare twitch id without login still works
        assert await U.get_or_create_twitch(db, "77", "") == c
        assert await U.public_ref(db, a) == ("twitch", "dynamite")
        # watch time: grows, floors (never lowers), formats
        await U.add_watch_minutes(db, a, 5)
        await U.floor_watch_minutes(db, a, 3)
        assert await U.watch_minutes_of(db, a) == 5
        await U.floor_watch_minutes(db, a, 1500)
        assert await U.watch_minutes_of(db, a) == 1500
        assert U.format_watch(1500) == "1d 1h 0m" and U.format_watch(65) == "1h 5m"
        assert U.format_watch(7) == "7m" and U.format_watch(0) == "0m"
    finally:
        await db.close()


async def test_youtube_create_and_public_ref():
    db = await make_db(econ=False)
    try:
        y = await U.get_or_create_youtube(db, "UCabc", "YT Viewer")
        assert await U.find_youtube(db, "UCabc") == y
        assert await U.get_or_create_youtube(db, "UCabc") == y
        assert await U.youtube_channel_of(db, y) == "UCabc"
        assert await U.twitch_login_of(db, y) is None
        assert await U.public_ref(db, y) == ("youtube", "UCabc")
        assert await U.display_name_of(db, y) == "YT Viewer"
        try:
            await U.get_or_create_youtube(db, "")
            assert False, "empty channel accepted"
        except ValueError:
            pass
    finally:
        await db.close()


async def test_opt_out_and_excluded():
    db = await make_db(econ=False)
    try:
        a = await U.get_or_create_twitch(db, "1", "viewer")
        bot = await U.get_or_create_twitch(db, "2", "nightbot")
        assert not await U.get_leaderboard_opt_out(db, a)
        await U.set_leaderboard_opt_out(db, a, True)
        assert await U.get_leaderboard_opt_out(db, a)
        ex = await U.excluded_uuids(db, ["NightBot", "streamelements"])
        assert ex == {bot}
        clause, params = U.sql_not_excluded("p.user_uuid", ex)
        assert clause == " AND p.user_uuid NOT IN (?)" and params == (bot,)
        assert U.sql_not_excluded("x", set()) == ("", ())
    finally:
        await db.close()


# ── linking + merging ──────────────────────────────────────────────────

async def test_link_youtube_unknown_and_same():
    db = await make_db()
    try:
        tw = await U.get_or_create_twitch(db, "1", "dyna", "Dyna")
        r = await U.link_youtube(db, tw, "UCnew", "Dyna on YT")
        assert r == {"ok": True, "already": False, "merged": None}
        assert await U.find_youtube(db, "UCnew") == tw
        r = await U.link_youtube(db, tw, "UCnew")
        assert r["ok"] and r["already"]
        assert len(await U.identities(db, tw)) == 2
        # another person's channel (already tied to a Twitch account)
        other = await U.get_or_create_twitch(db, "2", "bob")
        r = await U.link_youtube(db, other, "UCnew")
        assert r == {"ok": False, "reason": "linked_elsewhere"}
        assert await U.link_youtube(db, "nope", "UCx") == {"ok": False, "reason": "no_user"}
    finally:
        await db.close()


async def test_merge_rules():
    db = await make_db()
    try:
        await db.execute("INSERT INTO god_prices VALUES ('Ymir', 200)")
        tw = await U.get_or_create_twitch(db, "1", "dyna", "Dyna")
        yt = await U.get_or_create_youtube(db, "UCabc", "Dyna on YT")
        # positions: both hold Ymir, only yt holds Loki
        await db.execute("INSERT INTO portfolios VALUES (?, 'Ymir', 10, 100)", (tw,))
        await db.execute("INSERT INTO portfolios VALUES (?, 'Ymir', 10, 300)", (yt,))
        await db.execute("INSERT INTO portfolios VALUES (?, 'Loki', 3, 50)", (yt,))
        for uid in (tw, yt):
            await db.execute(
                "INSERT INTO transactions (user_uuid, god_name, type, shares, price, total) "
                "VALUES (?, 'Ymir', 'buy', 1, 1, 1)", (uid,))
        # daily vote singleton: same day on both sides -> survivor's stays
        await db.execute("INSERT INTO god_pool_votes (user_uuid, vote_date, god_name) "
                         "VALUES (?, '2026-09-12', 'Ymir')", (tw,))
        await db.execute("INSERT INTO god_pool_votes (user_uuid, vote_date, god_name) "
                         "VALUES (?, '2026-09-12', 'Loki')", (yt,))
        await db.execute("INSERT INTO god_pool_votes (user_uuid, vote_date, god_name) "
                         "VALUES (?, '2026-09-11', 'Loki')", (yt,))
        await db.execute("INSERT INTO priority_payments VALUES ('cs', 'x', 'Ymir', ?)", (yt,))
        await U.set_leaderboard_opt_out(db, yt, True)
        await U.add_watch_minutes(db, tw, 10)
        await U.add_watch_minutes(db, yt, 7)
        await db.commit()

        hooks = []
        async def hook(a, b):
            hooks.append((a, b)); return "moved"
        U.register_merge_hook(hook)

        # link = merge: the standalone YouTube user folds into the Twitch one
        r = await U.link_youtube(db, tw, "UCabc", initiated_by="user")
        assert r["ok"] and not r["already"] and r["merged"]["survivor"] == tw
        assert hooks == [(yt, tw)]

        assert await holding(db, tw, "Ymir") == (20.0, 200.0)   # weighted average
        assert await holding(db, tw, "Loki") == (3.0, 50.0)
        assert await holding(db, yt, "Ymir") is None
        async with db.execute("SELECT COUNT(*) FROM transactions WHERE user_uuid = ?", (tw,)) as c:
            assert (await c.fetchone())[0] == 4          # 2 originals + 2 merge_in rows
        async with db.execute("SELECT god_name, ref FROM transactions WHERE type = 'merge_in' "
                              "ORDER BY god_name") as c:
            rows = await c.fetchall()
        assert [r[0] for r in rows] == ["Loki", "Ymir"] and all(r[1] == yt for r in rows)
        async with db.execute("SELECT vote_date, god_name FROM god_pool_votes WHERE user_uuid = ? "
                              "ORDER BY vote_date", (tw,)) as c:
            assert await c.fetchall() == [("2026-09-11", "Loki"), ("2026-09-12", "Ymir")]
        async with db.execute("SELECT user_uuid FROM priority_payments") as c:
            assert (await c.fetchone())[0] == tw
        # identities all point at the survivor; the absorbed row resolves to it
        assert {i["provider"] for i in await U.identities(db, tw)} == {"twitch", "youtube"}
        assert await U.resolve(db, yt) == tw
        assert await U.find_youtube(db, "UCabc") == tw
        assert (await U.get_user(db, yt))["uuid"] == tw
        # survivor keeps its own opt-out (rule 4); watch time adds up
        assert not await U.get_leaderboard_opt_out(db, tw)
        assert await U.watch_minutes_of(db, tw) == 17
        async with db.execute("SELECT absorbed_uuid, survivor_uuid, initiated_by, summary "
                              "FROM user_merges") as c:
            m = await c.fetchone()
        assert m[0] == yt and m[1] == tw and m[2] == "user" and '"hooks"' in m[3]
        # merging again is a no-op
        assert (await U.merge(db, yt, tw))["noop"]
        # survivor rule: twitch beats youtube whichever order is asked
        y2 = await U.get_or_create_youtube(db, "UCz")
        assert await U.pick_survivor(db, y2, tw) == (tw, y2)
    finally:
        U._merge_hooks.clear()
        await db.close()


async def test_attach_user_uuid_backfill():
    db = await make_db(econ=False)
    try:
        await db.execute("CREATE TABLE things (id INTEGER PRIMARY KEY, username TEXT)")
        for n in ("Dyna", "dyna", "bob", None, ""):
            await db.execute("INSERT INTO things (username) VALUES (?)", (n,))
        n = await U.attach_user_uuid(db, "things", "username", "twitch")
        await db.commit()
        assert n == 3
        async with db.execute("SELECT username, user_uuid FROM things ORDER BY id") as c:
            rows = await c.fetchall()
        assert rows[0][1] == rows[1][1] and rows[2][1] not in (None, rows[0][1])
        assert rows[3][1] is None and rows[4][1] is None
        assert await U.find_twitch_login(db, "dyna") == rows[0][1]
        # idempotent
        assert await U.attach_user_uuid(db, "things", "username", "twitch") == 0
        assert await U.attach_user_uuid(db, "missing", "username", "twitch") == 0
    finally:
        await db.close()


# ── the economy's one-shot migration on a login-era database ───────────

LEGACY = """
CREATE TABLE god_prices (god_name TEXT PRIMARY KEY, price REAL NOT NULL DEFAULT 100,
  games_played INTEGER NOT NULL DEFAULT 0, total_wins INTEGER NOT NULL DEFAULT 0,
  total_losses INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
CREATE TABLE price_history (id INTEGER PRIMARY KEY AUTOINCREMENT, god_name TEXT,
  price REAL, event TEXT, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE portfolios (username TEXT NOT NULL, god_name TEXT NOT NULL,
  shares REAL NOT NULL DEFAULT 0, avg_cost REAL NOT NULL DEFAULT 0,
  leaderboard_opt_out INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (username, god_name));
CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
  god_name TEXT NOT NULL, type TEXT NOT NULL, shares REAL NOT NULL, price REAL NOT NULL,
  total REAL NOT NULL, fee REAL NOT NULL DEFAULT 0,
  timestamp TEXT NOT NULL DEFAULT (datetime('now')), channel TEXT NOT NULL DEFAULT 'chat');
CREATE TABLE dividends (id INTEGER PRIMARY KEY AUTOINCREMENT, god_name TEXT, rate REAL,
  price REAL, total_hats REAL, holders INTEGER, match_id TEXT, timestamp TEXT);
CREATE TABLE processed_matches (match_id TEXT PRIMARY KEY, god_name TEXT, outcome TEXT,
  kills INTEGER, deaths INTEGER, assists INTEGER, price_change REAL, source TEXT,
  was_live_at_settle INTEGER, processed_at TEXT, played_at TEXT);
CREATE TABLE youtube_portfolios (yt_channel_id TEXT PRIMARY KEY, yt_display_name TEXT NOT NULL,
  first_seen_at TEXT NOT NULL DEFAULT '2026-01-01 00:00:00', last_seen_at TEXT,
  leaderboard_opt_out INTEGER NOT NULL DEFAULT 0);
CREATE TABLE youtube_holdings (yt_channel_id TEXT NOT NULL, god_name TEXT NOT NULL,
  shares REAL NOT NULL DEFAULT 0, avg_cost REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (yt_channel_id, god_name));
CREATE TABLE youtube_transactions (id INTEGER PRIMARY KEY AUTOINCREMENT,
  yt_channel_id TEXT NOT NULL, god_name TEXT NOT NULL, type TEXT NOT NULL,
  shares REAL NOT NULL, price REAL NOT NULL, yt_video_id TEXT,
  timestamp TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE account_links (yt_channel_id TEXT PRIMARY KEY, twitch_login TEXT NOT NULL,
  linked_at TEXT);
INSERT INTO god_prices (god_name, price) VALUES ('Ymir', 200), ('Loki', 100);
INSERT INTO portfolios VALUES ('Dyna', 'Ymir', 4, 100, 1);
INSERT INTO portfolios VALUES ('dyna', 'Ymir', 4, 300, 0);
INSERT INTO portfolios VALUES ('bob', 'Loki', 2, 90, 0);
INSERT INTO transactions (username, god_name, type, shares, price, total) VALUES
  ('Dyna', 'Ymir', 'buy', 4, 100, 400), ('dyna', 'Ymir', 'buy', 4, 300, 1200),
  ('bob', 'Loki', 'buy', 2, 90, 180);
INSERT INTO youtube_portfolios (yt_channel_id, yt_display_name, leaderboard_opt_out)
  VALUES ('UCbob', 'Bob on YT', 1), ('UCsolo', 'Solo', 0);
INSERT INTO youtube_holdings VALUES ('UCsolo', 'Loki', 5, 80), ('UCbob', 'Ymir', 1, 0);
INSERT INTO youtube_transactions (yt_channel_id, god_name, type, shares, price, yt_video_id)
  VALUES ('UCsolo', 'Loki', 'comment_share', 5, 80, 'vid1'),
         ('UCbob', 'Ymir', 'comment_share', 1, 0, 'vid2');
INSERT INTO account_links VALUES ('UCbob', 'bob', '2026-08-01');
"""


async def test_economy_migration_rekeys_legacy_db():
    from plugins.economy import EconomyPlugin
    d = Path(tempfile.mkdtemp(prefix="users_mig_"))
    db = await aiosqlite.connect(str(d / "economy.db"))
    try:
        await db.executescript(LEGACY)
        eco = EconomyPlugin()
        await eco._init_schema(db)

        dyna = await U.find_twitch_login(db, "dyna")
        bob = await U.find_twitch_login(db, "bob")
        solo = await U.find_youtube(db, "UCsolo")
        assert dyna and bob and solo and len({dyna, bob, solo}) == 3
        # case variants of one login collapse into one weighted position
        assert await holding(db, dyna, "Ymir") == (8.0, 200.0)
        assert await U.get_leaderboard_opt_out(db, dyna)
        # the YouTube cluster folded into portfolios + transactions
        assert await holding(db, solo, "Loki") == (5.0, 80.0)
        async with db.execute("SELECT channel, ref FROM transactions WHERE user_uuid = ?",
                              (solo,)) as c:
            assert await c.fetchall() == [("youtube", "vid1")]
        # account_links became an identity link: bob's channel is bob
        assert await U.find_youtube(db, "UCbob") == bob
        assert await holding(db, bob, "Ymir") == (1.0, 0.0)
        assert await holding(db, bob, "Loki") == (2.0, 90.0)
        # every transaction row carries a uuid; legacy tables are parked
        async with db.execute("SELECT COUNT(*) FROM transactions WHERE user_uuid IS NULL") as c:
            assert (await c.fetchone())[0] == 0
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name LIKE '_legacy_%' ORDER BY name") as c:
            names = [r[0] for r in await c.fetchall()]
        assert names == ["_legacy_account_links", "_legacy_portfolios",
                         "_legacy_youtube_holdings", "_legacy_youtube_portfolios",
                         "_legacy_youtube_transactions"]
        # a new-shape row can be written without username
        await db.execute("INSERT INTO transactions (user_uuid, god_name, type, shares, "
                         "price, total) VALUES (?, 'Ymir', 'buy', 1, 1, 1)", (dyna,))
        # second run is a no-op
        before = await holding(db, dyna, "Ymir")
        await eco._init_schema(db)
        assert await holding(db, dyna, "Ymir") == before
        async with db.execute("SELECT COUNT(*) FROM users WHERE merged_into IS NULL") as c:
            assert (await c.fetchone())[0] == 3
    finally:
        await db.close()


# ── harness ────────────────────────────────────────────────────────────

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


async def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    passed = failed = 0
    for t in TESTS:
        if name_filter and name_filter not in t.__name__:
            continue
        try:
            await t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed or not passed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
