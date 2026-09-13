"""
Tests for plugins/timed_messages.py (rotating chat messages managed
from /mod).

Self-running script per house convention: exit 0 only on full pass.
Hermetic: fake bot, fake clock, temp store file, no network.

    python tests/test_timed_messages.py            # all
    python tests/test_timed_messages.py gap        # name filter
"""

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins import timed_messages as TM  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="hatmas_timed_"))


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


class FakeBot:
    def __init__(self, live=True, features=None):
        self.sent = []
        self.features = {"timed_messages": True} if features is None else features
        status = types.SimpleNamespace(get_status=lambda: {"is_live": live})
        self.plugins = {"stream_status": status}
        self._raw_handlers = []
        self.bot_username = "hatmasbot"

    async def send_chat(self, text):
        self.sent.append(text)

    def is_feature_enabled(self, name):
        return self.features.get(name, False)

    def register_raw_handler(self, h):
        self._raw_handlers.append(h)


def _plugin(name, live=True, gap=60, features=None):
    store = TMP / f"{name}.json"
    if store.exists():
        store.unlink()
    clock = FakeClock()
    p = TM.TimedMessagesPlugin(clock=clock, store_file=store)
    p.setup(FakeBot(live=live, features=features))
    ok, r = p.set_settings(global_gap_sec=gap)
    assert ok, r
    return p, clock


async def _run(p, clock, seconds, live=None):
    """Advance one second at a time (like the real loop), collect posts."""
    out = []
    for _ in range(int(seconds)):
        clock.tick(1)
        out += [m["text"] for m in await p.step(live=live)]
    return out


# ── rotation order within a lane ───────────────────────────────────────

async def test_lane_rotation_order():
    p, clock = _plugin("order", gap=0)
    ok, r = p.set_lane("general", interval_min=1)
    assert ok, r
    for t in ("A", "B", "C"):
        ok, r = p.add_or_update_message(t, "general", created_by="mod")
        assert ok, r
    assert await _run(p, clock, 59) == []          # first post after a full interval
    assert await _run(p, clock, 1) == ["A"]
    assert await _run(p, clock, 60) == ["B"]
    assert await _run(p, clock, 60) == ["C"]
    assert await _run(p, clock, 60) == ["A"]       # wraps
    # a disabled message is skipped without breaking the cycle
    b = next(m for m in p.store["messages"] if m["text"] == "B")
    p.add_or_update_message("B", "general", enabled=False, msg_id=b["id"])
    assert await _run(p, clock, 60) == ["C"]
    assert await _run(p, clock, 60) == ["A"]
    # reorder = new rotation order
    ids = [m["id"] for m in p.store["messages"]]
    ok, r = p.reorder(list(reversed(ids)))
    assert ok, r
    assert [m["text"] for m in p.store["messages"]] == ["C", "B", "A"]
    ok, r = p.reorder(ids[:1])
    assert not ok
    assert p.bot.sent == ["A", "B", "C", "A", "C", "A"]


# ── two lanes, independent intervals, global gap ───────────────────────

async def test_global_gap_between_lanes():
    p, clock = _plugin("gap", gap=30)
    p.set_lane("general", interval_min=1)
    p.set_lane("socials", interval_min=1)
    p.add_or_update_message("g1", "general")
    p.add_or_update_message("s1", "socials")
    # both lanes come due at the same second; only one may post, the
    # other waits for the 30 s gap
    posts = []
    for _ in range(60):
        clock.tick(1)
        posts += [(int(clock.t), m["text"]) for m in await p.step()]
    assert len(posts) == 1, posts
    t0 = posts[0][0]
    posts = []
    for _ in range(31):
        clock.tick(1)
        posts += [(int(clock.t), m["text"]) for m in await p.step()]
    assert len(posts) == 1 and posts[0][0] - t0 == 30, posts
    assert set(p.bot.sent) == {"g1", "s1"}
    # the waiting lane's timer restarts from when it actually posted
    p.bot.sent.clear()
    await _run(p, clock, 29)
    assert p.bot.sent == ["g1"]                    # general due again at +60
    await _run(p, clock, 30)
    assert p.bot.sent == ["g1", "s1"]              # socials 60 s after its post
    # gap 0: both may post in the same pass
    p.set_settings(global_gap_sec=0)
    p.bot.sent.clear()
    p2, c2 = _plugin("gap0", gap=0)
    p2.set_lane("a", interval_min=1)
    p2.set_lane("b", interval_min=1)
    p2.add_or_update_message("a1", "a")
    p2.add_or_update_message("b1", "b")
    assert sorted(await _run(p2, c2, 60)) == ["a1", "b1"]


# ── live only ──────────────────────────────────────────────────────────

async def test_live_only():
    p, clock = _plugin("live", live=False, gap=0)
    p.set_lane("general", interval_min=1)
    p.add_or_update_message("hello", "general")
    assert await _run(p, clock, 180) == []         # offline: nothing, ever
    # going live never dumps a backlog: the lane was re-armed each time it
    # came due, so the first post is a full interval after going live
    assert await _run(p, clock, 59, live=True) == []
    assert await _run(p, clock, 1, live=True) == ["hello"]
    # live_only off: posts while offline
    p.set_settings(live_only=False)
    assert await _run(p, clock, 60, live=False) == ["hello"]
    # the feature toggle silences everything without losing state
    p.bot.features["timed_messages"] = False
    assert await _run(p, clock, 120, live=True) == []
    p.bot.features["timed_messages"] = True
    assert await _run(p, clock, 60, live=True) == ["hello"]


# ── enable / disable, empty lanes, min chat messages ───────────────────

async def test_enable_disable_and_min_chat():
    p, clock = _plugin("toggle", gap=0)
    p.set_lane("general", interval_min=1)
    ok, m = p.add_or_update_message("x", "general")
    assert await _run(p, clock, 60) == ["x"]
    p.add_or_update_message("x", "general", enabled=False, msg_id=m["id"])
    assert await _run(p, clock, 180) == []         # nothing enabled: silent
    p.add_or_update_message("x", "general", enabled=True, msg_id=m["id"])
    assert await _run(p, clock, 60) == ["x"]       # the lane kept ticking
    ok, r = p.delete_message(m["id"])
    assert ok and p.store["messages"] == []
    assert await _run(p, clock, 120) == []
    # min_chat_messages: the lane waits for chat, then posts at once
    p.set_lane("general", interval_min=1, min_chat_messages=2)
    p.add_or_update_message("y", "general")
    assert await _run(p, clock, 90) == []          # due at 60, no chat
    for who in ("viewer", "hatmasbot", "viewer"):  # the bot's own lines don't count
        await p._on_chat(types.SimpleNamespace(chatter=types.SimpleNamespace(name=who)))
    assert await _run(p, clock, 1) == ["y"]
    assert p._chat_since["general"] == 0           # counter resets on post
    assert await _run(p, clock, 120) == []         # needs chat again


# ── validation + persistence ───────────────────────────────────────────

async def test_validation_and_persistence():
    p, clock = _plugin("persist", gap=45)
    assert not p.set_lane("Bad Lane!")[0]
    assert not p.set_lane("fast", interval_min=0)[0]
    assert not p.set_lane("fast", interval_min=True)[0]
    assert not p.set_settings(global_gap_sec=99999)[0]
    assert not p.add_or_update_message("", "general")[0]
    assert not p.add_or_update_message("t" * 451, "general")[0]
    assert not p.add_or_update_message("t", "nope")[0]
    assert not p.add_or_update_message("t", "general", msg_id="missing")[0]
    assert not p.delete_lane("general")[0]         # the only lane stays
    p.set_lane("socials", interval_min=15, min_chat_messages=3)
    ok, m = p.add_or_update_message("follow me", "socials", created_by="dyna")
    assert ok and m["created_by"] == "dyna" and m["enabled"] is True
    assert not p.delete_lane("socials")[0]         # has a message
    assert p.set_settings(live_only=False)[0]
    # reload from disk = same store, and the file is valid JSON
    raw = json.loads(p._store_file.read_text(encoding="utf-8"))
    assert raw["settings"] == {"live_only": False, "global_gap_sec": 45}
    assert raw["lanes"]["socials"] == {"interval_min": 15, "min_chat_messages": 3}
    q = TM.TimedMessagesPlugin(clock=clock, store_file=p._store_file)
    assert q.store == p.store
    # junk on disk is normalized, never crashes
    p._store_file.write_text(json.dumps({"settings": {"global_gap_sec": "x"},
                                         "lanes": {"ok": {"interval_min": 5}, "BAD!": {}},
                                         "messages": [{"id": "1", "text": "a", "lane": "ok"},
                                                      {"id": "2", "text": "b", "lane": "gone"},
                                                      "junk"]}), encoding="utf-8")
    q = TM.TimedMessagesPlugin(clock=clock, store_file=p._store_file)
    assert list(q.store["lanes"]) == ["ok"]
    assert [m["id"] for m in q.store["messages"]] == ["1"]
    assert q.store["settings"]["global_gap_sec"] == 60
    # snapshot exposes the runtime the page shows
    snap = q.snapshot()
    assert snap["lanes"]["ok"]["enabled_count"] == 1 and snap["lanes"]["ok"]["next_id"] == "1"
    assert snap["lanes"]["ok"]["due_in_sec"] == 300 and snap["status"]["live"] is False
    # post_now says it immediately and starts the gap
    q.setup(FakeBot(live=False))
    ok, r = await q.post_now("1")
    assert ok and q.bot.sent == ["a"]
    assert q.snapshot()["status"]["gap_remaining_sec"] == 60
    assert not (await q.post_now("zz"))[0]


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
