"""
Tests for !vipsr (plugins/songrequest.py): Hats to cut the song queue.

Self-running script per house convention: exit 0 only on full pass.
Hermetic: in-memory wallet DB, fake bot, song resolver stubbed, temp
data files, no Spotify / YouTube / network.

    python tests/test_priority_sr.py
"""

import asyncio
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aiosqlite  # noqa: E402

from core import users as U  # noqa: E402
from core import wallet as W  # noqa: E402
from core import db as _shared_db  # noqa: E402
from plugins import songrequest as SR  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="hatmas_vipsr_"))
for name in ("SR_QUEUE_FILE", "SR_HISTORY_FILE", "SR_LIKES_FILE", "SR_BLACKLIST_FILE", "SR_STATE_FILE"):
    setattr(SR, name, TMP / f"{name.lower()}.json")


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.replies = []
        self.chat = []
        self.web_server = None
        self.features = {"song_requests": True, "priority_sr": True}
        self.commands = {}

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def is_feature_enabled(self, f):
        return self.features.get(f, False)

    async def send_reply(self, message, text, whisper=False):
        self.replies.append(text)

    async def send_chat(self, text):
        self.chat.append(text)

    async def user_uuid_for(self, chatter):
        return await U.get_or_create_twitch(self.db, str(chatter.id), chatter.name.lower(), chatter.name)


def msg(name, tid):
    return types.SimpleNamespace(chatter=types.SimpleNamespace(name=name, id=tid, subscriber=False))


async def make():
    db = await aiosqlite.connect(":memory:")
    await U.ensure_schema(db)
    await W.ensure_schema(db)
    U.clear_cache(db)

    async def _get_db():
        return db
    _shared_db.get_db = _get_db

    p = SR.SongRequestPlugin()
    p.queue = []
    p._current_remaining_ms = 0
    bot = FakeBot(db)
    p.setup(bot)

    async def resolve(query):
        return {"title": query.title(), "artist": "Artist", "source": "spotify",
                "uri": f"spotify:track:{query}", "duration_ms": 180000, "album_art": None}
    p._search_spotify = resolve
    p._nsfw_checker = None
    SR._cfg.SR_PRIORITY_COST = 200
    SR._cfg.SR_PRIORITY_MAX_PER_HOUR = 2
    return p, bot, db


def titles(p):
    return [(s["title"], bool(s.get("priority"))) for s in p.queue]


async def test_vipsr_cuts_line_and_charges():
    p, bot, db = await make()
    try:
        await p.cmd_sr(msg("Alice", "1"), "one")
        await p.cmd_sr(msg("Bob", "2"), "two")
        rich = await U.get_or_create_twitch(db, "3", "rich", "Rich")
        await W.credit(db, rich, "hats", 500, "watch")
        await p.cmd_vipsr(msg("Rich", "3"), "vip song")
        assert titles(p) == [("Vip Song", True), ("One", False), ("Two", False)], titles(p)
        assert await W.get(db, rich) == 300
        assert bot.replies[-1].startswith("VIP: Vip Song by Artist [Spotify] | Position: #1 | up next | -200 Hats (300 left)"), bot.replies[-1]
        assert bot.chat == ["Rich spent 200 Hats to cut the line with Vip Song by Artist!"]
        hist = await W.history(db, rich, 1)
        assert hist[0]["reason"] == "priority_sr" and hist[0]["delta"] == -200
        # a second VIP goes behind the first VIP, still ahead of normal songs
        await p.cmd_vipsr(msg("Rich", "3"), "second vip")
        assert titles(p) == [("Vip Song", True), ("Second Vip", True), ("One", False), ("Two", False)]
        assert "Position: #2" in bot.replies[-1]
        # queue payload + songstatus carry the flag
        assert [q["priority"] for q in p._queue_payload()] == [True, True, False, False]
        await p.cmd_songstatus(msg("Rich", "3"), "")
        assert bot.replies[-1] == "Your songs: #1 VIP Vip Song (up next) | #2 VIP Second Vip (~3 min)"
    finally:
        await db.close()


async def test_vipsr_never_jumps_a_pushed_song():
    p, bot, db = await make()
    try:
        await p.cmd_sr(msg("Alice", "1"), "pushed")
        await p.cmd_sr(msg("Bob", "2"), "waiting")
        p.queue[0]["pushed_to_spotify"] = True     # Spotify already has it
        rich = await U.get_or_create_twitch(db, "3", "rich", "Rich")
        await W.credit(db, rich, "hats", 200, "watch")
        await p.cmd_vipsr(msg("Rich", "3"), "vip")
        assert titles(p) == [("Pushed", False), ("Vip", True), ("Waiting", False)], titles(p)
        assert "Position: #2" in bot.replies[-1]
    finally:
        await db.close()


async def test_vipsr_short_balance_and_cap():
    p, bot, db = await make()
    try:
        poor = await U.get_or_create_twitch(db, "4", "poor", "Poor")
        await W.credit(db, poor, "hats", 150, "watch")
        await p.cmd_vipsr(msg("Poor", "4"), "nope")
        assert p.queue == [] and await W.get(db, poor) == 150
        assert bot.replies[-1] == "A VIP request costs 200 Hats; you have 150."
        assert bot.chat == []
        # cap: two per rolling hour, the third waits; a normal !sr still works
        rich = await U.get_or_create_twitch(db, "3", "rich", "Rich")
        await W.credit(db, rich, "hats", 10000, "watch")
        await p.cmd_vipsr(msg("Rich", "3"), "a")
        await p.cmd_vipsr(msg("Rich", "3"), "b")
        # the normal per-user song cap (2) is now full for rich: bump it
        SR.SR_MAX_PER_USER = 5
        await p.cmd_vipsr(msg("Rich", "3"), "c")
        assert "used your 2 VIP requests this hour" in bot.replies[-1], bot.replies[-1]
        assert await W.get(db, rich) == 9600 and len(p.queue) == 2
        await p.cmd_sr(msg("Rich", "3"), "c")
        assert titles(p)[-1] == ("C", False)
        # an hour later the cap resets
        p._priority_times[rich] = [t - 3601 for t in p._priority_times[rich]]
        await p.cmd_vipsr(msg("Rich", "3"), "d")
        assert titles(p)[2] == ("D", True) and await W.get(db, rich) == 9400
        # feature toggle off: refused before any charge
        bot.features["priority_sr"] = False
        await p.cmd_vipsr(msg("Rich", "3"), "e")
        assert bot.replies[-1] == "VIP song requests are currently closed."
        assert await W.get(db, rich) == 9400
        # validation failures never charge either (duplicate title)
        bot.features["priority_sr"] = True
        await p.cmd_vipsr(msg("Rich", "3"), "d")
        assert "already in the queue" in bot.replies[-1] and await W.get(db, rich) == 9400
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
