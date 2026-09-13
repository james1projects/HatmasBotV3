"""
Burn Plugin
===========
`!burn <amount>`: destroy your own Hats for the whole stream to see. The
top of the "Hats as a flex" ladder (James, 2026-09-13): the spend
creates nothing, changes nothing about the stream, and only the size
of the number matters. Minimum BURN_MIN_HATS so it is a statement, not
spam; no cap, because outburning each other is the game.

What happens on a burn:
  * the wallet is debited (reason "burn", one conditional UPDATE, so a
    short balance changes nothing);
  * the alert box gets a `hats_burned` event (kind "burn": name, amount,
    biggest burn of the stream, all-time record, a sound);
  * chat sees the flex line, and whether it is the biggest of the stream
    or a new all-time record.

"Tonight" = since the stream went live (stream_status live listener
resets the session); the all-time record comes from the ledger, so it
survives restarts. `!burns` lists the biggest burns of the stream and
the all-time record; `!burned` is your own all-time total and rank.
Per-viewer totals are sums over the ledger (reason = burn), never a
second counter that could drift.

Feature toggle "burn".
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from core import config as _cfg
from core import db as _shared_db
from core import users as _users
from core import wallet as _wallet


def _c(name: str, default):
    return getattr(_cfg, name, default)


class BurnPlugin:
    def __init__(self):
        self.bot = None
        self._db = None
        self.session: Dict[str, Any] = {"since": None, "total": 0, "burns": []}

    # ── lifecycle ─────────────────────────────────────────────────────

    def setup(self, bot):
        self.bot = bot
        cur = _c("ECONOMY_CURRENCY_NAME", "Hats")
        bot.register_command(
            "burn", self.cmd_burn, identity=True, plugin="burn",
            description=f"Burn your {cur} for everyone to see: !burn <amount> (min {self._min():,})")
        bot.register_command(
            "burns", self.cmd_burns, platforms=("twitch", "discord"), plugin="burn",
            description=f"Biggest {cur} burns of the stream, the record, the top burners")
        bot.register_command(
            "burned", self.cmd_burned, identity=True, plugin="burn",
            description=f"How many {cur} you have burned all-time, and your rank")
        ss = (bot.plugins or {}).get("stream_status") if hasattr(bot, "plugins") else None
        if ss is not None and hasattr(ss, "add_live_listener"):
            ss.add_live_listener(self._on_live)

    async def on_ready(self):
        self._db = await _shared_db.get_db()
        if self._db is None:
            print("[Burn] DB unavailable — !burn disabled")

    async def _on_live(self, info=None):
        """A new stream: the "biggest burn tonight" board starts over."""
        self.session = {"since": time.time(), "total": 0, "burns": []}

    def _enabled(self) -> bool:
        if self.bot is not None and hasattr(self.bot, "is_feature_enabled"):
            return bool(self.bot.is_feature_enabled("burn"))
        return True

    @staticmethod
    def _min() -> int:
        return max(1, int(_c("BURN_MIN_HATS", 500)))

    # ── records ───────────────────────────────────────────────────────

    def stream_record(self) -> Optional[dict]:
        burns = self.session["burns"]
        return max(burns, key=lambda b: b["amount"]) if burns else None

    def stream_top(self, limit: int = 3) -> List[dict]:
        return sorted(self.session["burns"], key=lambda b: -b["amount"])[:limit]

    async def user_total(self, user_uuid: str) -> dict:
        """Everything this viewer has ever burned, and their rank among
        burners (1 = most burned)."""
        if self._db is None:
            return {"total": 0, "burns": 0, "rank": None, "burners": 0}
        async with self._db.execute(
                "SELECT COALESCE(SUM(-delta), 0), COUNT(*) FROM wallet_ledger "
                "WHERE reason = 'burn' AND user_uuid = ?", (user_uuid,)) as cur:
            total, count = await cur.fetchone()
        async with self._db.execute(
                "SELECT COUNT(*), SUM(CASE WHEN t > ? THEN 1 ELSE 0 END) FROM ("
                "SELECT SUM(-delta) AS t FROM wallet_ledger WHERE reason = 'burn' "
                "GROUP BY user_uuid)", (int(total),)) as cur:
            burners, ahead = await cur.fetchone()
        return {"total": int(total), "burns": int(count),
                "rank": (int(ahead or 0) + 1) if total else None, "burners": int(burners or 0)}

    async def top_burners(self, limit: int = 3) -> List[dict]:
        """Most Hats burned all-time, per viewer."""
        if self._db is None:
            return []
        out = []
        async with self._db.execute(
                "SELECT l.user_uuid, COALESCE(u.display_name, l.user_uuid), SUM(-l.delta) AS t, COUNT(*) "
                "FROM wallet_ledger l LEFT JOIN users u ON u.uuid = l.user_uuid "
                "WHERE l.reason = 'burn' GROUP BY l.user_uuid ORDER BY t DESC, l.user_uuid LIMIT ?",
                (int(limit),)) as cur:
            async for uuid_, name, total, count in cur:
                out.append({"rank": len(out) + 1, "user_uuid": uuid_, "display": name,
                            "total": int(total), "burns": int(count)})
        return out

    async def alltime_record(self) -> Optional[dict]:
        """Biggest single burn ever, from the ledger."""
        if self._db is None:
            return None
        async with self._db.execute(
                "SELECT l.user_uuid, COALESCE(u.display_name, l.user_uuid), -l.delta "
                "FROM wallet_ledger l LEFT JOIN users u ON u.uuid = l.user_uuid "
                "WHERE l.reason = 'burn' ORDER BY l.delta ASC, l.id ASC LIMIT 1") as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {"user_uuid": row[0], "display": row[1], "amount": int(row[2])}

    # ── the burn ──────────────────────────────────────────────────────

    async def burn(self, user_uuid: str, display: str, amount: int,
                   login: str = "") -> dict:
        """Debit, record, announce. Returns a result dict; "error" is set
        when nothing was burned."""
        cur = _c("ECONOMY_CURRENCY_NAME", "Hats")
        if self._db is None:
            return {"error": f"{cur} are still loading. Try again in a moment."}
        if amount < self._min():
            return {"error": f"Minimum burn is {self._min():,} {cur}."}
        prev_alltime = await self.alltime_record()
        prev_stream = self.stream_record()
        left = await _wallet.debit(self._db, user_uuid, "hats", amount, "burn",
                                   channel="chat", note=f"burned by {display}")
        if left is None:
            have = await _wallet.get(self._db, user_uuid)
            return {"error": f"You have {have:,} {cur}; you can't burn {amount:,}."}
        entry = {"user_uuid": user_uuid, "display": display, "login": login,
                 "amount": amount, "at": time.time()}
        self.session["burns"].append(entry)
        self.session["total"] += amount
        is_stream_record = prev_stream is None or amount > prev_stream["amount"]
        is_alltime_record = prev_alltime is None or amount > prev_alltime["amount"]
        record = self.stream_record()
        alltime = await self.alltime_record()
        data = {
            "display_name": display, "login": login, "amount": amount, "balance_after": left,
            "stream_total": self.session["total"], "stream_burns": len(self.session["burns"]),
            "stream_record": {"display": record["display"], "amount": record["amount"]} if record else None,
            "is_stream_record": is_stream_record,
            "alltime_record": {"display": alltime["display"], "amount": alltime["amount"]} if alltime else None,
            "is_alltime_record": is_alltime_record,
        }
        mine = await self.user_total(user_uuid)
        data.update(user_total=mine["total"], user_burns=mine["burns"],
                    user_rank=mine["rank"], burners=mine["burners"])
        await self._emit(data)
        return data

    async def _emit(self, data: dict) -> None:
        ws = getattr(self.bot, "web_server", None)
        overlay = getattr(ws, "overlay", None)
        if overlay is None:
            return
        try:
            await overlay.emit("hats_burned", data)
        except Exception as e:
            print(f"[Burn] alert emit failed: {e}")

    # ── chat ──────────────────────────────────────────────────────────

    async def cmd_burn(self, message, args, whisper=False):
        cur = _c("ECONOMY_CURRENCY_NAME", "Hats")
        if not self._enabled():
            await self.bot.send_reply(message, "Burning is closed right now.", whisper)
            return
        raw = (args or "").strip().replace(",", "")
        if not raw.isdigit() or int(raw) <= 0:
            await self.bot.send_reply(
                message, f"Usage: !burn <amount> (min {self._min():,} {cur}). It's gone forever.", whisper)
            return
        amount = int(raw)
        uid = await self.bot.user_uuid_for(message.chatter)
        if not uid:
            await self.bot.send_reply(message, f"{cur} are still loading. Try again in a moment.", whisper)
            return
        chatter = message.chatter
        display = getattr(chatter, "display_name", None) or chatter.name
        res = await self.burn(uid, str(display), amount, login=(chatter.name or "").lower())
        if res.get("error"):
            await self.bot.send_reply(message, res["error"], whisper)
            return
        tag = ""
        if res["is_alltime_record"]:
            tag = " NEW ALL-TIME RECORD!"
        elif res["is_stream_record"]:
            tag = " Biggest burn of the stream!"
        await self.bot.send_chat(f"{display} just burned {amount:,} {cur}!{tag}")
        total = ""
        if res["user_burns"] > 1:
            total = f" You've burned {res['user_total']:,} total (#{res['user_rank']} of {res['burners']} burners)."
        await self.bot.send_reply(
            message, f"{amount:,} {cur} gone. {res['balance_after']:,} left.{total}", whisper)

    async def cmd_burned(self, message, args, whisper=False):
        cur = _c("ECONOMY_CURRENCY_NAME", "Hats")
        uid = await self.bot.user_uuid_for(message.chatter)
        if not uid:
            await self.bot.send_reply(message, f"{cur} are still loading. Try again in a moment.", whisper)
            return
        mine = await self.user_total(uid)
        if not mine["total"]:
            await self.bot.send_reply(
                message, f"You haven't burned any {cur} yet. !burn <amount> (min {self._min():,}).", whisper)
            return
        await self.bot.send_reply(
            message, f"You've burned {mine['total']:,} {cur} in {mine['burns']} burn(s): "
                     f"#{mine['rank']} of {mine['burners']} burners.", whisper)

    async def cmd_burns(self, message, args, whisper=False):
        cur = _c("ECONOMY_CURRENCY_NAME", "Hats")
        top = self.stream_top(3)
        alltime = await self.alltime_record()
        parts = []
        if top:
            parts.append("Tonight: " + " | ".join(
                f"{i + 1}. {b['display']} {b['amount']:,}" for i, b in enumerate(top)))
        else:
            parts.append(f"Nobody has burned any {cur} tonight.")
        if alltime:
            parts.append(f"Biggest ever: {alltime['display']} {alltime['amount']:,}")
        top_total = await self.top_burners(3)
        if top_total:
            parts.append("Most burned: " + " | ".join(
                f"{b['rank']}. {b['display']} {b['total']:,}" for b in top_total))
        await self.bot.send_reply(message, " || ".join(parts), whisper)
