"""
plugins/bingo/plugin.py — Stream Bingo.

One round per stream (James presses BINGO START on the deck or the
dashboard). Viewers get a free 5x5 card on hatmaster.tv/bingo (Twitch
login) and can buy up to BINGO_MAX_CARDS with Hats at rising prices.
Squares get marked two ways:

  auto    kill detector (kill / death / assist / double..penta / first
          blood / 10 kills / 5 deaths / deathless) and the economy's
          match settle (win / loss / new god this stream)
  manual  James: deck button, dashboard button, or !bingocall <id>

A line does not win by itself: the viewer must press Bingo! on the site
(`claim_bingo`), the server re-checks the line against the calls, and the
first accepted claim wins the pot (BINGO_BASE_PRIZE + half of the Hats
spent on extra cards), paid through the economy's wallet balance like a
dividend; the round closes and a new one can start. Viewers can tick
"Show my card on stream" (prefs) to appear on the /overlay/bingo_cards
carousel.

Feature toggle "bingo" (default on) gates the site page and the API;
nothing happens anyway until a round is open.

The dashboard control page (localhost:8069/bingo, routes in
core/bingo_web.BingoControl) can also undo a call, force an auto square,
simulate detector events, edit the pool, and browse past rounds.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core import config

from .pool import (CARD_SIZE, FREE, label_of, load_pool, make_card, card_seed, next_card_price,
                   marked_indexes, save_pool, winning_lines)
from .store import BingoStore

Listener = Callable[[str, dict], Awaitable[None]]

MULTIKILL_TYPES = {"double_kill": "double", "triple_kill": "triple",
                   "quadra_kill": "quadra", "penta_kill": "penta"}

# Square ids the bot knows how to fire by itself. A pool square with
# source "auto" and any other id would never be marked, so the pool
# editor forces those to "manual".
AUTO_IDS = frozenset({"kill", "death", "assist", "double", "triple", "quadra", "penta",
                      "first_blood", "kills_10", "deaths_5", "deathless",
                      "match_win", "match_loss", "new_god"})

# Detector / economy events the control page can simulate (same code
# path as the real listeners, so the per-match counters are exercised).
SIMULATED_EVENTS = ("kill", "death", "assist", "double_kill", "triple_kill", "quadra_kill",
                    "penta_kill", "god", "win", "loss")


def square_id(text: str) -> str:
    """'Says "no mana"' -> 'says_no_mana' (lowercase, [a-z0-9_], <= 40)."""
    out = "".join(ch if ch.isalnum() else "_" for ch in str(text or "").strip().lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")[:40]


def _cfg(name: str, default: Any) -> Any:
    return getattr(config, name, default)


def render_card(card: dict, pool: List[dict], claimed: bool = False) -> dict:
    """Card as the page renders it: labels, marks, winning line, and
    whether this card's claim won the round."""
    marks = set(card.get("marks") or [])
    lines = winning_lines(marks)
    return {
        "id": int(card["id"]), "seq": int(card["seq"]), "price": int(card.get("price") or 0),
        "user_uuid": card.get("user_uuid"), "login": card.get("login"), "display": card.get("display"),
        "squares": [{"id": sid, "label": label_of(pool, sid), "marked": i in marks,
                     "free": sid == FREE} for i, sid in enumerate(card["squares"])],
        "to_bingo": int(card.get("to_bingo", 5)),
        "bingo": bool(lines), "line": list(lines[0]) if lines else None,
        "created_at": card.get("created_at"), "bingo_at": card.get("bingo_at"),
        "claimed": bool(claimed),
    }


class BingoPlugin:
    def __init__(self, overlay_manager=None):
        self.bot = None
        self.overlay_manager = overlay_manager
        self.store: Optional[BingoStore] = None
        self.pool: List[dict] = []
        self._listeners: List[Listener] = []
        self._lock = asyncio.Lock()
        # per-match counters for the threshold squares
        self._match_kills = 0
        self._match_deaths = 0
        self._match_open = False
        self._gods_this_round: set = set()
        self.stats = {"calls": 0, "errors": 0, "last_error": ""}

    # ── lifecycle ─────────────────────────────────────────────────────

    def setup(self, bot):
        self.bot = bot
        bot.register_command("bingo", self.cmd_bingo)
        bot.register_command("bingocall", self.cmd_bingocall, mod_only=True)
        bot.register_command("bingostart", self.cmd_bingostart, mod_only=True)
        bot.register_command("bingoend", self.cmd_bingoend, mod_only=True)

    async def on_ready(self):
        self.pool = load_pool(Path(_cfg("BINGO_POOL_FILE", config.DATA_DIR / "bingo" / "pool.json")))
        self.store = BingoStore(_cfg("BINGO_DB", config.DATA_DIR / "bingo.db"))
        try:
            from core import users as _users
            _users.register_merge_hook(self._on_user_merge)
        except Exception as e:
            self._error(f"merge hook: {e}")
        if self.overlay_manager is not None:
            try:
                self.overlay_manager.add_event_listener(self._on_overlay_event)
            except Exception as e:
                self._error(f"overlay listener: {e}")
        r = self.store.current_round()
        print(f"[Bingo] ready: {len(self.pool)} squares in the pool, "
              f"{'round ' + str(r['id']) + ' open' if r else 'no open round'}, "
              f"toggle {'on' if self._enabled() else 'off'}")

    async def _on_user_merge(self, absorbed: str, survivor: str) -> dict:
        return self.store.repoint_user(absorbed, survivor) if self.store else {}

    async def cleanup(self):
        if self.store:
            self.store.close()

    def attach_detector(self, kd) -> None:
        kd.add_kill_listener(self._on_kill)
        kd.add_multikill_listener(self._on_multikill)
        kd.add_death_listener(self._on_death)
        kd.add_assist_listener(self._on_assist)

    def add_listener(self, coro: Listener) -> None:
        """coro(event, data) for bingo_open / bingo_call / bingo_line /
        bingo_claim / bingo_closed / card_added / bingo_prefs. The public
        server pushes these to /ws/bingo."""
        self._listeners.append(coro)

    # ── helpers ───────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        if self.bot is None or not hasattr(self.bot, "is_feature_enabled"):
            return True
        return bool(self.bot.is_feature_enabled("bingo"))

    def _economy(self):
        plugins = getattr(self.bot, "plugins", {}) if self.bot else {}
        return plugins.get("economy")

    def _error(self, msg: str) -> None:
        self.stats["errors"] += 1
        self.stats["last_error"] = msg
        print(f"[Bingo] {msg}")

    async def _notify(self, event: str, data: dict) -> None:
        if self.overlay_manager is not None:
            try:
                await self.overlay_manager.emit(event, data)
            except Exception as e:
                self._error(f"overlay emit {event}: {e}")
        for fn in list(self._listeners):
            try:
                await fn(event, data)
            except Exception as e:
                self._error(f"listener {event}: {e}")

    async def _say(self, text: str) -> None:
        if self.bot is None:
            return
        try:
            await self.bot.send_chat(text)
        except Exception as e:
            self._error(f"chat: {e}")

    def prices(self) -> List[int]:
        return [int(p) for p in _cfg("BINGO_CARD_PRICES", (50, 100, 200))]

    def max_cards(self) -> int:
        return int(_cfg("BINGO_MAX_CARDS", 4))

    def manual_squares(self) -> List[dict]:
        return [sq for sq in self.pool if sq["source"] == "manual"]

    # ── round control ─────────────────────────────────────────────────

    def current(self) -> Optional[dict]:
        return self.store.current_round() if self.store else None

    async def start_round(self) -> dict:
        async with self._lock:
            r = self.store.open_round(int(_cfg("BINGO_BASE_PRIZE", 500)), float(_cfg("BINGO_POT_SHARE", 0.5)))
            self._gods_this_round = set()
            self._match_kills = self._match_deaths = 0
            summary = self.store.summary(r["id"])
        await self._notify("bingo_open", summary)
        await self._say(f"BINGO is open! Grab your free card at hatmaster.tv/bingo and watch the board fill. "
                        f"Pot: {summary['pot']} Hats.")
        return summary

    async def end_round(self, reason: str = "manual") -> Optional[dict]:
        async with self._lock:
            r = self.store.current_round()
            if not r:
                return None
            self.store.close_round(r["id"])
            summary = self.store.summary(r["id"])
        summary["reason"] = reason
        await self._notify("bingo_closed", summary)
        if reason == "manual":
            await self._say("Bingo round closed with no winner. Cards reset next round.")
        return summary

    # ── calls ─────────────────────────────────────────────────────────

    def known_event(self, event_id: str) -> Optional[dict]:
        for sq in self.pool:
            if sq["id"] == event_id:
                return sq
        return None

    async def fire(self, event_id: str, source: str = "manual") -> dict:
        """Call a square. Marks every card, pays and closes on a win."""
        sq = self.known_event(event_id)
        if not sq:
            return {"ok": False, "error": f"unknown square '{event_id}'"}
        if not self._enabled() or not self.store:
            return {"ok": False, "error": "bingo is off"}
        async with self._lock:
            r = self.store.current_round()
            if not r:
                return {"ok": False, "error": "no open round"}
            res = self.store.mark_event(r["id"], event_id, sq["label"], source)
            self.stats["calls"] += 1
            summary = self.store.summary(r["id"])
        lines = res["winners"]          # cards that just completed a line; they still have to claim
        out = {"ok": True, "round_id": r["id"], "event_id": event_id, "label": sq["label"],
               "source": source, "already": res["already"], "changed": len(res["changed"]),
               "lines": [{"user_uuid": w["user_uuid"], "login": w["login"], "display": w["display"],
                          "card_id": w["id"]} for w in lines],
               "summary": summary}
        await self._notify("bingo_call", {**summary, "call": {"event_id": event_id, "label": sq["label"],
                                                             "source": source, "already": res["already"]},
                                          "changed_cards": [c["id"] for c in res["changed"]],
                                          "lines": out["lines"]})
        if lines:
            await self._notify("bingo_line", {**summary, "call": {"event_id": event_id, "label": sq["label"]},
                                              "lines": out["lines"]})
            names = ", ".join(sorted({w["display"] for w in lines}))
            await self._say(f"Bingo call: {sq['label']}. {names} has a line! Press Bingo! on hatmaster.tv/bingo "
                            f"to claim the {summary['pot']} Hats.")
        elif not res["already"] and res["changed"]:
            lead = summary["leaders"][0] if summary["leaders"] else None
            tail = f" Closest: {lead['display']} needs {lead['to_bingo']}." if lead and lead["to_bingo"] <= 1 else ""
            await self._say(f"Bingo call: {sq['label']} ({len(res['changed'])} cards marked).{tail}")
        return out

    async def claim_bingo(self, user_uuid: str, card_id: Any) -> dict:
        """The viewer pressed Bingo!. Re-check the line against the calls,
        pay the pot, close the round, announce. First valid claim wins."""
        user_uuid = (user_uuid or "").strip()
        if not self._enabled() or not self.store:
            return {"ok": False, "error": "Bingo is off right now."}
        try:
            card_id = int(card_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Which card?"}
        async with self._lock:
            r = self.store.current_round()
            if not r:
                last = self.store.last_round()
                if last and last.get("winner_uuid"):
                    who = "you" if last["winner_uuid"] == user_uuid else last.get("winner_name")
                    return {"ok": False, "error": f"The round is over: {who} already claimed it."}
                return {"ok": False, "error": "No round is open."}
            chk = self.store.check_claim(r["id"], card_id, user_uuid)
            if not chk["ok"]:
                return chk
            card = chk["card"]
            summary = self.store.summary(r["id"])
            prize = summary["pot"]
            payout = await self._pay_winner(r, card, prize)
            self.store.close_round(r["id"], winner={"user_uuid": card["user_uuid"], "login": card["login"],
                                                    "display": card["display"], "card_id": card["id"]},
                                   prize_paid=prize, prize_ok=payout)
            summary = self.store.summary(r["id"])
        winner = {"user_uuid": card["user_uuid"], "login": card["login"], "display": card["display"],
                  "card_id": card["id"], "prize": prize, "paid": bool(payout), "line": list(chk["line"])}
        await self._notify("bingo_claim", {**summary, "winner": winner})
        paid = f"{prize} Hats paid out" if payout else f"{prize} Hats owed (balance service unavailable)"
        await self._say(f"BINGO! {card['display']} claimed it. {paid}. Next round opens when Hatmaster starts it.")
        return {"ok": True, "winner": winner, "summary": summary, "card": render_card(card, self.pool, claimed=True)}

    # ── show my card on stream ────────────────────────────────────────

    def on_stream(self, user_uuid: str) -> bool:
        return bool(self.store and user_uuid and self.store.on_stream(user_uuid))

    async def set_on_stream(self, user_uuid: str, on: bool) -> dict:
        user_uuid = (user_uuid or "").strip()
        if not self.store or not user_uuid:
            return {"ok": False, "error": "Log in first."}
        self.store.set_on_stream(user_uuid, bool(on))
        r = self.current()
        if r:
            await self._notify("bingo_prefs", {**self.store.summary(r["id"]), "user_uuid": user_uuid,
                                               "on_stream": bool(on)})
        return {"ok": True, "on_stream": bool(on)}

    def cards_on_stream(self) -> dict:
        """What the /overlay/bingo_cards carousel shows: opted-in cards of
        the open round, closest to bingo first."""
        r = self.current()
        if not r:
            return {"open": False, "round": None, "cards": []}
        summary = self.store.summary(r["id"])
        return {"open": True, "round": r["id"], "pot": summary["pot"], "calls": len(summary["calls"]),
                "cards": [render_card(c, self.pool) for c in self.store.cards_on_stream(r["id"])]}

    async def _pay_winner(self, round_row: dict, card: dict, prize: int) -> bool:
        eco = self._economy()
        if eco is None or not getattr(eco, "_connected", False) or prize <= 0:
            return False
        try:
            return bool(await eco._adjust_balance(
                card["user_uuid"], int(prize), reason="bingo_prize",
                ref=f"{round_row['id']}:{card['user_uuid']}", channel="web"))
        except Exception as e:
            self._error(f"payout: {e}")
            return False

    # ── cards ─────────────────────────────────────────────────────────

    async def claim_card(self, user_uuid: str, display: str, login: Optional[str] = None) -> dict:
        """Free card first, then bought ones at rising prices (Hats via
        the economy plugin). -> {"ok", "card"|"error", "price"}"""
        if not self._enabled() or not self.store:
            return {"ok": False, "error": "Bingo is off right now."}
        user_uuid = (user_uuid or "").strip()
        if not user_uuid:
            return {"ok": False, "error": "Log in first."}
        async with self._lock:
            r = self.store.current_round()
            if not r:
                return {"ok": False, "error": "No bingo round is open. Wait for Hatmaster to start one."}
            mine = self.store.cards_for(r["id"], user_uuid)
            seq = len(mine) + 1
            if seq > self.max_cards():
                return {"ok": False, "error": f"You already have the maximum of {self.max_cards()} cards."}
            price = next_card_price(seq, self.prices())
            if price is None:
                return {"ok": False, "error": "No more cards for sale this round."}
            if price > 0:
                eco = self._economy()
                if eco is None or not getattr(eco, "_connected", False):
                    return {"ok": False, "error": "Hats are unavailable right now, so extra cards are off."}
                bal = await eco._get_balance(user_uuid)
                if bal is None or bal < price:
                    return {"ok": False, "error": f"That card costs {price} Hats; you have {bal or 0}."}
                if not await eco._adjust_balance(user_uuid, -int(price), reason="bingo_card",
                                                 note=f"round {r['id']} card {seq}", channel="web"):
                    return {"ok": False, "error": "Could not take the Hats. Try again."}
            squares = make_card(self.pool, card_seed(r["id"], user_uuid, seq))
            card = self.store.add_card(r["id"], user_uuid, login, display, seq, price, squares,
                                       self.store.called_ids(r["id"]))
            summary = self.store.summary(r["id"])
        await self._notify("card_added", {**summary, "user_uuid": user_uuid, "login": login,
                                          "display": display, "seq": seq, "price": price})
        return {"ok": True, "card": render_card(card, self.pool), "price": price,
                "next_price": next_card_price(seq + 1, self.prices()) if seq + 1 <= self.max_cards() else None}

    def my_cards(self, user_uuid: str) -> dict:
        """The viewer's cards for the open round, or, once it has closed,
        for the last round (so a winning line stays on screen until the
        next round opens). Buying is only possible while open; a card with
        `bingo` true and the round open shows the Bingo! button."""
        if not self.store or not user_uuid:
            return {"round": None, "open": False, "cards": [], "next_price": None, "can_claim": False,
                    "on_stream": False, "winner": None}
        r = self.current()
        is_open = r is not None
        if r is None:
            r = self.store.last_round()
        if r is None:
            return {"round": None, "open": False, "cards": [], "next_price": None, "can_claim": False}
        cards = self.store.cards_for(r["id"], user_uuid)
        seq = len(cards) + 1
        nxt = next_card_price(seq, self.prices()) if (is_open and seq <= self.max_cards()) else None
        won = r.get("winner_card")
        return {"round": r["id"], "open": is_open,
                "cards": [render_card(c, self.pool, claimed=(won is not None and c["id"] == won)) for c in cards],
                "next_price": nxt, "can_claim": nxt is not None, "on_stream": self.store.on_stream(user_uuid),
                "winner": {"user_uuid": r.get("winner_uuid"), "login": r.get("winner_login"),
                           "display": r.get("winner_name")} if r.get("winner_uuid") else None}

    def public_state(self) -> dict:
        r = self.current() if self.store else None
        last = self.store.last_round() if self.store else None
        state = {"enabled": self._enabled(), "open": bool(r), "prices": self.prices(),
                 "max_cards": self.max_cards(), "round": None, "last": None}
        if r:
            state["round"] = self.store.summary(r["id"])
        elif last:
            state["last"] = self.store.summary(last["id"])
        return state

    def status(self) -> dict:
        s = self.public_state()
        s["manual_squares"] = self.manual_squares()
        s["auto_squares"] = [sq for sq in self.pool if sq["source"] == "auto"]
        s["match"] = {"open": self._match_open, "kills": self._match_kills, "deaths": self._match_deaths,
                      "gods_this_round": sorted(self._gods_this_round)}
        s["auto_ids"] = self.auto_ids()
        s["simulated_events"] = list(SIMULATED_EVENTS)
        s["pool_file"] = str(self._pool_path())
        s["round_cards"] = self.round_cards()
        s["history"] = self.history(10)
        s.update(self.stats)
        return s

    def history(self, limit: int = 20) -> List[dict]:
        """Summaries of the most recent rounds, newest first (the open
        round included, if any)."""
        if not self.store:
            return []
        return [self.store.summary(r["id"]) for r in self.store.rounds(limit)]

    def round_cards(self) -> List[dict]:
        r = self.current()
        return self.store.card_list(r["id"]) if r else []

    @staticmethod
    def auto_ids() -> List[str]:
        return sorted(AUTO_IDS)

    # ── pool editing (data/bingo/pool.json) ───────────────────────────

    def _pool_path(self) -> Path:
        return Path(_cfg("BINGO_POOL_FILE", config.DATA_DIR / "bingo" / "pool.json"))

    def _save_pool(self) -> None:
        try:
            save_pool(self._pool_path(), self.pool)
        except OSError as e:
            self._error(f"pool save: {e}")

    def set_square(self, sid: str, label: str, source: str = "manual", weight: Any = 1) -> dict:
        """Add a square or update an existing one (by id). Squares added
        mid-round land on cards claimed from now on; label and weight
        edits show up everywhere at once."""
        sid = square_id(sid or label)
        label = str(label or "").strip()[:60]
        if not sid or sid == FREE:
            return {"ok": False, "error": "square needs an id (letters, digits, underscores)"}
        if not label:
            return {"ok": False, "error": "square needs a label"}
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            return {"ok": False, "error": "weight must be a number"}
        if not weight > 0:
            return {"ok": False, "error": "weight must be above 0"}
        source = "auto" if (str(source).lower() == "auto" and sid in AUTO_IDS) else "manual"
        sq = {"id": sid, "label": label, "source": source, "weight": weight}
        created = True
        for i, cur in enumerate(self.pool):
            if cur["id"] == sid:
                self.pool[i] = sq
                created = False
                break
        else:
            self.pool.append(sq)
        self._save_pool()
        return {"ok": True, "square": sq, "created": created, "pool_size": len(self.pool)}

    def remove_square(self, sid: str) -> dict:
        """Drop a square from the pool. Refused while a round is open
        (cards in play hold it) and when the pool would get too small."""
        sid = square_id(sid)
        if self.current():
            return {"ok": False, "error": "close the round first: cards in play hold this square"}
        if not self.known_event(sid):
            return {"ok": False, "error": f"unknown square '{sid}'"}
        if len(self.pool) - 1 < CARD_SIZE - 1:
            return {"ok": False, "error": f"the pool needs at least {CARD_SIZE - 1} squares"}
        self.pool = [sq for sq in self.pool if sq["id"] != sid]
        self._save_pool()
        return {"ok": True, "removed": sid, "pool_size": len(self.pool)}

    def reload_pool(self) -> dict:
        """Re-read pool.json after a hand edit."""
        self.pool = load_pool(self._pool_path())
        return {"ok": True, "pool_size": len(self.pool)}

    # ── undo + simulated events (control page) ────────────────────────

    async def uncall(self, event_id: str) -> dict:
        """Undo a call in the open round (a mis-pressed deck key): the
        call rows go, every card's marks are rebuilt from what remains."""
        event_id = (event_id or "").strip().lower()
        if not self.store:
            return {"ok": False, "error": "bingo is off"}
        async with self._lock:
            r = self.store.current_round()
            if not r:
                return {"ok": False, "error": "no open round"}
            res = self.store.uncall(r["id"], event_id)
            summary = self.store.summary(r["id"])
        if not res["removed"]:
            return {"ok": False, "error": f"'{event_id}' has not been called this round"}
        label = label_of(self.pool, event_id)
        await self._notify("bingo_uncall", {**summary, "call": {"event_id": event_id, "label": label},
                                            "changed_cards": [c["id"] for c in res["changed"]]})
        await self._say(f"Bingo call undone: {label} (mis-press). {len(res['changed'])} cards unmarked.")
        return {"ok": True, "round_id": r["id"], "event_id": event_id, "label": label,
                "removed": res["removed"], "changed": len(res["changed"]), "summary": summary}

    async def simulate(self, kind: str, value: str = "") -> dict:
        """Feed a fake detector / economy event through the real listeners
        (so first blood, 10 kills, deathless etc. are testable without a
        match). kind: kill death assist double_kill..penta_kill god win loss."""
        kind = (kind or "").strip().lower()
        if kind not in SIMULATED_EVENTS:
            return {"ok": False, "error": f"unknown event '{kind}'; one of {', '.join(SIMULATED_EVENTS)}"}
        r = self.current()
        before = len(self.store.called_ids(r["id"])) if r else 0
        if kind == "kill":
            await self._on_kill("kill")
        elif kind == "death":
            await self._on_death()
        elif kind == "assist":
            await self._on_assist()
        elif kind in MULTIKILL_TYPES:
            await self._on_multikill(kind)
        elif kind == "god":
            await self._on_overlay_event("economy_god_detected", {"god": (value or "Test God").strip()})
        else:
            await self._on_overlay_event("match_end_economy", {"outcome": kind})
        r = self.current()
        called = self.store.called_ids(r["id"]) if r else []
        return {"ok": True, "simulated": kind, "new_calls": max(0, len(called) - before),
                "match": {"open": self._match_open, "kills": self._match_kills, "deaths": self._match_deaths,
                          "gods_this_round": sorted(self._gods_this_round)},
                "summary": self.store.summary(r["id"]) if r else None}

    # ── automatic squares ─────────────────────────────────────────────

    async def _auto(self, event_id: str) -> None:
        if not self.store or not self.store.current_round():
            return
        try:
            await self.fire(event_id, source="auto")
        except Exception as e:
            self._error(f"auto {event_id}: {e}")

    async def _on_kill(self, kill_type: str, count: int = 1) -> None:
        first = self._match_kills == 0
        self._match_kills += int(count or 1)
        await self._auto("kill")
        if first:
            await self._auto("first_blood")
        if self._match_kills >= 10:
            await self._auto("kills_10")

    async def _on_multikill(self, kill_type: str) -> None:
        sq = MULTIKILL_TYPES.get(str(kill_type))
        if sq:
            await self._auto(sq)

    async def _on_death(self, count: int = 1) -> None:
        self._match_deaths += int(count or 1)
        await self._auto("death")
        if self._match_deaths >= 5:
            await self._auto("deaths_5")

    async def _on_assist(self, count: int = 1) -> None:
        await self._auto("assist")

    async def _on_overlay_event(self, event_name: str, data: Any) -> None:
        if event_name == "economy_god_detected":
            god = (data or {}).get("god") if isinstance(data, dict) else None
            self._match_open = True
            self._match_kills = self._match_deaths = 0
            if god and self.store and self.store.current_round():
                if god not in self._gods_this_round:
                    self._gods_this_round.add(god)
                    if len(self._gods_this_round) > 1:
                        await self._auto("new_god")
                else:
                    pass
        elif event_name == "match_end_economy":
            outcome = str((data or {}).get("outcome") or "").lower() if isinstance(data, dict) else ""
            if outcome in ("win", "won", "victory"):
                await self._auto("match_win")
            elif outcome in ("loss", "lost", "defeat"):
                await self._auto("match_loss")
            if self._match_open and self._match_deaths == 0:
                await self._auto("deathless")
            self._match_open = False
            self._match_kills = self._match_deaths = 0

    # ── chat commands ─────────────────────────────────────────────────

    async def cmd_bingo(self, message, args: str) -> None:
        state = self.public_state()
        if not state["open"]:
            await self.bot.send_reply(message, "No bingo round is open right now.")
            return
        s = state["round"]
        user_uuid = await self.bot.user_uuid_for(getattr(message, "chatter", None)) if self.bot else None
        mine = self.store.cards_for(s["round_id"], user_uuid) if user_uuid else []
        best = min((c["to_bingo"] for c in mine), default=None)
        you = f" You: {len(mine)} card(s), {best} to go." if mine else " Grab a free card at hatmaster.tv/bingo."
        await self.bot.send_reply(message, f"Bingo round open: {s['cards']} cards, {s['players']} players, "
                                           f"pot {s['pot']} Hats, {len(s['calls'])} calls so far.{you}")

    async def cmd_bingocall(self, message, args: str) -> None:
        event_id = (args or "").strip().lower().split(" ")[0]
        if not event_id:
            ids = ", ".join(sq["id"] for sq in self.manual_squares())
            await self.bot.send_reply(message, f"Usage: !bingocall <square>. Manual squares: {ids}")
            return
        res = await self.fire(event_id, source="chat")
        if not res.get("ok"):
            await self.bot.send_reply(message, f"bingo: {res.get('error')}")

    async def cmd_bingostart(self, message, args: str) -> None:
        await self.start_round()

    async def cmd_bingoend(self, message, args: str) -> None:
        out = await self.end_round("manual")
        if out is None:
            await self.bot.send_reply(message, "No open round.")
