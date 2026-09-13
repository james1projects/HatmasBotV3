"""
Timed Messages Plugin
=====================
Messages the bot says in chat on a rotation, managed by mods from the
/mod page (no code, no restart). This replaces the last MixItUp feature
still in use ("say this every N minutes").

Model (docs/HATMASBOT.md v2.18):
  * A **lane** is a rotation group with its own interval in minutes.
    Messages in one lane post one at a time, in list order, wrapping
    round; different lanes run independently (same idea as the alert
    box's lanes). Lanes are just names typed on the page.
  * A **global minimum gap** (seconds) between any two rotated messages,
    whatever lane they came from, so two lanes never post back to back.
    A lane that is due while the gap is still running waits and posts
    as soon as the gap allows.
  * **Live only** (default on): nothing posts while the stream is
    offline (plugins/stream_status.py get_status()["is_live"]). Lanes
    that come due while offline are re-armed, so going live never dumps
    a backlog into chat.
  * Optional per lane: **min_chat_messages** — the lane only posts once
    chat has had that many messages since the lane's last post (0 = off,
    the MixItUp "minimum chat messages" option).

Storage: data/timed_messages.json (core/atomic_io)
  {"settings": {"live_only": true, "global_gap_sec": 60},
   "lanes": {"general": {"interval_min": 10, "min_chat_messages": 0}},
   "messages": [{"id": "a1b2c3d4", "text": "...", "lane": "general",
                 "enabled": true, "created_by": "modname",
                 "created_at": "...", "updated_at": "..."}]}

The loop is one `step(now)` per second; `step` is pure state + clock so
tests/test_timed_messages.py drives it with a fake clock and a fake bot.
Feature toggle "timed_messages" (DEFAULT_FEATURES).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from core import config as _cfg
from core.config import DATA_DIR

STORE_FILE = getattr(_cfg, "TIMED_MESSAGES_FILE", DATA_DIR / "timed_messages.json")
LANE_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
MAX_TEXT = 450
MAX_MESSAGES = 200
MAX_LANES = 20
MIN_INTERVAL_MIN = 1
MAX_INTERVAL_MIN = 24 * 60
MAX_GAP_SEC = 3600
MAX_MIN_CHAT = 1000
DEFAULT_LANE = "general"


def _c(name: str, default):
    return getattr(_cfg, name, default)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default_store() -> dict:
    return {
        "settings": {"live_only": True,
                     "global_gap_sec": int(_c("TIMED_MESSAGES_DEFAULT_GAP_SEC", 60))},
        "lanes": {DEFAULT_LANE: {"interval_min": int(_c("TIMED_MESSAGES_DEFAULT_INTERVAL_MIN", 10)),
                                 "min_chat_messages": 0}},
        "messages": [],
    }


def _clean_lane(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    try:
        interval = int(raw.get("interval_min", 10))
        min_chat = int(raw.get("min_chat_messages", 0))
    except (TypeError, ValueError):
        return None
    return {"interval_min": max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, interval)),
            "min_chat_messages": max(0, min(MAX_MIN_CHAT, min_chat))}


def normalize(raw) -> dict:
    """Coerce whatever was on disk into a valid store: bad lanes and
    messages are dropped, every message points at an existing lane."""
    store = _default_store()
    if not isinstance(raw, dict):
        return store
    s = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    store["settings"]["live_only"] = bool(s.get("live_only", True))
    try:
        gap = int(s.get("global_gap_sec", store["settings"]["global_gap_sec"]))
    except (TypeError, ValueError):
        gap = store["settings"]["global_gap_sec"]
    store["settings"]["global_gap_sec"] = max(0, min(MAX_GAP_SEC, gap))

    lanes_in = raw.get("lanes") if isinstance(raw.get("lanes"), dict) else {}
    lanes = {}
    for name, lane in lanes_in.items():
        name = str(name).strip().lower()
        clean = _clean_lane(lane)
        if LANE_RE.match(name) and clean is not None and len(lanes) < MAX_LANES:
            lanes[name] = clean
    if lanes:
        store["lanes"] = lanes

    msgs = []
    seen_ids = set()
    for m in raw.get("messages") or []:
        if not isinstance(m, dict):
            continue
        text = str(m.get("text") or "").strip()[:MAX_TEXT]
        lane = str(m.get("lane") or "").strip().lower()
        mid = str(m.get("id") or "").strip()
        if not text or lane not in store["lanes"] or not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)
        msgs.append({"id": mid, "text": text, "lane": lane,
                     "enabled": bool(m.get("enabled", True)),
                     "created_by": str(m.get("created_by") or ""),
                     "created_at": str(m.get("created_at") or ""),
                     "updated_at": str(m.get("updated_at") or "")})
        if len(msgs) >= MAX_MESSAGES:
            break
    store["messages"] = msgs
    return store


class TimedMessagesPlugin:
    def __init__(self, clock: Optional[Callable[[], float]] = None,
                 store_file=None):
        self.bot = None
        self._clock = clock or time.monotonic
        self._store_file = store_file or STORE_FILE
        self.store = _default_store()
        self._task: Optional[asyncio.Task] = None
        # runtime (never persisted): per lane next index / due time /
        # chat messages since that lane last posted
        self._next_index: Dict[str, int] = {}
        self._due_at: Dict[str, float] = {}
        self._chat_since: Dict[str, int] = {}
        self._last_post_at: Optional[float] = None
        self.stats = {"posted": 0, "last_text": "", "last_lane": "",
                      "last_post_wall": None, "last_error": ""}
        self._load()

    # ── persistence ───────────────────────────────────────────────────

    def _load(self):
        try:
            if self._store_file.exists():
                raw = json.loads(self._store_file.read_text(encoding="utf-8"))
                self.store = normalize(raw)
        except Exception as e:
            print(f"[TimedMsgs] Failed to load store: {e}")
            self.store = _default_store()

    def _save(self):
        try:
            from core.atomic_io import atomic_write_json
            atomic_write_json(self._store_file, self.store)
        except Exception as e:
            print(f"[TimedMsgs] Failed to save store: {e}")

    # ── plugin lifecycle ──────────────────────────────────────────────

    def setup(self, bot):
        self.bot = bot
        if hasattr(bot, "register_raw_handler"):
            bot.register_raw_handler(self._on_chat)
        elif hasattr(bot, "_raw_handlers"):
            bot._raw_handlers.append(self._on_chat)

    async def on_ready(self):
        self._arm_all(self._clock())
        self._task = asyncio.create_task(self._loop())
        n = len([m for m in self.store["messages"] if m["enabled"]])
        print(f"[TimedMsgs] ready — {n} enabled message(s) in "
              f"{len(self.store['lanes'])} lane(s) ({'on' if self._enabled() else 'off'})")

    async def cleanup(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def _enabled(self) -> bool:
        if self.bot is not None and hasattr(self.bot, "is_feature_enabled"):
            return bool(self.bot.is_feature_enabled("timed_messages"))
        return True

    def _is_live(self) -> bool:
        ss = (self.bot.plugins or {}).get("stream_status") if self.bot else None
        if ss is None or not hasattr(ss, "get_status"):
            return False
        try:
            return bool(ss.get_status().get("is_live"))
        except Exception:
            return False

    async def _loop(self):
        period = float(_c("TIMED_MESSAGES_TICK_SEC", 1.0))
        while True:
            try:
                await asyncio.sleep(period)
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats["last_error"] = str(e)
                print(f"[TimedMsgs] step error: {e}")

    # ── chat activity (for min_chat_messages) ─────────────────────────

    async def _on_chat(self, payload) -> None:
        try:
            chatter = getattr(payload, "chatter", None)
            login = (getattr(chatter, "name", "") or "").lower()
            bot_login = (getattr(self.bot, "bot_username", None)
                         or _c("TWITCH_BOT_USERNAME", "") or "").lower()
            if login and login == bot_login:
                return
            for lane in self.store["lanes"]:
                self._chat_since[lane] = self._chat_since.get(lane, 0) + 1
        except Exception:
            pass

    # ── the rotation ──────────────────────────────────────────────────

    def _arm(self, lane: str, now: float):
        self._due_at[lane] = now + self.store["lanes"][lane]["interval_min"] * 60

    def _arm_all(self, now: float):
        for lane in self.store["lanes"]:
            self._due_at.setdefault(lane, now + self.store["lanes"][lane]["interval_min"] * 60)
            self._next_index.setdefault(lane, 0)
            self._chat_since.setdefault(lane, 0)
        for lane in list(self._due_at):
            if lane not in self.store["lanes"]:
                self._due_at.pop(lane, None)
                self._next_index.pop(lane, None)
                self._chat_since.pop(lane, None)

    def _lane_messages(self, lane: str) -> List[dict]:
        return [m for m in self.store["messages"] if m["lane"] == lane and m["enabled"]]

    def _pick(self, lane: str) -> Optional[dict]:
        msgs = self._lane_messages(lane)
        if not msgs:
            return None
        idx = self._next_index.get(lane, 0) % len(msgs)
        self._next_index[lane] = idx + 1
        return msgs[idx]

    async def step(self, now: Optional[float] = None, live: Optional[bool] = None) -> List[dict]:
        """One pass: post every lane that is due and allowed. Returns the
        messages posted (usually 0 or 1: the global gap blocks the rest).
        `now` / `live` are injectable for tests."""
        now = self._clock() if now is None else now
        self._arm_all(now)
        posted: List[dict] = []
        if not self._enabled():
            return posted
        live_ok = (not self.store["settings"]["live_only"]
                   or (self._is_live() if live is None else bool(live)))
        gap = self.store["settings"]["global_gap_sec"]
        # earliest-due lane first so a starved lane is served before a
        # lane that just came due
        for lane in sorted(self.store["lanes"], key=lambda l: self._due_at.get(l, now)):
            if now < self._due_at.get(lane, now):
                continue
            if not self._lane_messages(lane):
                self._arm(lane, now)          # empty lane: check again next interval
                continue
            if not live_ok:
                self._arm(lane, now)          # offline: re-arm, never backlog
                continue
            need = self.store["lanes"][lane]["min_chat_messages"]
            if need and self._chat_since.get(lane, 0) < need:
                continue                      # stay due, wait for chat
            if self._last_post_at is not None and now - self._last_post_at < gap:
                continue                      # stay due, wait for the gap
            msg = self._pick(lane)
            if msg is None:
                continue
            await self._post(msg, lane, now)
            posted.append(msg)
        return posted

    async def _post(self, msg: dict, lane: str, now: float):
        await self.bot.send_chat(msg["text"])
        self._last_post_at = now
        self._arm(lane, now)
        self._chat_since[lane] = 0
        self.stats["posted"] += 1
        self.stats["last_text"] = msg["text"]
        self.stats["last_lane"] = lane
        self.stats["last_post_wall"] = time.time()

    # ── management API (called by the /mod webserver) ─────────────────

    def snapshot(self) -> dict:
        """Everything the page shows: config + live runtime status."""
        now = self._clock()
        self._arm_all(now)
        lanes = {}
        for name, lane in self.store["lanes"].items():
            msgs = self._lane_messages(name)
            lanes[name] = dict(lane,
                               enabled_count=len(msgs),
                               total_count=len([m for m in self.store["messages"] if m["lane"] == name]),
                               due_in_sec=max(0, int(self._due_at.get(name, now) - now)),
                               next_id=(msgs[self._next_index.get(name, 0) % len(msgs)]["id"]
                                        if msgs else None),
                               chat_since=self._chat_since.get(name, 0))
        return {
            "settings": dict(self.store["settings"]),
            "lanes": lanes,
            "messages": [dict(m) for m in self.store["messages"]],
            "status": {
                "feature_on": self._enabled(),
                "running": self._task is not None and not self._task.done(),
                "live": self._is_live(),
                "posted": self.stats["posted"],
                "last_text": self.stats["last_text"],
                "last_lane": self.stats["last_lane"],
                "last_post_ago_sec": (None if self._last_post_at is None
                                      else max(0, int(now - self._last_post_at))),
                "gap_remaining_sec": (0 if self._last_post_at is None else max(
                    0, int(self.store["settings"]["global_gap_sec"] - (now - self._last_post_at)))),
            },
        }

    def set_settings(self, live_only=None, global_gap_sec=None):
        """Returns (ok, error_or_settings)."""
        s = self.store["settings"]
        if live_only is not None:
            if not isinstance(live_only, bool):
                return False, "live_only must be true or false."
            s["live_only"] = live_only
        if global_gap_sec is not None:
            if (isinstance(global_gap_sec, bool) or not isinstance(global_gap_sec, (int, float))
                    or not (0 <= global_gap_sec <= MAX_GAP_SEC)):
                return False, f"Gap must be 0-{MAX_GAP_SEC} seconds."
            s["global_gap_sec"] = int(global_gap_sec)
        self._save()
        return True, dict(s)

    def set_lane(self, name, interval_min=None, min_chat_messages=None):
        """Create or update a lane. Returns (ok, error_or_action)."""
        name = str(name or "").strip().lower()
        if not LANE_RE.match(name):
            return False, "Lane name must be 1-24 chars: a-z 0-9 _ -"
        is_new = name not in self.store["lanes"]
        if is_new and len(self.store["lanes"]) >= MAX_LANES:
            return False, f"Limit of {MAX_LANES} lanes reached."
        lane = dict(self.store["lanes"].get(name) or
                    {"interval_min": int(_c("TIMED_MESSAGES_DEFAULT_INTERVAL_MIN", 10)),
                     "min_chat_messages": 0})
        if interval_min is not None:
            if (isinstance(interval_min, bool) or not isinstance(interval_min, (int, float))
                    or not (MIN_INTERVAL_MIN <= interval_min <= MAX_INTERVAL_MIN)):
                return False, f"Interval must be {MIN_INTERVAL_MIN}-{MAX_INTERVAL_MIN} minutes."
            lane["interval_min"] = int(interval_min)
        if min_chat_messages is not None:
            if (isinstance(min_chat_messages, bool) or not isinstance(min_chat_messages, (int, float))
                    or not (0 <= min_chat_messages <= MAX_MIN_CHAT)):
                return False, f"Min chat messages must be 0-{MAX_MIN_CHAT}."
            lane["min_chat_messages"] = int(min_chat_messages)
        old = self.store["lanes"].get(name)
        self.store["lanes"][name] = lane
        now = self._clock()
        if is_new:
            self._arm(name, now)
            self._next_index[name] = 0
            self._chat_since[name] = 0
        elif old and old["interval_min"] != lane["interval_min"]:
            # a changed interval takes effect from now, not after the old one
            self._due_at[name] = min(self._due_at.get(name, now + lane["interval_min"] * 60),
                                     now + lane["interval_min"] * 60)
        self._save()
        return True, "added" if is_new else "updated"

    def delete_lane(self, name):
        """Returns (ok, error_or_action). A lane with messages cannot go."""
        name = str(name or "").strip().lower()
        if name not in self.store["lanes"]:
            return False, "No such lane."
        if any(m["lane"] == name for m in self.store["messages"]):
            return False, "Move or delete its messages first."
        if len(self.store["lanes"]) == 1:
            return False, "Keep at least one lane."
        del self.store["lanes"][name]
        self._arm_all(self._clock())
        self._save()
        return True, "deleted"

    def add_or_update_message(self, text, lane, enabled=True, created_by="", msg_id=None):
        """Upsert a message. Returns (ok, error_or_message)."""
        text = str(text or "").strip()
        lane = str(lane or "").strip().lower()
        if not text:
            return False, "Message text is required."
        if len(text) > MAX_TEXT:
            return False, f"Message too long (max {MAX_TEXT} chars)."
        if lane not in self.store["lanes"]:
            return False, "No such lane. Add it first."
        if not isinstance(enabled, bool):
            return False, "enabled must be true or false."
        now = _now_iso()
        existing = self._find(msg_id) if msg_id else None
        if msg_id and existing is None:
            return False, "No such message."
        if existing is None:
            if len(self.store["messages"]) >= MAX_MESSAGES:
                return False, f"Limit of {MAX_MESSAGES} messages reached."
            msg = {"id": secrets.token_hex(4), "text": text, "lane": lane,
                   "enabled": enabled, "created_by": str(created_by or ""),
                   "created_at": now, "updated_at": now}
            self.store["messages"].append(msg)
        else:
            existing.update(text=text, lane=lane, enabled=enabled, updated_at=now)
            msg = existing
        self._save()
        return True, dict(msg)

    def delete_message(self, msg_id):
        """Returns (ok, error_or_action)."""
        msg = self._find(msg_id)
        if msg is None:
            return False, "No such message."
        self.store["messages"].remove(msg)
        self._save()
        return True, "deleted"

    def reorder(self, ids):
        """Set list order (= rotation order). `ids` must be exactly the
        current ids, permuted. Returns (ok, error_or_action)."""
        if not isinstance(ids, list) or sorted(map(str, ids)) != sorted(m["id"] for m in self.store["messages"]):
            return False, "ids must list every message exactly once."
        by_id = {m["id"]: m for m in self.store["messages"]}
        self.store["messages"] = [by_id[str(i)] for i in ids]
        self._save()
        return True, "reordered"

    async def post_now(self, msg_id):
        """Say one message immediately (the page's Post now button).
        Counts as a rotated post for the global gap; does not touch the
        lane's timer or its place in the rotation."""
        msg = self._find(msg_id)
        if msg is None:
            return False, "No such message."
        await self.bot.send_chat(msg["text"])
        self._last_post_at = self._clock()
        self.stats["posted"] += 1
        self.stats["last_text"] = msg["text"]
        self.stats["last_lane"] = msg["lane"]
        self.stats["last_post_wall"] = time.time()
        return True, "posted"

    def _find(self, msg_id) -> Optional[dict]:
        msg_id = str(msg_id or "").strip()
        for m in self.store["messages"]:
            if m["id"] == msg_id:
                return m
        return None
