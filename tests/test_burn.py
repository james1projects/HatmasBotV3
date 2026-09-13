"""
Tests for plugins/burn.py (!burn: destroy Hats for the flex).

Self-running script per house convention: exit 0 only on full pass.
Hermetic: in-memory wallet DB, fake bot with a recording overlay, no
network.

    python tests/test_burn.py
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
from core import db as _shared_db  # noqa: E402
from core import alert_box as AB  # noqa: E402
from plugins import burn as B  # noqa: E402


class FakeOverlay:
    def __init__(self):
        self.events = []

    async def emit(self, name, data=None):
        self.events.append((name, data))


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.replies, self.chat, self.commands = [], [], {}
        self.features = {"burn": True}
        self.web_server = types.SimpleNamespace(overlay=FakeOverlay())
        self.live_listeners = []
        self.plugins = {"stream_status": types.SimpleNamespace(
            add_live_listener=lambda cb: self.live_listeners.append(cb))}

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def is_feature_enabled(self, f):
        return self.features.get(f, False)

    async def send_reply(self, message, text, whisper=False):
        self.replies.append(text)

    async def send_chat(self, text):
        self.chat.append(text)

    async def user_uuid_for(self, chatter):
        return await U.get_or_create_twitch(self.db, str(chatter.id), chatter.name.lower(), chatter.display_name)


def msg(name, tid):
    return types.SimpleNamespace(chatter=types.SimpleNamespace(name=name.lower(), display_name=name, id=tid))


async def make():
    db = await aiosqlite.connect(":memory:")
    await U.ensure_schema(db)
    await W.ensure_schema(db)
    U.clear_cache(db)

    async def _get_db():
        return db
    _shared_db.get_db = _get_db
    B._cfg.BURN_MIN_HATS = 500
    p = B.BurnPlugin()
    bot = FakeBot(db)
    p.setup(bot)
    await p.on_ready()
    assert bot.live_listeners, "burn must hook the live transition"
    return p, bot, db


async def test_burn_debits_records_and_announces():
    p, bot, db = await make()
    try:
        rich = await U.get_or_create_twitch(db, "1", "rich", "Rich")
        await W.credit(db, rich, "hats", 5000, "watch")
        await p.cmd_burn(msg("Rich", "1"), "1,000")
        assert await W.get(db, rich) == 4000
        assert bot.chat == ["Rich just burned 1,000 Hats! NEW ALL-TIME RECORD!"]
        assert bot.replies[-1] == "1,000 Hats gone. 4,000 left."
        name, data = bot.web_server.overlay.events[-1]
        assert name == "hats_burned"
        assert data["amount"] == 1000 and data["is_alltime_record"] and data["is_stream_record"]
        assert data["stream_record"] == {"display": "Rich", "amount": 1000}
        assert data["alltime_record"] == {"display": "Rich", "amount": 1000}
        hist = await W.history(db, rich, 1)
        assert hist[0]["reason"] == "burn" and hist[0]["delta"] == -1000
        # a smaller burn: no record tags, the records still show
        bob = await U.get_or_create_twitch(db, "2", "bob", "Bob")
        await W.credit(db, bob, "hats", 600, "watch")
        await p.cmd_burn(msg("Bob", "2"), "500")
        assert bot.chat[-1] == "Bob just burned 500 Hats!"
        data = bot.web_server.overlay.events[-1][1]
        assert not data["is_stream_record"] and not data["is_alltime_record"]
        assert data["stream_record"]["display"] == "Rich" and data["stream_total"] == 1500
        await p.cmd_burns(msg("Bob", "2"), "")
        assert bot.replies[-1] == "Tonight: 1. Rich 1,000 | 2. Bob 500 || All-time: Rich 1,000"
        # the alert box knows the kind and its sample renders a summary
        assert "burn" in AB.KINDS and AB.KINDS["burn"]["events"] == ["hats_burned"]
        assert AB._summary({"kind": "burn", "data": data}) == "Bob burned 500 Hats"
    finally:
        await db.close()


async def test_burn_guards():
    p, bot, db = await make()
    try:
        poor = await U.get_or_create_twitch(db, "3", "poor", "Poor")
        await W.credit(db, poor, "hats", 700, "watch")
        for bad in ("", "abc", "0", "-5", "499"):
            await p.cmd_burn(msg("Poor", "3"), bad)
        assert await W.get(db, poor) == 700 and bot.chat == []
        assert bot.replies[-1] == "Minimum burn is 500 Hats."
        assert bot.replies[0].startswith("Usage: !burn <amount>")
        await p.cmd_burn(msg("Poor", "3"), "701")
        assert bot.replies[-1] == "You have 700 Hats; you can't burn 701." and await W.get(db, poor) == 700
        assert bot.web_server.overlay.events == []
        bot.features["burn"] = False
        await p.cmd_burn(msg("Poor", "3"), "500")
        assert bot.replies[-1] == "Burning is closed right now." and await W.get(db, poor) == 700
        bot.features["burn"] = True
        await p.cmd_burn(msg("Poor", "3"), "700")
        assert await W.get(db, poor) == 0 and len(bot.chat) == 1
    finally:
        await db.close()


async def test_stream_resets_but_alltime_survives():
    p, bot, db = await make()
    try:
        rich = await U.get_or_create_twitch(db, "1", "rich", "Rich")
        await W.credit(db, rich, "hats", 10000, "watch")
        await p.cmd_burn(msg("Rich", "1"), "3000")
        await bot.live_listeners[0]({"is_live": True})          # next stream
        assert p.session["burns"] == [] and p.session["total"] == 0
        await p.cmd_burns(msg("Rich", "1"), "")
        assert bot.replies[-1] == "Nobody has burned any Hats tonight. || All-time: Rich 3,000"
        # a fresh plugin (restart) still knows the all-time record from the ledger
        q = B.BurnPlugin(); q.setup(bot); await q.on_ready()
        assert (await q.alltime_record())["amount"] == 3000
        await q.cmd_burn(msg("Rich", "1"), "1000")
        assert bot.chat[-1] == "Rich just burned 1,000 Hats! Biggest burn of the stream!"
        await q.cmd_burn(msg("Rich", "1"), "3001")
        assert bot.chat[-1] == "Rich just burned 3,001 Hats! NEW ALL-TIME RECORD!"
    finally:
        await db.close()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


async def main() -> int:
    passed = failed = 0
    for t in TESTS:
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
