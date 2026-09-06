"""
Tests for plugins/bingo (Stream Bingo): pool + card math, the store, and
the plugin's round / call / claim / payout logic with a fake economy.

Self-running script per house convention: exit 0 only on full pass.
Hermetic: temp SQLite + temp pool file, no network, no bot.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.bingo import pool as P  # noqa: E402
from plugins.bingo.store import BingoStore  # noqa: E402
from plugins.bingo import plugin as plugin_mod  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="bingo_test_"))


# ── pool + cards ──────────────────────────────────────────────────────

def test_default_pool_and_file_roundtrip():
    pool = P.default_pool()
    assert len(pool) >= 30 and all(sq["weight"] > 0 for sq in pool)
    assert {sq["source"] for sq in pool} == {"auto", "manual"}
    d = _tmp()
    path = d / "pool.json"
    loaded = P.load_pool(path)                       # creates the file
    assert path.exists() and loaded == pool
    path.write_text('{"squares": [{"id": "a", "label": "A"}]}', encoding="utf-8")
    assert P.load_pool(path) == pool                 # too small -> defaults
    path.write_text("not json", encoding="utf-8")
    assert P.load_pool(path) == pool
    bad = P.validate_pool([{"id": "x", "label": "X", "weight": 0}, {"id": "free", "label": "F"},
                           {"id": "y", "label": "Y", "source": "weird", "weight": "2"},
                           {"id": "y", "label": "dup"}, {"label": "no id"}])
    assert bad == [{"id": "y", "label": "Y", "source": "manual", "weight": 2.0}]


def test_cards_are_deterministic_unique_and_shaped():
    pool = P.default_pool()
    a = P.make_card(pool, P.card_seed(1, "Dyna", 1))
    b = P.make_card(pool, P.card_seed(1, "dyna", 1))
    c = P.make_card(pool, P.card_seed(1, "dyna", 2))
    assert a == b and a != c
    assert len(a) == 25 and a[12] == P.FREE and len(set(a)) == 25
    assert P.FREE not in a[:12] + a[13:]
    try:
        P.make_card(pool[:10], 1)
        assert False, "small pool accepted"
    except ValueError:
        pass
    # weights matter: a heavy square shows up on far more cards than a rare one
    cards = [P.make_card(pool, P.card_seed(7, f"u{i}", 1)) for i in range(300)]
    assert sum("kill" in cd for cd in cards) > sum("penta" in cd for cd in cards) * 3


def test_bingo_math():
    card = P.make_card(P.default_pool(), 42)
    marks = P.marked_indexes(card, [])
    assert marks == {12} and not P.has_bingo(marks) and P.squares_to_bingo(marks) == 4
    row0 = P.marked_indexes(card, card[0:5])
    assert P.has_bingo(row0) and P.winning_lines(row0)[0] == (0, 1, 2, 3, 4)
    col = P.marked_indexes(card, [card[2], card[7], card[17], card[22]])       # + free at 12
    assert P.has_bingo(col) and (2, 7, 12, 17, 22) in P.winning_lines(col)
    diag = P.marked_indexes(card, [card[0], card[6], card[18]])
    assert not P.has_bingo(diag) and P.squares_to_bingo(diag) == 1
    assert P.label_of(P.default_pool(), "penta") == "PENTA KILL" and P.label_of([], P.FREE) == "FREE"
    assert P.next_card_price(1, [50, 100]) == 0 and P.next_card_price(2, [50, 100]) == 50
    assert P.next_card_price(3, [50, 100]) == 100 and P.next_card_price(4, [50, 100]) is None


# ── store ─────────────────────────────────────────────────────────────

def test_store_rounds_cards_calls_and_winner():
    s = BingoStore(_tmp() / "b.db")
    pool = P.default_pool()
    assert s.current_round() is None
    r = s.open_round(500, 0.5, now=1000.0)
    assert r["status"] == "open" and s.current_round()["id"] == r["id"]
    c1 = s.add_card(r["id"], "Dyna", "Dyna", 1, 0, P.make_card(pool, 1), [], now=1001.0)
    c2 = s.add_card(r["id"], "bob", "Bob", 1, 0, P.make_card(pool, 2), [], now=1002.0)
    s.add_card(r["id"], "bob", "Bob", 2, 50, P.make_card(pool, 3), [], now=1003.0)
    assert s.card_count(r["id"]) == (3, 2) and s.pot(r["id"]) == 525
    assert [c["seq"] for c in s.cards_for(r["id"], "BOB")] == [1, 2]
    # first call marks whichever cards hold that square; a repeat is a no-op
    first = c1["squares"][0]
    res = s.mark_event(r["id"], first, "L", "manual", now=1010.0)
    assert not res["already"] and any(c["id"] == c1["id"] for c in res["changed"])
    again = s.mark_event(r["id"], first, "L", "manual", now=1011.0)
    assert again["already"] and again["changed"] == []
    assert s.called_ids(r["id"]) == [first] and len(s.calls(r["id"])) == 2
    # a late card starts with the already-called squares marked
    c4 = s.add_card(r["id"], "cat", "Cat", 1, 0, [first] + P.make_card(pool, 4)[1:], s.called_ids(r["id"]))
    assert 0 in c4["marks"] and 12 in c4["marks"]
    # complete c1's top row -> winner, summary reflects it, round closes
    winners = []
    for sid in c1["squares"][1:5]:
        winners += s.mark_event(r["id"], sid, "L", "auto")["winners"]
    assert [w["id"] for w in winners] == [c1["id"]] and s.all_cards(r["id"])[0]["bingo_at"] is not None
    s.close_round(r["id"], winner={"login": "dyna", "display": "Dyna", "card_id": c1["id"]}, prize_paid=525, prize_ok=True)
    summ = s.summary(r["id"])
    assert summ["status"] == "closed" and summ["winner"] == {"login": "dyna", "display": "Dyna", "prize": 525}
    assert summ["cards"] == 4 and summ["players"] == 3 and summ["leaders"][0]["login"] == "dyna"
    assert s.current_round() is None and s.last_round()["id"] == r["id"]
    # opening a new round closes a stale open one
    s.open_round(100, 0.5); r3 = s.open_round(100, 0.5)
    assert s.current_round()["id"] == r3["id"]
    s.close()


# ── plugin with fakes ─────────────────────────────────────────────────

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


def test_claim_cards_prices_and_limits():
    p, bot, eco, ov = _plugin({"dyna": 120})
    assert asyncio.run(p.claim_card("dyna", "Dyna"))["error"].startswith("No bingo round")
    asyncio.run(p.start_round())
    assert ov.events[-1][0] == "bingo_open" and "BINGO is open" in bot.chat[-1]
    r1 = asyncio.run(p.claim_card("dyna", "Dyna"))
    assert r1["ok"] and r1["price"] == 0 and r1["next_price"] == 50 and len(r1["card"]["squares"]) == 25
    r2 = asyncio.run(p.claim_card("dyna", "Dyna"))
    assert r2["ok"] and r2["price"] == 50 and eco.balances["dyna"] == 70 and eco.adjustments[-1] == ("dyna", -50)
    r3 = asyncio.run(p.claim_card("dyna", "Dyna"))
    assert not r3["ok"] and "100 Hats" in r3["error"] and eco.balances["dyna"] == 70
    mine = p.my_cards("DYNA")
    assert len(mine["cards"]) == 2 and mine["next_price"] == 100 and mine["can_claim"]
    assert p.public_state()["round"]["pot"] == 525 and p.public_state()["round"]["sales"] == 50
    # no Hats service -> free card still works, paid card refused
    eco._connected = False
    assert asyncio.run(p.claim_card("bob", "Bob"))["ok"]
    assert "unavailable" in asyncio.run(p.claim_card("bob", "Bob"))["error"]
    # max cards
    eco._connected = True
    eco.balances["cat"] = 10_000
    for _ in range(4):
        asyncio.run(p.claim_card("cat", "Cat"))
    assert "maximum" in asyncio.run(p.claim_card("cat", "Cat"))["error"]
    assert p.my_cards("cat")["can_claim"] is False


def test_fire_marks_pays_and_closes():
    p, bot, eco, ov = _plugin({"dyna": 0})
    assert asyncio.run(p.fire("kill"))["error"] == "no open round"
    assert "unknown" in asyncio.run(p.fire("nope"))["error"]
    asyncio.run(p.start_round())
    card = asyncio.run(p.claim_card("dyna", "Dyna"))["card"]
    ids = [s["id"] for s in card["squares"]]
    res = asyncio.run(p.fire(ids[0], source="manual"))
    assert res["ok"] and res["changed"] == 1 and not res["winners"]
    assert ov.events[-1][0] == "bingo_call" and ov.events[-1][1]["call"]["event_id"] == ids[0]
    assert any("Bingo call" in t for t in bot.chat)
    assert asyncio.run(p.fire(ids[0]))["already"] is True
    for sid in ids[1:4]:
        asyncio.run(p.fire(sid))
    assert p.my_cards("dyna")["cards"][0]["to_bingo"] == 1
    win = asyncio.run(p.fire(ids[4]))
    assert win["winners"][0]["login"] == "dyna" and win["summary"]["status"] == "closed"
    assert eco.adjustments[-1] == ("dyna", 500)                     # base prize, no sales
    assert ov.events[-1][0] == "bingo_win" and ov.events[-1][1]["winner"]["paid"] is True
    assert any("BINGO! Dyna wins" in t for t in bot.chat)
    assert p.current() is None and p.public_state()["last"]["winner"]["login"] == "dyna"
    # after the round: firing is refused, ending is a no-op
    assert asyncio.run(p.fire("kill"))["error"] == "no open round"
    assert asyncio.run(p.end_round()) is None


def test_auto_squares_from_detector_and_economy():
    p, bot, eco, ov = _plugin()
    asyncio.run(p.start_round())
    # build a card that holds every auto square we will trigger
    r = p.current()
    wanted = ["kill", "first_blood", "double", "death", "deaths_5", "match_win", "new_god", "deathless", "kills_10"]
    # place them so no row / column / diagonal can complete (a bingo would
    # close the round mid-test)
    slots = [0, 1, 2, 3, 5, 6, 7, 10, 11]
    filler = [sq["id"] for sq in p.pool if sq["id"] not in wanted]
    squares = []
    for i in range(25):
        if i == 12:
            squares.append(P.FREE)
        elif i in slots:
            squares.append(wanted[slots.index(i)])
        else:
            squares.append(filler.pop(0))
    p.store.add_card(r["id"], "dyna", "Dyna", 1, 0, squares, [])
    asyncio.run(p._on_overlay_event("economy_god_detected", {"god": "Ymir"}))
    asyncio.run(p._on_kill("player_kill", 1))
    asyncio.run(p._on_multikill("double_kill"))
    for _ in range(5):
        asyncio.run(p._on_death(1))
    called = set(p.store.called_ids(r["id"]))
    assert {"kill", "first_blood", "double", "death", "deaths_5"} <= called and "kills_10" not in called
    asyncio.run(p._on_overlay_event("match_end_economy", {"outcome": "win"}))
    assert "match_win" in set(p.store.called_ids(r["id"])) and "deathless" not in set(p.store.called_ids(r["id"]))
    # second match: a different god, no deaths -> new_god and deathless
    asyncio.run(p._on_overlay_event("economy_god_detected", {"god": "Loki"}))
    for _ in range(10):
        asyncio.run(p._on_kill("player_kill", 1))
    asyncio.run(p._on_overlay_event("match_end_economy", {"outcome": "loss"}))
    called = set(p.store.called_ids(r["id"]))
    assert {"new_god", "deathless", "kills_10", "match_loss"} <= called
    # the toggle silences everything
    bot.features["bingo"] = False
    assert asyncio.run(p.fire("kill"))["error"] == "bingo is off"


def test_chat_commands():
    p, bot, eco, ov = _plugin({"dyna": 0})
    class Msg:
        class chatter:
            name = "dyna"
    asyncio.run(bot.commands["bingo"](Msg(), ""))
    assert bot.chat[-1].startswith("reply: No bingo round")
    asyncio.run(bot.commands["bingostart"](Msg(), ""))
    asyncio.run(p.claim_card("dyna", "Dyna"))
    asyncio.run(bot.commands["bingo"](Msg(), ""))
    assert "You: 1 card(s)" in bot.chat[-1]
    asyncio.run(bot.commands["bingocall"](Msg(), ""))
    assert "Usage: !bingocall" in bot.chat[-1]
    asyncio.run(bot.commands["bingocall"](Msg(), "no_mana"))
    assert "no_mana" in p.store.called_ids(p.current()["id"])
    asyncio.run(bot.commands["bingoend"](Msg(), ""))
    assert p.current() is None and ov.events[-1][0] == "bingo_closed"


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
