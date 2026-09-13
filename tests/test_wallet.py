"""
Tests for core/wallet.py (Hats + God Tokens ledger) and the passive
earning tick in plugins/wallet.py.

Self-running script per house convention: exit 0 only on full pass.
Hermetic: in-memory SQLite, no network, no bot, no live data.

    python tests/test_wallet.py            # all
    python tests/test_wallet.py tick       # name filter
"""

import asyncio
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aiosqlite  # noqa: E402

from core import users as U  # noqa: E402
from core import wallet as W  # noqa: E402


async def make_db():
    db = await aiosqlite.connect(":memory:")
    await U.ensure_schema(db)
    await W.ensure_schema(db)
    U.clear_cache(db)
    return db


async def ledger_sum(db, uid, asset="hats"):
    async with db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM wallet_ledger "
            "WHERE user_uuid = ? AND asset = ?", (uid, asset)) as c:
        return int((await c.fetchone())[0])


# ── credit / debit ─────────────────────────────────────────────────────

async def test_credit_debit_and_floor():
    db = await make_db()
    try:
        u = await U.get_or_create_twitch(db, "1", "dyna", "Dyna")
        assert await W.get(db, u) == 0
        assert await W.credit(db, u, "hats", 100, "watch") == 100
        assert await W.credit(db, u, "hats", 50, "mod_grant", actor="mod") == 150
        assert await W.debit(db, u, "hats", 120, "buy", note="Ymir") == 30
        # insufficient: nothing moves, no ledger row
        assert await W.debit(db, u, "hats", 31, "buy") is None
        assert await W.get(db, u) == 30 and await ledger_sum(db, u) == 30
        # adjust() dispatches on sign
        assert await W.adjust(db, u, "hats", -30, "gamble_loss") == 0
        assert await W.adjust(db, u, "hats", 7, "gamble_win") == 7
        assert await W.adjust(db, u, "hats", 0, "adjust") == 7
        # tokens are a separate asset
        assert await W.credit(db, u, "god_token", 2, "sub_award") == 2
        assert (await W.get_all(db, u)) == {"hats": 7, "god_token": 2}
        assert await W.debit(db, u, "god_token", 3, "godreq_spend") is None
        assert await W.debit(db, u, "god_token", 1, "godreq_spend") == 1
        # bad input never touches the tables
        for bad in ((u, "gold", 1, "watch"), (u, "hats", -1, "watch"),
                    (u, "hats", 1, "tip"), ("", "hats", 1, "watch")):
            try:
                await W.credit(db, *bad)
                assert False, f"accepted {bad}"
            except ValueError:
                pass
        hist = await W.history(db, u, 3)
        assert [h["reason"] for h in hist] == ["godreq_spend", "sub_award", "gamble_win"]
        assert hist[0]["asset"] == "god_token" and hist[0]["delta"] == -1
        assert await W.audit(db) == []
    finally:
        await db.close()


async def test_ref_idempotency():
    db = await make_db()
    try:
        u = await U.get_or_create_twitch(db, "1", "dyna")
        assert await W.credit(db, u, "hats", 25, "dividend", ref="m1:Ymir:" + u) == 25
        assert await W.credit(db, u, "hats", 25, "dividend", ref="m1:Ymir:" + u) is None
        assert await W.get(db, u) == 25
        # same ref under a different reason is a different event
        assert await W.credit(db, u, "hats", 5, "watch", ref="m1:Ymir:" + u) == 30
        # a debit with a used ref is refused too, balance untouched
        assert await W.debit(db, u, "hats", 5, "watch", ref="m1:Ymir:" + u) is None
        assert await W.get(db, u) == 30 and await ledger_sum(db, u) == 30
        # ref-less rows never collide
        assert await W.credit(db, u, "hats", 1, "watch") == 31
        assert await W.credit(db, u, "hats", 1, "watch") == 32
    finally:
        await db.close()


async def test_leaderboard_and_opt_out():
    db = await make_db()
    try:
        a = await U.get_or_create_twitch(db, "1", "a", "A")
        b = await U.get_or_create_twitch(db, "2", "b", "B")
        bot = await U.get_or_create_twitch(db, "3", "nightbot")
        hidden = await U.get_or_create_twitch(db, "4", "shy")
        for uid, n in ((a, 10), (b, 30), (bot, 999), (hidden, 500)):
            await W.credit(db, uid, "hats", n, "watch")
        await U.set_leaderboard_opt_out(db, hidden, True)
        rows = await W.leaderboard(db, "hats", 10, excluded={bot})
        assert [(r["rank"], r["display_name"], r["amount"]) for r in rows] == \
            [(1, "B", 30), (2, "A", 10)]
    finally:
        await db.close()


async def test_ledger_check_migration():
    """A DB created before priority_sr / burn existed must be rebuilt on
    ensure_schema, keeping every row and id (the 2026-09-13 bug: every
    !burn / !vipsr failed the CHECK and looked like an empty wallet)."""
    db = await aiosqlite.connect(":memory:")
    try:
        await U.ensure_schema(db)
        old_reasons = [r for r in W.REASONS if r not in ("priority_sr", "burn")]
        old_sql = W.SCHEMA_SQL.replace(
            ", ".join("'" + r + "'" for r in W.REASONS),
            ", ".join("'" + r + "'" for r in old_reasons))
        assert old_sql != W.SCHEMA_SQL
        await db.executescript(old_sql)
        u = await U.get_or_create_twitch(db, "1", "dyna")
        await W.credit(db, u, "hats", 1000, "migration", ref="miu:1:hats")
        # the old CHECK refuses the new reason, and that must NOT look like
        # an empty wallet: it raises
        try:
            await W.debit(db, u, "hats", 500, "burn")
            assert False, "old CHECK should have raised"
        except Exception as e:
            assert "CHECK" in str(e).upper(), e
        assert await W.get(db, u) == 1000
        assert await W._ledger_reasons_in_db(db) == set(old_reasons)
        assert await W.ensure_schema(db) is None
        assert await W._ledger_reasons_in_db(db) == set(W.REASONS)
        hist = await W.history(db, u)
        assert len(hist) == 1 and hist[0]["id"] == 1 and hist[0]["ref"] == "miu:1:hats"
        # same ref is still refused (unique index rebuilt), new reasons work
        assert await W.credit(db, u, "hats", 1000, "migration", ref="miu:1:hats") is None
        assert await W.debit(db, u, "hats", 500, "burn") == 500
        assert await W.debit(db, u, "hats", 200, "priority_sr") == 300
        assert await W.audit(db) == []
        # a second ensure_schema is a no-op
        assert await W._migrate_ledger_reasons(db) is False
    finally:
        await db.close()


async def test_audit_and_fix():
    db = await make_db()
    try:
        u = await U.get_or_create_twitch(db, "1", "dyna")
        await W.credit(db, u, "hats", 40, "watch")
        # corrupt the cache behind the ledger's back
        await db.execute("UPDATE wallet_balances SET amount = 55 WHERE user_uuid = ?", (u,))
        await db.commit()
        assert await W.audit(db) == [(u, "hats", 55, 40)]
        assert await W.fix_balance(db, u, "hats", actor="test") == -15
        assert await W.get(db, u) == 40 and await W.audit(db) == []
        hist = await W.history(db, u, 1)
        assert hist[0]["reason"] == "adjust" and hist[0]["delta"] == 0
        assert await W.fix_balance(db, u, "hats", actor="test") == 0
    finally:
        await db.close()


async def test_merge_adds_balances():
    db = await make_db()
    try:
        tw = await U.get_or_create_twitch(db, "1", "dyna")
        yt = await U.get_or_create_youtube(db, "UCabc", "Dyna YT")
        await W.credit(db, tw, "hats", 100, "watch")
        await W.credit(db, yt, "hats", 40, "watch")
        await W.credit(db, yt, "god_token", 1, "sub_award")
        r = await U.link_youtube(db, tw, "UCabc")
        assert r["ok"] and r["merged"]
        assert await W.get_all(db, tw) == {"hats": 140, "god_token": 1}
        assert await W.get_all(db, yt) == {"hats": 0, "god_token": 0}
        # the ledger rows moved with the balance, so the audit still holds
        assert await W.audit(db) == []
    finally:
        await db.close()


# ── passive earning tick ───────────────────────────────────────────────

def _plugin(db, live=True, **cfg):
    from plugins import wallet as wp
    for k, v in {"WALLET_EARN_HATS_PER_TICK": 25, "WALLET_EARN_INTERVAL_MIN": 5,
                 "WALLET_EARN_CHAT_BONUS": 0, "WALLET_EARN_SUB_MULTIPLIER": 1.0,
                 "WALLET_EARN_OFFLINE": False, "WALLET_BONUS_SUB": 0,
                 "ECONOMY_EXCLUDED_USERNAMES": ["nightbot"], **cfg}.items():
        setattr(wp._cfg, k, v)
    p = wp.WalletPlugin()
    p._db = db
    p.bot = types.SimpleNamespace(
        plugins={"stream_status": types.SimpleNamespace(
            get_status=lambda: {"is_live": live})},
        is_feature_enabled=lambda f: True,
        is_sub=lambda c: bool(getattr(c, "subscriber", False)),
        send_reply=None, send_chat=None)
    return p


async def test_tick_pays_chatters_once_per_window():
    db = await make_db()
    try:
        p = _plugin(db)
        chatters = [("10", "dyna"), ("11", "bob"), (None, "cat"), ("12", "nightbot"), ("10", "dyna")]
        r = await p.tick(chatters=chatters, now=1_700_000_000)
        assert r["was_live"] and r["chatters"] == 4 and r["credited"] == 3
        assert r["skipped"] == 1 and r["hats_total"] == 75
        dyna = await U.find_twitch_id(db, "10")
        cat = await U.find_twitch_login(db, "cat")
        assert await W.get(db, dyna) == 25 and await W.get(db, cat) == 25
        assert await U.watch_minutes_of(db, dyna) == 5      # one 5-minute tick
        assert await U.find_twitch_login(db, "nightbot") is None   # never created
        # a second pass inside half an interval (a restart) pays nobody again
        r2 = await p.tick(chatters=chatters[:2], now=1_700_000_000 + 60)
        assert r2["credited"] == 0 and r2["skipped"] == 2
        assert await W.get(db, dyna) == 25 and await U.watch_minutes_of(db, dyna) == 5
        ticks = await W.recent_ticks(db, 5)
        assert len(ticks) == 2 and ticks[1]["credited"] == 3 and ticks[1]["hats_total"] == 75
        assert (await W.history(db, dyna, 1))[0]["reason"] == "watch"
        assert await W.audit(db) == []
    finally:
        await db.close()


async def test_tick_offline_chat_bonus_and_sub_multiplier():
    db = await make_db()
    try:
        # offline: a tick row, nobody paid
        p = _plugin(db, live=False)
        r = await p.tick(chatters=[("1", "dyna")])
        assert not r["was_live"] and r["credited"] == 0
        ticks = await W.recent_ticks(db, 1)
        assert ticks[0]["was_live"] is False and ticks[0]["chatters"] == 0
        # live with a chat bonus + sub multiplier: dyna chatted as a sub,
        # bob chatted, cat lurked, eve chatted then left before the tick
        p = _plugin(db, live=True, WALLET_EARN_CHAT_BONUS=5, WALLET_EARN_SUB_MULTIPLIER=2.0)
        for login, sub, tid in (("dyna", True, "1"), ("bob", False, "2"), ("eve", False, "4")):
            await p._on_chat(types.SimpleNamespace(chatter=types.SimpleNamespace(
                name=login, subscriber=sub, id=tid)))
        r = await p.tick(chatters=[("1", "dyna"), ("2", "bob"), ("3", "cat")])
        assert r["chatters"] == 4 and r["credited"] == 4
        assert await W.get(db, await U.find_twitch_id(db, "1")) == 55   # 25*2 + 5
        assert await W.get(db, await U.find_twitch_id(db, "2")) == 30   # 25 + 5
        assert await W.get(db, await U.find_twitch_id(db, "3")) == 25   # lurker
        assert await W.get(db, await U.find_twitch_id(db, "4")) == 30   # chatted, left
        assert r["hats_total"] == 140
        # the chat window resets after the tick
        assert p._chatted == {}
    finally:
        await db.close()


async def test_event_bonus():
    db = await make_db()
    try:
        p = _plugin(db, WALLET_BONUS_SUB=50)
        assert await p.bonus("dyna", "sub", ref="sub:evt1", twitch_id="1") == 50
        assert await p.bonus("dyna", "sub", ref="sub:evt1", twitch_id="1") == 0   # redelivered
        assert await p.bonus("dyna", "sub", ref="sub:evt2", count=3) == 150        # gift x3
        assert await p.bonus("dyna", "raid") == 0                                    # configured 0
        assert await W.get(db, await U.find_twitch_id(db, "1")) == 200
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
