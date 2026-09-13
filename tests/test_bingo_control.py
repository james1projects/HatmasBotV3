"""
Tests for the Stream Bingo control surface (plugins/bingo + core/bingo_web
BingoControl): round history, pool editing, call undo, simulated detector
events, and the dashboard routes on a bare aiohttp app.

Self-running script per house convention: exit 0 only on full pass.
Hermetic: temp SQLite + temp pool file, no network, no bot.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.bingo import pool as P  # noqa: E402
from plugins.bingo import plugin as plugin_mod  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="bingo_ctl_test_"))


class _Eco:
    _connected = True

    def __init__(self, balances):
        self.balances = dict(balances)
        self.adjustments = []

    async def _get_balance(self, login):
        return self.balances.get(login)

    async def _adjust_balance(self, login, amount):
        self.adjustments.append((login, amount))
        self.balances[login] = self.balances.get(login, 0) + amount
        return True


class _Bot:
    def __init__(self, eco):
        self.plugins = {"economy": eco}
        self.features = {"bingo": True}
        self.chat = []
        self.commands = {}

    def is_feature_enabled(self, name):
        return self.features.get(name, False)

    def register_command(self, name, handler, mod_only=False, **kw):
        self.commands[name] = handler

    async def send_chat(self, text):
        self.chat.append(text)

    async def send_reply(self, message, text, whisper=False):
        self.chat.append("reply: " + text)


class _Overlay:
    def __init__(self):
        self.events = []
        self.listeners = []

    def add_event_listener(self, fn):
        self.listeners.append(fn)

    async def emit(self, name, data=None):
        self.events.append((name, data))


def _plugin(balances=None):
    d = _tmp()
    plugin_mod.config.BINGO_POOL_FILE = d / "pool.json"
    plugin_mod.config.BINGO_DB = d / "bingo.db"
    plugin_mod.config.BINGO_BASE_PRIZE = 500
    plugin_mod.config.BINGO_CARD_PRICES = (50, 100, 200)
    plugin_mod.config.BINGO_MAX_CARDS = 4
    plugin_mod.config.BINGO_POT_SHARE = 0.5
    ov = _Overlay()
    p = plugin_mod.BingoPlugin(overlay_manager=ov)
    eco = _Eco(balances or {})
    bot = _Bot(eco)
    p.setup(bot)
    asyncio.run(p.on_ready())
    return p, bot, eco, ov


def _card_square(p, login):
    """A non-free square id on the viewer's first card."""
    card = p.my_cards(login)["cards"][0]
    return next(s["id"] for s in card["squares"] if not s["free"])


# ── history + cards in play ───────────────────────────────────────────

def test_history_and_round_cards():
    p, bot, eco, ov = _plugin({"dyna": 1000})
    assert p.history() == [] and p.round_cards() == []
    r1 = asyncio.run(p.start_round())["round_id"]
    assert [h["round_id"] for h in p.history()] == [r1]          # the open round counts
    assert p.round_cards() == []
    asyncio.run(p.claim_card("dyna", "Dyna"))
    asyncio.run(p.claim_card("dyna", "Dyna"))                    # 50 Hats
    cards = p.round_cards()
    assert [c["seq"] for c in cards] == [1, 2] and cards[1]["price"] == 50
    assert cards[0]["marked"] == 1 and cards[0]["to_bingo"] == 4 and not cards[0]["bingo"]
    asyncio.run(p.end_round("manual"))
    r2 = asyncio.run(p.start_round())["round_id"]
    hist = p.history()
    assert [h["round_id"] for h in hist] == [r2, r1]              # newest first
    assert hist[1]["status"] == "closed" and hist[1]["cards"] == 2 and hist[1]["players"] == 1
    assert hist[1]["pot"] == 525 and hist[1]["winner"] is None
    assert p.history(1) == hist[:1]
    assert p.round_cards() == []                                  # new round, no cards yet
    st = p.status()
    for key in ("auto_ids", "simulated_events", "pool_file", "round_cards", "history"):
        assert key in st, key
    assert st["auto_ids"] == sorted(plugin_mod.AUTO_IDS) and st["history"][0]["round_id"] == r2


# ── pool editing ──────────────────────────────────────────────────────

def test_square_id_slug():
    assert plugin_mod.square_id('Says "no mana"') == "says_no_mana"
    assert plugin_mod.square_id("  Blames---the JUNGLER!  ") == "blames_the_jungler"
    assert plugin_mod.square_id("x" * 80) == "x" * 40 and plugin_mod.square_id("") == ""


def test_set_square_add_update_and_validation():
    p, *_ = _plugin()
    n = len(p.pool)
    res = p.set_square("", 'Says "one more game"', weight="2.5")
    assert res["ok"] and res["created"] and res["pool_size"] == n + 1
    assert res["square"] == {"id": "says_one_more_game", "label": 'Says "one more game"',
                             "source": "manual", "weight": 2.5}
    assert p.known_event("says_one_more_game") is not None
    on_disk = json.loads(Path(plugin_mod.config.BINGO_POOL_FILE).read_text(encoding="utf-8"))["squares"]
    assert any(sq["id"] == "says_one_more_game" for sq in on_disk)
    # update in place: same id, new label + weight, pool size unchanged, order kept
    idx = [sq["id"] for sq in p.pool].index("says_one_more_game")
    res = p.set_square("says_one_more_game", "One more game", weight=4)
    assert res["ok"] and not res["created"] and res["pool_size"] == n + 1
    assert p.pool[idx] == {"id": "says_one_more_game", "label": "One more game", "source": "manual", "weight": 4.0}
    # source auto only for ids the bot can fire
    assert p.set_square("custom_auto", "Never fires", source="auto")["square"]["source"] == "manual"
    assert p.set_square("first_blood", "First blood", source="auto")["square"]["source"] == "auto"
    assert p.set_square("first_blood", "First blood", source="manual")["square"]["source"] == "manual"
    # validation
    assert not p.set_square("", "")["ok"]
    assert not p.set_square("x", "")["ok"]
    assert not p.set_square("free", "Free")["ok"]
    assert not p.set_square("x", "X", weight=0)["ok"]
    assert not p.set_square("x", "X", weight="nope")["ok"]
    assert p.known_event("x") is None


def test_remove_square_rules_and_reload():
    p, *_ = _plugin()
    n = len(p.pool)
    asyncio.run(p.start_round())
    res = p.remove_square("kill")
    assert not res["ok"] and "close the round" in res["error"] and p.known_event("kill")
    asyncio.run(p.end_round("manual"))
    assert not p.remove_square("nonexistent")["ok"]
    res = p.remove_square("kill")
    assert res["ok"] and res["removed"] == "kill" and res["pool_size"] == n - 1 and p.known_event("kill") is None
    on_disk = json.loads(Path(plugin_mod.config.BINGO_POOL_FILE).read_text(encoding="utf-8"))["squares"]
    assert len(on_disk) == n - 1
    # never below 24 squares
    while len(p.pool) > P.CARD_SIZE - 1:
        assert p.remove_square(p.pool[-1]["id"])["ok"]
    res = p.remove_square(p.pool[-1]["id"])
    assert not res["ok"] and "at least" in res["error"] and len(p.pool) == P.CARD_SIZE - 1
    # hand edit + reload
    squares = P.default_pool() + [{"id": "extra_square", "label": "Extra", "source": "manual", "weight": 1}]
    Path(plugin_mod.config.BINGO_POOL_FILE).write_text(json.dumps({"squares": squares}), encoding="utf-8")
    res = p.reload_pool()
    assert res["ok"] and res["pool_size"] == len(squares) and p.known_event("extra_square")["label"] == "Extra"


# ── undo ──────────────────────────────────────────────────────────────

def test_uncall_rebuilds_marks_and_notifies():
    p, bot, eco, ov = _plugin()
    asyncio.run(p.start_round())
    assert not asyncio.run(p.uncall("kill"))["ok"]                       # not called yet
    asyncio.run(p.claim_card("dyna", "Dyna"))
    sid = _card_square(p, "dyna")
    other = next(s["id"] for s in p.my_cards("dyna")["cards"][0]["squares"] if not s["free"] and s["id"] != sid)
    asyncio.run(p.fire(sid, "deck"))
    asyncio.run(p.fire(sid, "deck"))                                      # duplicate call row
    asyncio.run(p.fire(other, "deck"))
    rid = p.current()["id"]
    assert p.my_cards("dyna")["cards"][0]["to_bingo"] <= 3
    seen = []

    async def listener(event, data):
        seen.append((event, data))
    p.add_listener(listener)
    res = asyncio.run(p.uncall(sid))
    assert res["ok"] and res["removed"] == 2 and res["changed"] == 1 and res["event_id"] == sid
    assert sid not in p.store.summary(rid)["called_ids"] and other in p.store.summary(rid)["called_ids"]
    card = p.my_cards("dyna")["cards"][0]
    marked = {s["id"] for s in card["squares"] if s["marked"]}
    assert sid not in marked and other in marked and "free" in marked
    assert seen and seen[-1][0] == "bingo_uncall" and seen[-1][1]["call"]["event_id"] == sid
    assert seen[-1][1]["changed_cards"] == [card["id"]]
    assert ov.events[-1][0] == "bingo_uncall"
    assert any("undone" in m for m in bot.chat)
    assert not asyncio.run(p.uncall(sid))["ok"]                          # gone now
    asyncio.run(p.end_round("manual"))
    assert not asyncio.run(p.uncall(other))["ok"]                        # no open round


# ── simulated detector / economy events ───────────────────────────────

def test_simulate_detector_events():
    p, *_ = _plugin()
    assert not asyncio.run(p.simulate("meteor"))["ok"]
    res = asyncio.run(p.simulate("kill"))                                # no round: harmless
    assert res["ok"] and res["summary"] is None and res["new_calls"] == 0
    asyncio.run(p.start_round())
    rid = p.current()["id"]
    called = lambda: set(p.store.called_ids(rid))  # noqa: E731
    res = asyncio.run(p.simulate("god", "Loki"))
    assert res["match"]["open"] and res["match"]["kills"] == 0 and "new_god" not in called()
    res = asyncio.run(p.simulate("kill"))
    assert res["new_calls"] == 2 and {"kill", "first_blood"} <= called()
    for _ in range(8):
        asyncio.run(p.simulate("kill"))
    assert "kills_10" not in called()
    res = asyncio.run(p.simulate("kill"))
    assert res["match"]["kills"] == 10 and "kills_10" in called() and res["new_calls"] == 1
    for k in ("double_kill", "penta_kill"):
        asyncio.run(p.simulate(k))
    assert {"double", "penta"} <= called() and "triple" not in called()
    asyncio.run(p.simulate("assist"))
    assert "assist" in called()
    res = asyncio.run(p.simulate("win"))                                 # no deaths -> deathless too
    assert {"match_win", "deathless"} <= called() and not res["match"]["open"] and res["match"]["kills"] == 0
    asyncio.run(p.simulate("god", "Ymir"))                               # second god this round
    assert "new_god" in called()
    for _ in range(5):
        asyncio.run(p.simulate("death"))
    assert {"death", "deaths_5"} <= called()
    asyncio.run(p.simulate("loss"))
    assert "match_loss" in called()
    assert sorted(res["match"]["gods_this_round"]) == ["Loki"]


# ── dashboard routes ──────────────────────────────────────────────────

def test_control_routes():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from core.bingo_web import BingoControl

    p, *_ = _plugin({"dyna": 1000})
    holder = {"plugin": None}

    async def run():
        app = web.Application()
        BingoControl(lambda: holder["plugin"]).register(app.router)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/bingo/status")
            assert r.status == 404 and (await r.json())["error"] == "bingo plugin not loaded"
            holder["plugin"] = p
            assert (await (await c.get("/api/bingo/status")).json())["open"] is False
            r = await c.post("/api/bingo/start")
            rid = (await r.json())["round_id"]
            await p.claim_card("dyna", "Dyna")
            sid = _card_square(p, "dyna")
            r = await c.post("/api/bingo/fire")
            assert r.status == 400 and "event is required" in (await r.json())["error"]
            r = await c.get(f"/api/bingo/fire?event={sid}")                      # deck bat: GET + query
            j = await r.json()
            assert r.status == 200 and j["ok"] and j["changed"] == 1 and j["source"] == "deck"
            r = await c.post("/api/bingo/fire", json={"event": sid, "source": "dashboard"})
            assert (await r.json())["already"] is True
            r = await c.post("/api/bingo/uncall?event=" + sid)
            assert (await r.json())["removed"] == 2
            r = await c.post("/api/bingo/simulate?event=kill")
            assert (await r.json())["new_calls"] == 2
            r = await c.post("/api/bingo/simulate?event=nope")
            assert r.status == 400
            r = await c.post("/api/bingo/pool/save", json={"label": "Custom thing", "weight": 2})
            j = await r.json()
            assert j["ok"] and j["square"]["id"] == "custom_thing" and j["created"]
            r = await c.get("/api/bingo/pool")
            j = await r.json()
            assert any(sq["id"] == "custom_thing" for sq in j["squares"]) and "kill" in j["auto_ids"]
            r = await c.post("/api/bingo/pool/delete", json={"id": "custom_thing"})
            assert r.status == 400 and "close the round" in (await r.json())["error"]
            st = await (await c.get("/api/bingo/status")).json()
            assert st["round"]["round_id"] == rid and len(st["round_cards"]) == 1 and "kill" in st["round"]["called_ids"]
            r = await c.post("/api/bingo/end")
            assert (await r.json())["status"] == "closed"
            r = await c.post("/api/bingo/pool/delete", json={"id": "custom_thing"})
            assert (await r.json())["ok"]
            r = await c.post("/api/bingo/pool/reload")
            assert (await r.json())["ok"]
            j = await (await c.get("/api/bingo/history?limit=5")).json()
            assert j["rounds"][0]["round_id"] == rid and j["rounds"][0]["status"] == "closed"
            r = await c.post("/api/bingo/end")
            assert r.status == 400 and "no open round" in (await r.json())["error"]
    asyncio.run(run())


# ── harness ───────────────────────────────────────────────────────────

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if not TESTS:
        print("FAIL  no tests were collected")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
