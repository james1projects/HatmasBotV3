r"""
tools/mod_devserver.py — open hatmaster.tv/mod without the bot.

Mounts the real PublicWebServer with a fake bot (an empty command
catalog, a fake stream status, send_chat that prints) and the real
TimedMessagesPlugin on a scratch store, and hands out a mod session
cookie at /dev/login, so the /mod page and every /api/mod/* route can be
tried from a browser.

    python tools\mod_devserver.py [--port 8082] [--as devmod] [--live] [--no-open]

Pages:  http://localhost:8082/dev/login   sets the cookie, lands on /mod
        http://localhost:8082/dev/sent    everything the fake bot "said"
Data:   data\timed_messages_dev.json (--store to point elsewhere)
"""

from __future__ import annotations

import argparse
import sys
import types
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aiohttp import web  # noqa: E402

from core import config  # noqa: E402
from core import web_session as ws  # noqa: E402
import core.public_webserver as pw  # noqa: E402
from plugins.timed_messages import TimedMessagesPlugin  # noqa: E402

SECRET = "mod-devserver-secret-0123456789abcdef0123456789"


class FakeBot:
    def __init__(self, live: bool):
        self.sent: list = []
        self.features = dict(config.DEFAULT_FEATURES)
        self.plugins = {
            "stream_status": types.SimpleNamespace(
                get_status=lambda: {"is_live": live}),
        }
        self.command_count = 0
        self._raw_handlers = []
        self.bot_username = "devbot"

    async def send_chat(self, text):
        print(f"[FakeBot] CHAT: {text}")
        self.sent.append(text)

    def is_feature_enabled(self, name):
        return self.features.get(name, False)

    def register_raw_handler(self, h):
        self._raw_handlers.append(h)

    def get_command_catalog(self):
        return []

    def get_uptime(self):
        return "dev"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--as", dest="login", default="devmod")
    ap.add_argument("--live", action="store_true", help="fake stream status = live")
    ap.add_argument("--store", default=str(config.DATA_DIR / "timed_messages_dev.json"))
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    origin = f"http://localhost:{args.port}"
    pw.WEB_SESSION_SECRET = SECRET
    pw.WEB_OAUTH_REDIRECT_URI = f"{origin}/auth/twitch/callback"
    pw.TWITCH_CLIENT_ID = "dev_client_id"
    pw.TWITCH_CLIENT_SECRET = "dev_client_secret"
    config.MODERATORS = [args.login]

    bot = FakeBot(live=args.live)
    timed = TimedMessagesPlugin(store_file=Path(args.store))
    timed.setup(bot)
    bot.plugins["timed_messages"] = timed
    server = pw.PublicWebServer(bot=bot)
    app = server.app

    async def dev_login(request):
        token = ws.issue("1", args.login, args.login.title(), secret=SECRET,
                         user_uuid="dev-" + args.login)
        resp = web.HTTPFound("/mod")
        resp.set_cookie(ws.SESSION_COOKIE, token, httponly=True, samesite="Strict", path="/")
        return resp

    async def dev_sent(request):
        return web.json_response({"sent": bot.sent})

    async def dev_live(request):
        live = request.query.get("on", "1") == "1"
        bot.plugins["stream_status"] = types.SimpleNamespace(get_status=lambda: {"is_live": live})
        return web.json_response({"live": live})

    app.router.add_get("/dev/login", dev_login)
    app.router.add_get("/dev/sent", dev_sent)
    app.router.add_get("/dev/live", dev_live)

    async def on_startup(app):
        await timed.on_ready()

    async def on_cleanup(app):
        await timed.cleanup()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    print(f"[mod-dev] {origin}/dev/login  (mod = {args.login}, live = {args.live}, "
          f"store = {args.store})")
    if not args.no_open:
        webbrowser.open(f"{origin}/dev/login")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
