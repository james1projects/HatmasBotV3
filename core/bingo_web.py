"""
core/bingo_web.py — Stream Bingo routes for the public site.

    GET  /bingo                the page (Twitch login for a card)
    GET  /api/bingo/round      public round state: cards, pot, calls, leaders
    GET  /api/bingo/me         the session viewer's cards + next card price
    POST /api/bingo/card       claim the free card / buy the next one (Hats)
    POST /api/bingo/claim      {card_id}: the Bingo! button (server re-checks the line)
    POST /api/bingo/prefs      {on_stream}: "Show my card on stream"
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
        r.add_post("/api/bingo/claim", self.handle_claim)
        r.add_post("/api/bingo/prefs", self.handle_prefs)
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

    async def _viewer(self, request: web.Request):
        """Twitch identity + origin/rate checks for the POSTs, or an error response."""
        if not self._enabled():
            raise web.HTTPNotFound()
        ident = self._identity(request)
        if ident is None:
            return None, web.json_response({"ok": False, "error": "Log in with Twitch first."}, status=401)
        if _ws.provider(ident) != "tw" or not ident.get("login"):
            return None, web.json_response({"ok": False, "error": "Bingo needs a Twitch login."}, status=403)
        origin_ok = getattr(self.server, "_origin_ok", None)
        if callable(origin_ok) and not origin_ok(request):
            return None, web.json_response({"ok": False, "error": "Bad origin."}, status=403)
        rate_ok = getattr(self.server, "_ip_rate_ok", None)
        if callable(rate_ok) and not rate_ok(request):
            return None, web.json_response({"ok": False, "error": "Too many requests."}, status=429)
        plugin = self._plugin()
        if plugin is None or plugin.store is None:
            return None, web.json_response({"ok": False, "error": "Bingo is starting up."}, status=503)
        return (ident, plugin), None

    async def handle_claim(self, request: web.Request):
        """POST {card_id}: the Bingo! button. Server-side line check."""
        ok, err = await self._viewer(request)
        if err is not None:
            return err
        ident, plugin = ok
        try:
            body = await request.json()
        except Exception:
            body = {}
        res = await plugin.claim_bingo(ident["login"], (body or {}).get("card_id"))
        return web.json_response(res, status=200 if res.get("ok") else 400, headers={"Cache-Control": "no-store"})

    async def handle_prefs(self, request: web.Request):
        """POST {on_stream: bool}: "Show my card on stream"."""
        ok, err = await self._viewer(request)
        if err is not None:
            return err
        ident, plugin = ok
        try:
            body = await request.json()
        except Exception:
            body = {}
        res = await plugin.set_on_stream(ident["login"], bool((body or {}).get("on_stream")))
        return web.json_response(res, status=200 if res.get("ok") else 400, headers={"Cache-Control": "no-store"})

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


class BingoControl:
    """Control routes for the dashboard (localhost:8069) and the dev
    server. Not for the public site: no auth beyond being local.

        GET|POST /api/bingo/status              plugin.status(): round, squares, history
        GET|POST /api/bingo/start | /end        open / close (no winner) a round
        GET|POST /api/bingo/fire?event=<id>     call a square (deck: bingo_call.bat <id>)
        GET|POST /api/bingo/uncall?event=<id>   undo a call in the open round
        GET|POST /api/bingo/simulate?event=<k>  fake detector/economy event (&god=<name>)
        GET      /api/bingo/history?limit=20    past rounds, newest first
        GET      /api/bingo/pool                the square pool
        POST     /api/bingo/pool/save           {id?, label, source, weight} add or update
        POST     /api/bingo/pool/delete?id=<id> remove a square (round must be closed)
        POST     /api/bingo/pool/reload         re-read pool.json after a hand edit
        GET      /api/bingo/cards_on_stream     the carousel's data (opted-in cards)
        GET      /overlay/bingo_cards           the carousel OBS source

    `get_plugin` returns the BingoPlugin (or None while the bot is starting).
    """

    def __init__(self, get_plugin):
        self._get_plugin = get_plugin

    def register(self, router) -> None:
        both = (("/api/bingo/status", self.handle_status), ("/api/bingo/start", self.handle_start),
                ("/api/bingo/end", self.handle_end), ("/api/bingo/fire", self.handle_fire),
                ("/api/bingo/uncall", self.handle_uncall), ("/api/bingo/simulate", self.handle_simulate))
        for path, h in both:
            router.add_get(path, h)
            router.add_post(path, h)
        router.add_get("/api/bingo/history", self.handle_history)
        router.add_get("/api/bingo/pool", self.handle_pool)
        router.add_post("/api/bingo/pool/save", self.handle_pool_save)
        router.add_post("/api/bingo/pool/delete", self.handle_pool_delete)
        router.add_post("/api/bingo/pool/reload", self.handle_pool_reload)
        router.add_get("/api/bingo/cards_on_stream", self.handle_cards_on_stream)
        router.add_get("/overlay/bingo_cards", self.handle_cards_overlay)

    def _plugin(self):
        p = self._get_plugin()
        if p is None or getattr(p, "store", None) is None:
            raise web.HTTPNotFound(text=json.dumps({"ok": False, "error": "bingo plugin not loaded"}),
                                   content_type="application/json")
        return p

    @staticmethod
    async def _params(request: web.Request) -> dict:
        """Query string first, then a JSON body (deck bats use the query)."""
        out = dict(request.query)
        if request.method == "POST" and request.can_read_body:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    for k, v in body.items():
                        out.setdefault(k, v)
            except Exception:
                pass
        return out

    @staticmethod
    def _reply(res: dict, ok_status: int = 200):
        return web.json_response(res, status=ok_status if res.get("ok", True) else 400,
                                 headers={"Cache-Control": "no-store"})

    async def handle_status(self, request):
        return self._reply(self._plugin().status())

    async def handle_start(self, request):
        return self._reply(await self._plugin().start_round())

    async def handle_end(self, request):
        out = await self._plugin().end_round("manual")
        return self._reply(out or {"ok": False, "error": "no open round"})

    async def handle_fire(self, request):
        p = self._plugin()
        q = await self._params(request)
        event = str(q.get("event") or "").strip().lower()
        if not event:
            return self._reply({"ok": False, "error": "event is required"})
        return self._reply(await p.fire(event, source=str(q.get("source") or "deck")[:20]))

    async def handle_uncall(self, request):
        p = self._plugin()
        q = await self._params(request)
        event = str(q.get("event") or "").strip().lower()
        if not event:
            return self._reply({"ok": False, "error": "event is required"})
        return self._reply(await p.uncall(event))

    async def handle_simulate(self, request):
        p = self._plugin()
        q = await self._params(request)
        return self._reply(await p.simulate(str(q.get("event") or ""), str(q.get("god") or q.get("value") or "")))

    async def handle_history(self, request):
        p = self._plugin()
        try:
            limit = max(1, min(200, int(request.query.get("limit") or 20)))
        except ValueError:
            limit = 20
        return self._reply({"ok": True, "rounds": p.history(limit)})

    async def handle_pool(self, request):
        p = self._plugin()
        return self._reply({"ok": True, "squares": p.pool, "auto_ids": p.auto_ids(),
                            "file": str(p._pool_path())})

    async def handle_pool_save(self, request):
        p = self._plugin()
        q = await self._params(request)
        return self._reply(p.set_square(str(q.get("id") or ""), str(q.get("label") or ""),
                                        str(q.get("source") or "manual"), q.get("weight", 1)))

    async def handle_pool_delete(self, request):
        p = self._plugin()
        q = await self._params(request)
        return self._reply(p.remove_square(str(q.get("id") or "")))

    async def handle_pool_reload(self, request):
        return self._reply(self._plugin().reload_pool())

    async def handle_cards_on_stream(self, request):
        """What the carousel shows: opted-in cards of the open round."""
        return self._reply(self._plugin().cards_on_stream())

    async def handle_cards_overlay(self, request):
        resp = web.FileResponse(PUBLIC_DIR.parent / "overlays" / "bingo_cards.html")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
