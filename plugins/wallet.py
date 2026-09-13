"""
Wallet Plugin
=============
The viewer-facing side of core/wallet.py (docs/WALLET_PLAN.md):

  * passive earning — every WALLET_EARN_INTERVAL_MIN minutes while live,
    everyone in chat (Helix chatters, lurkers included) gets
    WALLET_EARN_HATS_PER_TICK hats; viewers who chatted since the last
    tick get WALLET_EARN_CHAT_BONUS on top, and subscribers who chatted
    get WALLET_EARN_SUB_MULTIPLIER on the tick. Each pass writes a
    wallet_earn_ticks row so "why didn't I get hats" is a query.
  * event bonuses — sub / gift / raid hooks (bot.py) call bonus().
  * chat — !hats (balance, tokens, watch time), !tophats,
    !givehats / !takehats (mods).
  * watch time — users.watch_minutes grows by the interval for every
    viewer a tick pays (imported from MixItUp's OnlineViewingMinutes).

Sub status is only known for viewers who chatted since the last tick
(it rides on the chat message badges); lurking subs earn the base rate.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional, Set, Tuple

import aiohttp

from core import config as _cfg
from core import db as _shared_db
from core import users as _users
from core import wallet as _wallet
from core.config import TWITCH_OWNER_ID, TWITCH_BOT_USERNAME


def _c(name: str, default):
    return getattr(_cfg, name, default)


class WalletPlugin:
    def __init__(self):
        self.bot = None
        self._db = None
        self._task: Optional[asyncio.Task] = None
        # viewers who chatted since the last tick: login -> is_sub
        self._chatted: Dict[str, bool] = {}
        self._chatted_ids: Dict[str, str] = {}
        self.stats = {"ticks": 0, "last_tick": None, "last_error": ""}

    # ── lifecycle ─────────────────────────────────────────────────────

    def setup(self, bot):
        self.bot = bot
        cur = _c("ECONOMY_CURRENCY_NAME", "Hats")
        bot.register_command("hats", self.cmd_hats,
                             description=f"Your {cur} balance", identity=True, plugin="wallet")
        bot.register_command("tophats", self.cmd_tophats,
                             description=f"Top {cur} balances", platforms=("twitch", "discord"), plugin="wallet")
        bot.register_command("givehats", self.cmd_givehats, mod_only=True,
                             description=f"Give a viewer {cur}: !givehats <user> <n>", plugin="wallet")
        bot.register_command("takehats", self.cmd_takehats, mod_only=True,
                             description=f"Take {cur} from a viewer: !takehats <user> <n>", plugin="wallet")
        if hasattr(bot, "add_raw_handler"):
            bot.add_raw_handler(self._on_chat)
        elif hasattr(bot, "_raw_handlers"):
            bot._raw_handlers.append(self._on_chat)
        if _shared_db.is_available():
            _shared_db.register_schema(_wallet.ensure_schema)

    async def on_ready(self):
        self._db = await _shared_db.get_db()
        if self._db is None:
            print("[Wallet] DB unavailable — wallet disabled")
            return
        if self._enabled():
            self._task = asyncio.create_task(self._earn_loop())
        print(f"[Wallet] ready — {_c('WALLET_EARN_HATS_PER_TICK', 25)} hats every "
              f"{_c('WALLET_EARN_INTERVAL_MIN', 5)} min while live "
              f"({'on' if self._enabled() else 'off'})")

    async def cleanup(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def _enabled(self) -> bool:
        if not _c("WALLET_EARN_ENABLED", True):
            return False
        if self.bot is not None and hasattr(self.bot, "is_feature_enabled"):
            return bool(self.bot.is_feature_enabled("wallet_earn"))
        return True

    # ── chat activity (for the chat bonus + sub multiplier) ───────────

    async def _on_chat(self, payload) -> None:
        try:
            chatter = getattr(payload, "chatter", None)
            login = (getattr(chatter, "name", "") or "").lower()
            if not login:
                return
            is_sub = False
            try:
                is_sub = bool(self.bot.is_sub(chatter))
            except Exception:
                pass
            self._chatted[login] = self._chatted.get(login, False) or is_sub
            tid = getattr(chatter, "id", None)
            if tid:
                self._chatted_ids[login] = str(tid)
        except Exception:
            pass

    # ── passive earning ───────────────────────────────────────────────

    def _is_live(self) -> bool:
        ss = (self.bot.plugins or {}).get("stream_status") if self.bot else None
        if ss is None or not hasattr(ss, "get_status"):
            return False
        try:
            return bool(ss.get_status().get("is_live"))
        except Exception:
            return False

    async def _earn_loop(self) -> None:
        interval = max(1, int(_c("WALLET_EARN_INTERVAL_MIN", 5))) * 60
        try:
            await asyncio.sleep(min(interval, 60))
        except asyncio.CancelledError:
            raise
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats["last_error"] = str(e)
                print(f"[Wallet] tick error: {e}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def _chatter_identities(self) -> List[Tuple[Optional[str], str]]:
        """(twitch_id, login) for everyone in chat via Helix. Reuses the
        economy plugin's fetcher when it is loaded (same token, same
        pagination); otherwise a minimal fetch of its own."""
        eco = (self.bot.plugins or {}).get("economy") if self.bot else None
        if eco is not None and hasattr(eco, "_get_chatter_identities"):
            return await eco._get_chatter_identities()
        tm = getattr(self.bot, "token_manager", None) if self.bot else None
        if tm is None:
            return []
        headers = await tm.get_broadcaster_headers()
        out: List[Tuple[Optional[str], str]] = []
        cursor = None
        base = (f"https://api.twitch.tv/helix/chat/chatters?broadcaster_id="
                f"{TWITCH_OWNER_ID}&moderator_id={TWITCH_OWNER_ID}&first=1000")
        async with aiohttp.ClientSession() as session:
            while True:
                url = base + (f"&after={cursor}" if cursor else "")
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Helix chatters HTTP {resp.status}")
                    data = await resp.json()
                for u in data.get("data", []):
                    login = (u.get("user_login") or "").lower()
                    if login:
                        out.append((str(u.get("user_id") or ""), login))
                cursor = data.get("pagination", {}).get("cursor")
                if not cursor:
                    break
        return out

    async def tick(self, chatters: Optional[List[Tuple[Optional[str], str]]] = None,
                   live: Optional[bool] = None, now: Optional[float] = None) -> dict:
        """One passive-earning pass. `chatters` / `live` / `now` are
        injectable for tests. Returns the wallet_earn_ticks row as a dict."""
        if self._db is None:
            return {"error": "no_db"}
        was_live = self._is_live() if live is None else bool(live)
        offline_ok = bool(_c("WALLET_EARN_OFFLINE", False))
        chatted, self._chatted = self._chatted, {}
        chatted_ids, self._chatted_ids = self._chatted_ids, {}
        result = {"was_live": was_live, "chatters": 0, "credited": 0,
                  "skipped": 0, "hats_total": 0, "error": None}
        if not was_live and not offline_ok:
            result["id"] = await _wallet.record_tick(
                self._db, was_live, 0, 0, 0, 0, None)
            self.stats["ticks"] += 1
            self.stats["last_tick"] = time.time()
            return result

        try:
            if chatters is None:
                chatters = await self._chatter_identities()
        except Exception as e:
            result["error"] = f"chatters: {e}"
            result["id"] = await _wallet.record_tick(
                self._db, was_live, 0, 0, 0, 0, result["error"])
            self.stats["last_error"] = result["error"]
            return result

        per_tick = int(_c("WALLET_EARN_HATS_PER_TICK", 25))
        chat_bonus = int(_c("WALLET_EARN_CHAT_BONUS", 0))
        sub_mult = float(_c("WALLET_EARN_SUB_MULTIPLIER", 1.0))
        interval_s = max(1, int(_c("WALLET_EARN_INTERVAL_MIN", 5))) * 60
        excluded = self._excluded_logins()

        # everyone in chat plus anyone who chatted but already left
        seen: Dict[str, Optional[str]] = {}
        for tid, login in chatters:
            if login and login not in seen:
                seen[login] = tid or None
        for login, tid in chatted_ids.items():
            seen.setdefault(login, tid)
        result["chatters"] = len(seen)

        tick_id = await _wallet.record_tick(
            self._db, was_live, len(seen), 0, 0, 0, None, commit=False)
        now_ts = time.time() if now is None else now
        credited = skipped = total = 0
        uuids: Dict[str, str] = {}
        for login, tid in seen.items():
            if login in excluded:
                skipped += 1
                continue
            try:
                if tid:
                    uid = await _users.get_or_create_twitch(
                        self._db, tid, login, commit=False)
                else:
                    uid = await _users.get_or_create_twitch_login(
                        self._db, login, commit=False)
            except Exception as e:
                print(f"[Wallet] cannot resolve {login}: {e}")
                skipped += 1
                continue
            uuids[login] = uid
        last = await _wallet.last_earned(self._db, uuids.values())
        for login, uid in uuids.items():
            # a restart mid-interval must not pay the same window twice
            prev = last.get(uid)
            if prev and _too_soon(prev, now_ts, interval_s * 0.5):
                skipped += 1
                continue
            amount = per_tick
            if chatted.get(login) and sub_mult > 1.0:
                amount = int(round(amount * sub_mult))
            if amount > 0:
                res = await _wallet.credit(
                    self._db, uid, "hats", amount, "watch",
                    ref=f"{tick_id}:{uid}", channel="system", commit=False)
                if res is None:
                    skipped += 1
                    continue
                total += amount
            if chat_bonus > 0 and login in chatted:
                res = await _wallet.credit(
                    self._db, uid, "hats", chat_bonus, "chat_bonus",
                    ref=f"{tick_id}:{uid}", channel="system", commit=False)
                if res is not None:
                    total += chat_bonus
            await _wallet.mark_earned(self._db, uid)
            await _users.add_watch_minutes(self._db, uid, interval_s // 60)
            credited += 1
        await self._db.execute(
            "UPDATE wallet_earn_ticks SET credited = ?, skipped = ?, "
            "hats_total = ? WHERE id = ?", (credited, skipped, total, tick_id))
        await self._db.commit()
        result.update({"id": tick_id, "credited": credited, "skipped": skipped,
                       "hats_total": total})
        self.stats["ticks"] += 1
        self.stats["last_tick"] = time.time()
        if credited:
            print(f"[Wallet] tick #{tick_id}: {credited} viewers +{total} hats "
                  f"({skipped} skipped)")
        return result

    def _excluded_logins(self) -> Set[str]:
        out = {(u or "").lower() for u in (_c("ECONOMY_EXCLUDED_USERNAMES", []) or [])}
        if TWITCH_BOT_USERNAME and TWITCH_BOT_USERNAME != "YOUR_BOT_USERNAME":
            out.add(TWITCH_BOT_USERNAME.lower())
        return out - {""}

    # ── event bonuses (called from bot.py) ────────────────────────────

    async def bonus(self, login: str, kind: str, ref: Optional[str] = None,
                    twitch_id: Optional[str] = None, count: int = 1) -> int:
        """Credit a configured one-off bonus. kind: sub | raid | bits |
        first_msg. Returns the hats paid (0 when the bonus is 0 or the
        ref was already paid)."""
        if self._db is None:
            return 0
        per = {"sub": _c("WALLET_BONUS_SUB", 0), "raid": _c("WALLET_BONUS_RAID", 0),
               "bits": _c("WALLET_BONUS_BITS_PER_100", 0),
               "first_msg": _c("WALLET_BONUS_FIRST_MSG", 0)}.get(kind, 0)
        amount = int(per) * max(1, int(count))
        if amount <= 0:
            return 0
        try:
            if twitch_id:
                uid = await _users.get_or_create_twitch(self._db, twitch_id, login, commit=False)
            else:
                uid = await _users.get_or_create_twitch_login(self._db, login, commit=False)
            res = await _wallet.credit(self._db, uid, "hats", amount, f"{kind}_bonus",
                                       ref=ref, channel="system")
        except Exception as e:
            print(f"[Wallet] {kind} bonus for {login} failed: {e}")
            return 0
        return amount if res is not None else 0

    # ── chat commands ─────────────────────────────────────────────────

    async def cmd_hats(self, message, args, whisper=False):
        if self._db is None:
            await self.bot.send_reply(message, "Hats are still loading. Try again in a moment.", whisper)
            return
        uid = await self.bot.user_uuid_for(message.chatter)
        if not uid:
            await self.bot.send_reply(message, "Hats are still loading. Try again in a moment.", whisper)
            return
        bal = await _wallet.get_all(self._db, uid)
        cur = _c("ECONOMY_CURRENCY_NAME", "Hats")
        extra = f" and {bal['god_token']} God Token(s)" if bal.get("god_token") else ""
        watched = _users.format_watch(await _users.watch_minutes_of(self._db, uid))
        await self.bot.send_reply(
            message, f"You have {bal['hats']:,} {cur}{extra}. Watch time: {watched}.",
            whisper)

    async def cmd_tophats(self, message, args, whisper=False):
        if self._db is None:
            return
        excluded = set()
        try:
            excluded = await _users.excluded_uuids(self._db, self._excluded_logins())
        except Exception:
            pass
        rows = await _wallet.leaderboard(self._db, "hats", 5, excluded)
        if not rows:
            await self.bot.send_reply(message, "Nobody has any Hats yet.", whisper)
            return
        text = " | ".join(f"{r['rank']}. {r['display_name']} {r['amount']:,}" for r in rows)
        await self.bot.send_reply(message, f"Top {_c('ECONOMY_CURRENCY_NAME', 'Hats')}: {text}", whisper)

    async def _mod_adjust(self, message, args, whisper, take: bool):
        if self._db is None:
            return
        parts = (args or "").split()
        if len(parts) != 2 or not parts[1].replace(",", "").isdigit():
            verb = "takehats" if take else "givehats"
            await self.bot.send_reply(message, f"Usage: !{verb} <user> <amount>", whisper)
            return
        target = parts[0].lstrip("@").lower()
        amount = int(parts[1].replace(",", ""))
        if amount <= 0:
            return
        actor = await self.bot.user_uuid_for(message.chatter) or "mod"
        try:
            uid = await _users.get_or_create_twitch_login(self._db, target)
            if take:
                res = await _wallet.debit(self._db, uid, "hats", amount, "mod_take",
                                          actor=actor, note=f"by {message.chatter.name}")
            else:
                res = await _wallet.credit(self._db, uid, "hats", amount, "mod_grant",
                                           actor=actor, note=f"by {message.chatter.name}")
        except Exception as e:
            await self.bot.send_reply(message, f"Failed: {e}", whisper)
            return
        if res is None:
            await self.bot.send_reply(message, f"{target} does not have {amount:,} Hats to take.", whisper)
            return
        word = "took" if take else "gave"
        await self.bot.send_reply(
            message, f"{word.capitalize()} {amount:,} Hats {'from' if take else 'to'} {target} "
                     f"(now {res:,}).", whisper)

    async def cmd_givehats(self, message, args, whisper=False):
        await self._mod_adjust(message, args, whisper, take=False)

    async def cmd_takehats(self, message, args, whisper=False):
        await self._mod_adjust(message, args, whisper, take=True)


def _too_soon(prev_iso: str, now_ts: float, window_s: float) -> bool:
    """True when the last credit (UTC 'YYYY-MM-DD HH:MM:SS') is within
    window_s of now."""
    from datetime import datetime, timezone
    try:
        prev = datetime.strptime(prev_iso, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return False
    return (now_ts - prev) < window_s
