r"""
tools/bingo_devserver.py — play a Stream Bingo round without the bot.

Mounts the real BingoPlugin and core/bingo_web.py routes (public page +
BingoControl, the same control routes the dashboard mounts) on a bare
aiohttp app, plus the admin page, with a
fake economy (every viewer has Hats) and a fake Twitch login, so the
whole loop can be tried from a browser: open a round, grab cards, call
squares, watch the marks land live, hit bingo.

    python tools\bingo_devserver.py [--port 8088] [--as devviewer] [--no-open]

Pages:  http://localhost:8088/bingo          the viewer page (logged in as --as)
        http://localhost:8088/bingo/admin    start / end / call squares
        http://localhost:8088/sources        every OBS source, with Test buttons
        http://localhost:8088/alerts/layout  the alert box layout editor
        http://localhost:8088/overlay/alerts?box=main   the alert box itself
Data:   data\bingo_dev.db + data\bingo\pool.json (the real pool file) +
        data\alerts_dev.json (--alerts-file to point at data\alerts.json)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aiohttp import web  # noqa: E402

from core import config  # noqa: E402
from core.alert_box import AlertBox  # noqa: E402
from core.alert_web import AlertWeb  # noqa: E402
from core.bingo_web import BingoControl, BingoWeb  # noqa: E402
from core.overlay_manager import OverlayManager  # noqa: E402
from plugins.bingo import BingoPlugin  # noqa: E402

PUBLIC = REPO_ROOT / "public"
OVERLAYS = REPO_ROOT / "overlays"
STATIC = ("theme.css", "auth.js", "hat.png", "favicon-32.png", "favicon.ico",
          "apple-touch-icon.png", "og-image.png", "404.html")


class _Economy:
    """Fake MixItUp balances: everyone starts with 1000 Hats."""
    _connected = True

    def __init__(self):
        self.balances = {}
        self.log = []

    async def _get_balance(self, login):
        return self.balances.setdefault(login, 1000)

    async def _adjust_balance(self, login, amount):
        self.balances[login] = self.balances.setdefault(login, 1000) + int(amount)
        self.log.append((login, int(amount)))
        print(f"[dev-economy] {login} {amount:+d} -> {self.balances[login]}")
        return True


class _Bot:
    def __init__(self):
        self.plugins = {"economy": _Economy()}
        self.features = {"bingo": True}
        self.commands = {}

    def is_feature_enabled(self, name):
        return self.features.get(name, True)

    def register_command(self, name, handler, mod_only=False, **kw):
        self.commands[name] = handler

    async def send_chat(self, text):
        print(f"[dev-chat] {text}")

    async def send_reply(self, message, text, whisper=False):
        print(f"[dev-chat] {text}")


class _Server:
    """Just enough of PublicWebServer for BingoWeb."""

    def __init__(self, bot, login):
        self.app = web.Application()
        self.bot = bot
        self.login = login

    def _feature_on(self, name):
        return True

    def _session_identity(self, request):
        if not self.login:
            return None
        return {"uid": "0", "login": self.login, "name": self.login.title(), "prov": "tw"}

    async def _ident_user_uuid(self, ident):
        # no users table in the dev harness: a stable fake uuid per login
        return ("dev-" + ident["login"]) if ident and ident.get("login") else None

    def _origin_ok(self, request):
        return True

    def _ip_rate_ok(self, request):
        return True


def _static(path: Path):
    async def handler(request):
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
    return handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--as", dest="login", default="devviewer", help="fake Twitch login ('' = logged out)")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--alerts-file", default=str(config.DATA_DIR / "alerts_dev.json"),
                    help="alert box config (default: data/alerts_dev.json, not the bot's data/alerts.json)")
    args = ap.parse_args()

    config.BINGO_DB = config.DATA_DIR / "bingo_dev.db"
    bot = _Bot()
    overlay = OverlayManager(None)                      # the real rules engine + /ws/overlays
    alert_box = AlertBox(overlay, args.alerts_file)     # the real alert box on its own config file
    plugin = BingoPlugin(overlay_manager=overlay)
    plugin.setup(bot)
    bot.plugins["bingo"] = plugin

    server = _Server(bot, args.login)
    BingoWeb(server).register()
    for name in STATIC:
        server.app.router.add_get(f"/{name}", _static(PUBLIC / name))
    server.app.router.add_get("/bingo/admin", _static(OVERLAYS / "bingo_admin.html"))
    AlertWeb(lambda: alert_box, lambda: overlay, base=f"http://localhost:{args.port}").register(server.app.router)

    async def overlay_ws(request):
        """The dashboard's /ws/overlays, minus the youtube action plumbing."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        name = request.query.get("name", "")
        if not name:
            await ws.close()
            return ws
        overlay.register_ws(name, ws)
        if name.startswith("alerts:"):
            await alert_box.on_connect(name.split(":", 1)[1])
        elif overlay._visible.get(name):
            await overlay._send(name, "show", overlay._last_show_data.get(name, {}))
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            overlay.unregister_ws(name, ws)
        return ws

    server.app.router.add_get("/ws/overlays", overlay_ws)
    server.app.router.add_static("/overlays/", OVERLAYS)          # bingo.html, alerts/*.js, theme, client
    if (REPO_ROOT / "assets" / "sounds").is_dir():
        server.app.router.add_static("/assets/sounds/", REPO_ROOT / "assets" / "sounds")

    async def me(request):
        return web.json_response({"logged_in": bool(args.login), "login": args.login,
                                  "display_name": args.login.title(), "login_available": True})

    async def stream(request):
        return web.json_response({"is_live": False})

    server.app.router.add_get("/api/me", me)
    server.app.router.add_get("/api/stream-status", stream)
    BingoControl(lambda: plugin).register(server.app.router)   # the dashboard's /api/bingo/* routes

    async def on_startup(app):
        await plugin.on_ready()

    server.app.on_startup.append(on_startup)
    url = f"http://localhost:{args.port}/bingo"
    print(f"[bingo-dev] {url}  admin: {url}/admin  sources: http://localhost:{args.port}/sources  "
          f"layout: http://localhost:{args.port}/alerts/layout  logged in as: {args.login or '(nobody)'}")
    if not args.no_open:
        webbrowser.open(url)
    web.run_app(server.app, host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
