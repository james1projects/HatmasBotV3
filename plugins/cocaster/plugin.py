"""
plugins/cocaster/plugin.py — the co-caster, stage 1.

Two channels, both OFF until the "cocaster" feature toggle is on:

  EAR (private): every COCASTER_EAR_INTERVAL_S, if chat has said at least
  COCASTER_EAR_MIN_MSGS new things, one spoken sentence summarizing them
  is played on COCASTER_EAR_DEVICE (the headphones, never the stream
  mix). Built for the problem of missing chat mid-teamfight.

  LINES (on-stream, text first): on a multikill or death from the kill
  detector, one persona line is written to data/cocaster/lines.jsonl and
  emitted as the "cocaster_line" overlay event. It is only SPOKEN on the
  stream device when COCASTER_STREAM_VOICE is true; the default keeps
  the caster mute on stream until James has tuned the persona.

Always on, toggle or not: every chat message is appended to the local
chat log (plugins/cocaster/chatlog.py). That is the bot's first
telemetry and it never leaves data/.

Privacy: only chat text + match state go to the LLM backend (Claude API
by default, or local Ollama). Transcripts, recordings, and the VOD index
are never sent anywhere.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import config

from .chatlog import ChatLog
from .summarizer import (ClaudeBackend, OllamaBackend, RateLimiter, build_ear_prompt,
                         build_line_prompt, clean_spoken)
from .voice import Voice

DEFAULT_PERSONA = ("You are the Hatmas co-caster: dry, quick, never mean. "
                   "One thought per line. Never invent facts.")


def _cfg(name: str, default: Any) -> Any:
    return getattr(config, name, default)


class CoCasterPlugin:
    def __init__(self, stream_status=None, web_server=None, overlay_manager=None):
        self.bot = None
        self.stream_status = stream_status
        self.web_server = web_server
        self.overlay_manager = overlay_manager
        self.chatlog: Optional[ChatLog] = None
        self.backend = None
        self.ear_voice: Optional[Voice] = None
        self.stream_voice: Optional[Voice] = None
        self.persona = DEFAULT_PERSONA
        self.muted = False                    # runtime !cocaster off
        self._task: Optional[asyncio.Task] = None
        self._last_ear_ts = time.time()       # chat newer than this is "unread"
        self._last_ear_text = ""
        self._line_limiter = RateLimiter(float(_cfg("COCASTER_LINE_COOLDOWN_S", 45)))
        self._death_count = 0
        self._lines_path = Path(_cfg("COCASTER_DIR", config.DATA_DIR / "cocaster")) / "lines.jsonl"
        self._state_path = Path(_cfg("COCASTER_DIR", config.DATA_DIR / "cocaster")) / "state.json"
        self.stats = {"ear_summaries": 0, "lines": 0, "errors": 0, "last_error": ""}

    # ── lifecycle ─────────────────────────────────────────────────────

    def setup(self, bot):
        self.bot = bot
        bot.register_raw_handler(self._on_chat)
        bot.register_command("cocaster", self.cmd_cocaster, mod_only=True)

    async def on_ready(self):
        try:
            self.chatlog = ChatLog(_cfg("CHAT_LOG_DB", config.DATA_DIR / "chat_log.db"))
            # account merges re-point this file too (core/users.py)
            from core import users as _users
            _users.register_merge_hook(self._on_user_merge)
            asyncio.create_task(self._backfill_chatlog_uuids())
        except Exception as e:
            print(f"[CoCaster] chat log unavailable: {e}")
        self.persona = self._load_persona()
        self.backend = self._make_backend()
        work = Path(_cfg("COCASTER_DIR", config.DATA_DIR / "cocaster"))
        work.mkdir(parents=True, exist_ok=True)
        self.ear_voice = Voice(_cfg("COCASTER_EAR_DEVICE", "Headphones"),
                               _cfg("COCASTER_EAR_VOICE", ""), int(_cfg("COCASTER_EAR_RATE", 1)),
                               work_dir=work, label="ear")
        self.stream_voice = Voice(_cfg("COCASTER_STREAM_DEVICE", "SFX"),
                                  _cfg("COCASTER_STREAM_VOICE_NAME", ""),
                                  int(_cfg("COCASTER_STREAM_RATE", 0)), work_dir=work,
                                  enabled=bool(_cfg("COCASTER_STREAM_VOICE", False)), label="stream")
        self._task = asyncio.create_task(self._ear_loop(), name="cocaster-ear")
        self._task.add_done_callback(self._task_done)
        backend = getattr(self.backend, "name", "none")
        print(f"[CoCaster] ready: backend={backend}, ear device '{_cfg('COCASTER_EAR_DEVICE', 'Headphones')}',"
              f" stream voice {'ON' if self.stream_voice.enabled else 'off (text only)'},"
              f" toggle {'ON' if self._enabled() else 'off'}")

    async def cleanup(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.chatlog:
            self.chatlog.close()

    def attach_detector(self, kd) -> None:
        """Wire kill-detector listeners (main.py calls this)."""
        kd.add_multikill_listener(self._on_multikill)
        kd.add_death_listener(self._on_death)

    def _task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            print(f"[CoCaster] ear loop died: {exc!r}")

    # ── config helpers ────────────────────────────────────────────────

    def _enabled(self) -> bool:
        if self.bot is None or not hasattr(self.bot, "is_feature_enabled"):
            return False
        return bool(self.bot.is_feature_enabled("cocaster")) and not self.muted

    def _load_persona(self) -> str:
        path = Path(_cfg("COCASTER_PERSONA_FILE", Path(__file__).with_name("persona.md")))
        try:
            text = path.read_text(encoding="utf-8").strip()
            return text or DEFAULT_PERSONA
        except OSError:
            return DEFAULT_PERSONA

    def _make_backend(self):
        kind = str(_cfg("COCASTER_LLM_BACKEND", "claude")).lower()
        try:
            if kind == "ollama":
                return OllamaBackend(_cfg("COCASTER_OLLAMA_HOST", "http://localhost:11434"),
                                     _cfg("COCASTER_OLLAMA_MODEL", "qwen3.6:27b"))
            key = _cfg("CLAUDE_API_KEY", "")
            if not key or key.startswith("YOUR_"):
                print("[CoCaster] no CLAUDE_API_KEY; summaries disabled until one is set")
                return None
            return ClaudeBackend(key, _cfg("COCASTER_MODEL", "claude-opus-5"),
                                 _cfg("COCASTER_EFFORT", "low"))
        except Exception as e:
            print(f"[CoCaster] backend init failed: {e}")
            return None

    # ── context ───────────────────────────────────────────────────────

    def context(self) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {}
        try:
            if self.stream_status is not None:
                st = self.stream_status.get_status() or {}
                ctx["is_live"] = bool(st.get("is_live"))
                if st.get("viewer_count") is not None:
                    ctx["viewers"] = st.get("viewer_count")
        except Exception:
            pass
        plugins = getattr(self.bot, "plugins", {}) if self.bot else {}
        eco = plugins.get("economy")
        if eco is not None:
            god = getattr(eco, "_match_god", None)
            if god:
                ctx["god"] = god
                ctx["kda"] = list(getattr(eco, "_match_kda", None) or [0, 0, 0])
        smite = plugins.get("smite")
        started = getattr(smite, "match_start_time", None) if smite else None
        if started:
            ctx["match_minutes"] = (time.time() - started) / 60.0
        godreq = plugins.get("godrequest")
        queue = getattr(godreq, "queue", None) if godreq else None
        if isinstance(queue, list):
            ctx["queue_len"] = len(queue)
        return ctx

    async def _on_user_merge(self, absorbed: str, survivor: str) -> int:
        return self.chatlog.repoint_user(absorbed, survivor) if self.chatlog else 0

    async def _backfill_chatlog_uuids(self) -> None:
        """Rows logged before the uuid era get their user_uuid from the
        login (placeholder identity until the person is seen with an id)."""
        try:
            from core import db as _shared_db
            from core import users as _users
            db = await _shared_db.get_db()
            if db is None or not self.chatlog:
                return
            n = 0
            for login in self.chatlog.logins_without_uuid():
                uid = await _users.get_or_create_twitch_login(db, login, commit=False)
                n += self.chatlog.backfill_uuid(login, uid)
            await db.commit()
            if n:
                print(f"[CoCaster] chat log: backfilled user_uuid on {n} rows")
        except Exception as e:
            self._error(f"chat log uuid backfill: {e}")

    # ── chat ──────────────────────────────────────────────────────────

    async def _on_chat(self, payload) -> None:
        try:
            chatter = getattr(payload, "chatter", None)
            user = (getattr(chatter, "name", None) or "").lower()
            display = getattr(chatter, "display_name", None) or user
            text = (getattr(payload, "text", "") or "").strip()
            if not user or not text:
                return
            is_cmd = text.startswith("!")
            is_mod = False
            try:
                is_mod = bool(self.bot.is_mod(chatter)) if self.bot else False
            except Exception:
                pass
            if self.chatlog:
                user_uuid = None
                try:
                    user_uuid = await self.bot.user_uuid_for(chatter) if self.bot else None
                except Exception:
                    pass
                self.chatlog.add(user, display, text, is_command=is_cmd, is_mod=is_mod,
                                 user_uuid=user_uuid)
        except Exception as e:
            self._error(f"chat log: {e}")

    # ── ear channel ───────────────────────────────────────────────────

    async def _ear_loop(self) -> None:
        interval = float(_cfg("COCASTER_EAR_INTERVAL_S", 75))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.ear_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._error(f"ear tick: {e}")

    async def ear_tick(self, force: bool = False) -> Optional[str]:
        """One earpiece cycle. Returns the spoken text, or None when there
        was nothing to say. `force` speaks even below the message floor."""
        if not self._enabled() and not force:
            self._last_ear_ts = time.time()      # don't pile up unread chat while off
            return None
        if not self.chatlog or not self.backend or not self.ear_voice:
            return None
        min_msgs = int(_cfg("COCASTER_EAR_MIN_MSGS", 3))
        msgs = self.chatlog.recent(since_ts=self._last_ear_ts, limit=60)
        if len(msgs) < min_msgs and not force:
            return None
        self._last_ear_ts = time.time()
        if not msgs:
            return None
        built = build_ear_prompt(msgs, self.context(), self._last_ear_text,
                                 max_words=int(_cfg("COCASTER_EAR_MAX_WORDS", 35)))
        raw = await self.backend.complete(built["system"], built["prompt"], max_tokens=160)
        text = clean_spoken(raw, max_words=int(_cfg("COCASTER_EAR_MAX_WORDS", 35)))
        if not text or text.lower().startswith("nothing new"):
            return None
        self._last_ear_text = text
        self.stats["ear_summaries"] += 1
        self._append_line({"channel": "ear", "text": text, "messages": len(msgs)})
        await asyncio.to_thread(self.ear_voice.speak, text)
        return text

    # ── on-stream lines ───────────────────────────────────────────────

    async def _on_multikill(self, kill_type: str) -> None:
        await self.caster_line({"kind": "multikill", "detail": str(kill_type)})

    async def _on_death(self, count: int = 1) -> None:
        self._death_count += int(count or 1)
        # deaths are frequent; only every other one gets a line, and the
        # cooldown still applies
        if self._death_count % 2 == 0:
            await self.caster_line({"kind": "death", "count": self._death_count})

    async def caster_line(self, event: Dict[str, Any], force: bool = False) -> Optional[str]:
        if not (self._enabled() or force) or not self.backend:
            return None
        if not force and not self._line_limiter.allow():
            return None
        recent = self.chatlog.recent(since_ts=time.time() - 45, limit=8) if self.chatlog else []
        built = build_line_prompt(event, self.context(), self.persona,
                                  max_words=int(_cfg("COCASTER_LINE_MAX_WORDS", 22)),
                                  recent_chat=recent)
        try:
            raw = await self.backend.complete(built["system"], built["prompt"], max_tokens=120)
        except Exception as e:
            self._error(f"line: {e}")
            return None
        text = clean_spoken(raw, max_words=int(_cfg("COCASTER_LINE_MAX_WORDS", 22)))
        if not text:
            return None
        self.stats["lines"] += 1
        self._append_line({"channel": "stream", "event": event, "text": text})
        if self.overlay_manager is not None:
            try:
                await self.overlay_manager.emit("cocaster_line", {"text": text, "event": event})
            except Exception:
                pass
        if self.stream_voice is not None and self.stream_voice.enabled:
            await asyncio.to_thread(self.stream_voice.speak, text)
        return text

    # ── chat command ──────────────────────────────────────────────────

    async def cmd_cocaster(self, message, args: str) -> None:
        arg = (args or "").strip()
        sub, _, rest = arg.partition(" ")
        sub = sub.lower()
        if sub == "off":
            self.muted = True
            await self.bot.send_reply(message, "co-caster muted")
        elif sub == "on":
            self.muted = False
            await self.bot.send_reply(message, "co-caster unmuted")
        elif sub == "test":
            ok = await asyncio.to_thread(self.ear_voice.speak, rest or "Earpiece check. Chat is quiet.") \
                if self.ear_voice else False
            await self.bot.send_reply(message, "earpiece test sent" if ok else "earpiece unavailable")
        elif sub == "now":
            text = await self.ear_tick(force=True)
            await self.bot.send_reply(message, f"ear: {text}" if text else "nothing new in chat")
        elif sub == "line":
            text = await self.caster_line({"kind": rest or "test", "detail": rest}, force=True)
            await self.bot.send_reply(message, f"line: {text}" if text else "no line")
        else:
            s = self.status()
            await self.bot.send_reply(
                message, f"co-caster {'on' if s['enabled'] else 'off'} (toggle {'on' if s['toggle'] else 'off'},"
                         f" {'muted' if s['muted'] else 'unmuted'}), backend {s['backend']},"
                         f" {s['ear_summaries']} ear summaries, {s['lines']} lines, chat log {s['chat_messages']} msgs")

    # ── status / bookkeeping ──────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        toggle = bool(self.bot.is_feature_enabled("cocaster")) if self.bot and hasattr(self.bot, "is_feature_enabled") else False
        chat = self.chatlog.stats() if self.chatlog else {}
        return {
            "enabled": self._enabled(), "toggle": toggle, "muted": self.muted,
            "backend": getattr(self.backend, "name", "none"),
            "model": getattr(self.backend, "model", None),
            "ear_device": self.ear_voice.device_substring if self.ear_voice else None,
            "ear_device_index": self.ear_voice.resolve_device() if self.ear_voice else None,
            "stream_voice": bool(self.stream_voice and self.stream_voice.enabled),
            "last_ear": self._last_ear_text,
            "chat_messages": chat.get("messages", 0), "chat_users": chat.get("users", 0),
            **self.stats,
        }

    def _append_line(self, record: Dict[str, Any]) -> None:
        try:
            self._lines_path.parent.mkdir(parents=True, exist_ok=True)
            record = dict(record, ts=time.time())
            with self._lines_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _error(self, msg: str) -> None:
        self.stats["errors"] += 1
        self.stats["last_error"] = msg
        print(f"[CoCaster] {msg}")
