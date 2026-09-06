"""
core/bingo_web.py — Stream Bingo routes for the public site.

    GET  /bingo                the page (Twitch login for a card)
    GET  /api/bingo/round      public round state: cards, pot, calls, leaders
    GET  /api/bingo/me         the session viewer's cards + next card price
    POST /api/bingo/card       claim the free card / buy the next one (Hats)
    GET  /ws/bingo             live: bingo_open / bingo_call / bingo_win /
                               bingo_closed / card_added, straight from the plugin

Registered by PublicWebServer via `BingoWeb(self).register()`; the plugin
is looked up per request through bot.plugins["bingo"], so the server
never depends on registration order.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional, Set

import aiohttp
from aiohttp import web

from core import web_session as _ws

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"


class BingoWeb:
    def __init__(self, server):
        self.server = server
        self._clients: Set[web.WebSocketResponse] = set()
        self._send_lock = asyncio.Lock()
        self._hooked = False

    def register(self) -> None:
        r = self.server.app.router
        r.add_get("/bingo", self.handle_page)
        r.add_get("/api/bingo/round", self.handle_round)
        r.add_get("/api/bingo/me", self.handle_me)
        r.add_post("/api/bingo/card", self.handle_card)
        r.add_get("/ws/bingo", self.handle_ws)

    # ── plumbing ──────────────────────────────────────────────────────

    def _plugin(self):
        bot = getattr(self.server, "bot", None)
        plugin = (getattr(bot, "plugins", {}) or {}).get("bingo") if bot else None
        if plugin is not None and not self._hooked:
            plugin.add_listener(self._on_plugin_event)
            self._hooked = True
        return plugin

    def _enabled(self) -> bool:
        try:
            return bool(self.server._feature_on("bingo"))
        except Exception:
            return True

    def _identity(self, request: web.Request) -> Optional[dict]:
        fn = getattr(self.server, "_session_identity", None)
        if callable(fn):
            try:
                return fn(request)
            except Exception:
                return None
        return None

    async def _on_plugin_event(self, event: str, data: dict) -> None:
        if not self._clients:
            return
        try:
            msg = json.dumps({"event": event, "data": data})
        except (TypeError, ValueError):
            return
        async with self._send_lock:
            for ws in list(self._clients):
                try:
                    await ws.send_str(msg)
                except Exception:
                    self._clients.discard(ws)

    # ── handlers ──────────────────────────────────────────────────────

    async def handle_page(self, request: web.Request):
        if not self._enabled():
            raise web.HTTPNotFound()
        return web.FileResponse(PUBLIC_DIR / "bingo.html", headers={"Cache-Control": "no-cache"})

    async def handle_round(self, request: web.Request):
        if not self._enabled():
            raise web.HTTPNotFound()
        plugin = self._plugin()
        if plugin is None or plugin.store is None:
            return web.json_response({"enabled": True, "open": False, "round": None, "last": None,
                                      "prices": [], "max_cards": 0, "loading": True})
        return web.json_response(plugin.public_state(), headers={"Cache-Control": "no-cache"})

    async def handle_me(self, request: web.Request):
        if not self._enabled():
            raise web.HTTPNotFound()
        ident = self._identity(request)
        plugin = self._plugin()
        if ident is None or plugin is None or plugin.store is None:
            return web.json_response({"logged_in": ident is not None, "twitch": False,
                                      "round": None, "cards": [], "next_price": None, "can_claim": False},
                                     headers={"Cache-Control": "no-store"})
        twitch = _ws.provider(ident) == "tw" and bool(ident.get("login"))
        out = plugin.my_cards(ident.get("login") or "") if twitch else \
            {"round": None, "cards": [], "next_price": None, "can_claim": False}
        out.update({"logged_in": True, "twitch": twitch, "login": ident.get("login"),
                    "display": ident.get("name") or ident.get("login")})
        if not twitch:
            out["error"] = "Bingo cards need a Twitch login."
        return web.json_response(out, headers={"Cache-Control": "no-store"})

    async def handle_card(self, request: web.Request):
        if not self._enabled():
            raise web.HTTPNotFound()
        ident = self._identity(request)
        if ident is None:
            return web.json_response({"ok": False, "error": "Log in with Twitch first."}, status=401)
        if _ws.provider(ident) != "tw" or not ident.get("login"):
            return web.json_response({"ok": False, "error": "Bingo cards need a Twitch login."}, status=403)
        origin_ok = getattr(self.server, "_origin_ok", None)
        if callable(origin_ok) and not origin_ok(request):
            return web.json_response({"ok": False, "error": "Bad origin."}, status=403)
        rate_ok = getattr(self.server, "_ip_rate_ok", None)
        if callable(rate_ok) and not rate_ok(request):
            return web.json_response({"ok": False, "error": "Too many requests."}, status=429)
        plugin = self._plugin()
        if plugin is None or plugin.store is None:
            return web.json_response({"ok": False, "error": "Bingo is starting up."}, status=503)
        res = await plugin.claim_card(ident["login"], ident.get("name") or ident["login"])
        return web.json_response(res, status=200 if res.get("ok") else 400,
                                 headers={"Cache-Control": "no-store"})

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._enabled():
            raise web.HTTPNotFound()
        self._plugin()   # ensure the listener hook exists before the first event
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT and msg.data == "ping":
                    await ws.send_str("pong")
        finally:
            self._clients.discard(ws)
        return ws
