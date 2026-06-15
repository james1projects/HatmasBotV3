"""
FactorioPlugin
==============
Bot-side half of the Factorio integration. Pairs with the
factorio_mod/hatmas-events/ game mod.

Bridge:
  bot -> game   RCON (/silent-command remote.call("hatmas", ...)) —
                requires Factorio hosting a multiplayer save with
                --rcon-port/--rcon-password.
  game -> bot   OutboxTailer on <script-output>/hatmas/events.jsonl;
                events are formatted and announced in Twitch chat.

Cards: subscribes to the Streamloots hub (wired in main.py via
streamloots.add_redemption_listener). Card name -> action mappings
live in data/factorio_cards.json (CardStore), managed live from the
card manager page at /factorio/cards on the bot webserver — NOT in
code. FACTORIO_CARD_MAP in config only seeds the store on first run.

Successful card plays stay silent in chat (the mod's outbox
announcement covers them); failures and cooldowns are reported so the
viewer isn't left guessing. No chat commands — the entire control
surface is the card manager page + its test buttons.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import (
    BASE_DIR,
    FACTORIO_RCON_HOST, FACTORIO_RCON_PORT, FACTORIO_RCON_PASSWORD,
    FACTORIO_SCRIPT_OUTPUT, FACTORIO_CARD_MAP, FACTORIO_ANNOUNCE_EVENTS,
)
from plugins.factorio.rcon import RconClient, RconAuthError
from plugins.factorio.catalog import (
    ACTION_INFO, build_command, remote_call, format_event,
)
from plugins.factorio.cards import CardStore
from plugins.factorio.events import OutboxTailer

UNREACHABLE_CHAT_COOLDOWN = 60  # seconds between "not reachable" messages
CARD_STORE_PATH = Path(BASE_DIR) / "data" / "factorio_cards.json"
CARDS_PAGE_PATH = Path(BASE_DIR) / "overlays" / "factorio_cards.html"


def _default_script_output() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Factorio" / "script-output"
    return None


class FactorioPlugin:
    def __init__(self):
        self.bot = None
        self.rcon: Optional[RconClient] = None
        self.tailer: Optional[OutboxTailer] = None
        self.cards = CardStore(CARD_STORE_PATH, seed=FACTORIO_CARD_MAP)
        self._card_last_fired: Dict[str, float] = {}
        self._last_unreachable_chat = 0.0
        self.cards_handled = 0
        self.commands_sent = 0

    # ── lifecycle ───────────────────────────────────────────────────

    def setup(self, bot):
        self.bot = bot

    async def on_ready(self):
        if not FACTORIO_RCON_PASSWORD:
            print("[Factorio] FACTORIO_RCON_PASSWORD not set in "
                  "config_local.py — RCON disabled (card manager page "
                  "still available at /factorio/cards)")
        else:
            self.rcon = RconClient(FACTORIO_RCON_HOST, FACTORIO_RCON_PORT,
                                   FACTORIO_RCON_PASSWORD)
            # Connection is attempted lazily by command(); probe once
            # at startup purely for the log line. The game is usually
            # not running yet when the bot boots — that's fine.
            try:
                await self.rcon.connect()
                pong = await self.rcon.command(remote_call("ping"))
                print(f"[Factorio] RCON connected ({pong})")
            except RconAuthError:
                print("[Factorio] RCON password rejected — check "
                      "FACTORIO_RCON_PASSWORD vs the game's "
                      "--rcon-password")
            except Exception:
                print(f"[Factorio] RCON not reachable at "
                      f"{FACTORIO_RCON_HOST}:{FACTORIO_RCON_PORT} (game "
                      f"not running yet?) — will retry on demand")

        outbox_dir = (Path(FACTORIO_SCRIPT_OUTPUT)
                      if FACTORIO_SCRIPT_OUTPUT else _default_script_output())
        if outbox_dir is None:
            print("[Factorio] No script-output path (set "
                  "FACTORIO_SCRIPT_OUTPUT) — event announcements disabled")
            return
        self.tailer = OutboxTailer(outbox_dir / "hatmas" / "events.jsonl")
        self.tailer.add_listener(self._on_outbox_event)
        self.tailer.start()
        print(f"[Factorio] Tailing {self.tailer.path}")

    async def cleanup(self):
        if self.tailer:
            await self.tailer.stop()
            self.tailer = None
        if self.rcon:
            await self.rcon.close()
            self.rcon = None

    # ── card manager API (registered in main.py) ────────────────────

    def register_api_routes(self, app):
        from aiohttp import web

        async def page(request):
            return web.FileResponse(
                CARDS_PAGE_PATH,
                headers={"Cache-Control":
                         "no-cache, no-store, must-revalidate"})

        async def get_cards(request):
            return web.json_response(self._manager_state())

        async def post_cards(request):
            try:
                body = await request.json()
            except Exception:
                return web.json_response(
                    {"ok": False, "error": "invalid JSON"}, status=400)
            op = body.get("op")
            if op == "set":
                err = self.cards.set(body.get("card"),
                                     body.get("action"),
                                     body.get("cooldown", 0))
                if err:
                    return web.json_response({"ok": False, "error": err})
                return web.json_response(
                    {"ok": True, "state": self._manager_state()})
            if op == "remove":
                removed = self.cards.remove(body.get("card"))
                return web.json_response(
                    {"ok": removed,
                     "error": "" if removed else "no such card",
                     "state": self._manager_state()})
            if op == "test":
                result = await self._test_card(body.get("card"),
                                               body.get("user", "TestViewer"),
                                               body.get("message", ""))
                return web.json_response(
                    {"ok": True, "result": result,
                     "state": self._manager_state()})
            return web.json_response(
                {"ok": False, "error": f"unknown op {op!r}"}, status=400)

        app.router.add_get("/factorio/cards", page)
        app.router.add_get("/api/factorio/cards", get_cards)
        app.router.add_post("/api/factorio/cards", post_cards)

    def _manager_state(self) -> Dict[str, Any]:
        sl = (self.bot.plugins.get("streamloots")
              if self.bot and hasattr(self.bot, "plugins") else None)
        mapped = {c["card"].strip().lower() for c in self.cards.all()}
        recent = []
        now = time.time()
        for c in (getattr(sl, "recent_cards", None) or []):
            recent.append({
                "card_name": c["card_name"],
                "username": c.get("username"),
                "seconds_ago": int(now - (c.get("seen_at") or now)),
                "mapped": c["card_name"].strip().lower() in mapped,
            })
        return {
            "mappings": self.cards.all(),
            "actions": ACTION_INFO,
            "recent_cards": recent,
            "status": self.get_status(),
            "streamloots": (sl.get_status()
                            if sl and hasattr(sl, "get_status") else None),
        }

    async def _test_card(self, card_name: str, username: str,
                         message: str) -> str:
        """Fire a mapping exactly like a real redemption would, but
        without cooldown bookkeeping. Returns a plain status string
        for the manager UI."""
        entry = self.cards.get(card_name)
        if entry is None:
            return f"no mapping for card {card_name!r}"
        cmd = build_command(entry["action"], username, message)
        if cmd is None:
            return f"unknown action {entry['action']!r}"
        result = await self._run(cmd)
        if result is None:
            return ("Factorio not reachable (is the game running as a "
                    "multiplayer host with RCON flags?)")
        return result.strip() or "ok"

    # ── streamloots cards (wired in main.py) ────────────────────────

    async def on_streamloots_redemption(self, event: Dict[str, Any]):
        if self.bot and not self.bot.is_feature_enabled("factorio"):
            return
        card_name = event.get("card_name") or ""
        entry = self.cards.get(card_name)
        if entry is None:
            return  # not a Factorio card; other consumers may handle it
        username = event.get("username") or "viewer"
        key = card_name.strip().lower()
        cooldown = float(entry.get("cooldown") or 0)
        now = time.monotonic()
        last = self._card_last_fired.get(key, 0.0)
        if cooldown > 0 and now - last < cooldown:
            remaining = int(cooldown - (now - last)) + 1
            await self._chat(f"Card {card_name} is on cooldown "
                             f"({remaining}s left).")
            return
        message = event.get("long_message") or event.get("message") or ""
        cmd = build_command(entry["action"], username, message)
        if cmd is None:
            print(f"[Factorio] Card {card_name!r} maps to unknown "
                  f"action {entry.get('action')!r}")
            return
        self.cards_handled += 1
        self._card_last_fired[key] = now
        result = await self._run(cmd)
        if result is None:
            self._card_last_fired[key] = last  # don't burn cooldown
            now2 = time.monotonic()
            if now2 - self._last_unreachable_chat > UNREACHABLE_CHAT_COOLDOWN:
                self._last_unreachable_chat = now2
                await self._chat("Factorio is not reachable right now. "
                                 "Card not consumed by the game.")
            return
        if result.strip() != "ok":
            # Mod returned a plain-text error (e.g. "no pet for x") —
            # relay it so the viewer knows what happened.
            await self._chat(f"Factorio: {result.strip()}")

    # ── outbox -> chat ──────────────────────────────────────────────

    async def _on_outbox_event(self, event: Dict[str, Any]):
        if not FACTORIO_ANNOUNCE_EVENTS:
            return
        if self.bot and not self.bot.is_feature_enabled("factorio"):
            return
        line = format_event(event)
        if line:
            await self._chat(line)

    # ── helpers ─────────────────────────────────────────────────────

    async def _run(self, cmd: str) -> Optional[str]:
        """Send an RCON command. Returns the response string, or None
        when Factorio is unreachable / RCON unconfigured."""
        if self.rcon is None:
            return None
        try:
            result = await self.rcon.command(cmd)
            self.commands_sent += 1
            return result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Factorio] RCON command failed: {e}")
            return None

    async def _chat(self, text: str):
        if self.bot is None:
            return
        try:
            await self.bot.send_chat(text)
        except Exception as e:
            print(f"[Factorio] send_chat failed: {e}")

    def get_status(self) -> Dict[str, Any]:
        return {
            "configured": bool(FACTORIO_RCON_PASSWORD),
            "rcon_connected": bool(self.rcon and self.rcon.connected),
            "cards_handled": self.cards_handled,
            "commands_sent": self.commands_sent,
            "outbox_events": self.tailer.events_seen if self.tailer else 0,
            "cards_mapped": len(self.cards.all()),
        }
