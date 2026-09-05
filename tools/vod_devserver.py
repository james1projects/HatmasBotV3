r"""
tools/vod_devserver.py — run hatmaster.tv/vod against the real index
without starting the bot.

Mounts core/vod_web.py's routes (the exact handlers the public server
uses) on a bare aiohttp app plus the few static files the page needs,
so search, thumbnails, on-demand clips, streaming, and the review page
can be exercised end to end from a browser. Port 8078 (see
.claude/launch.json "vod-dev").

    python tools\vod_devserver.py [--port 8078] [--no-open]
                                  [--db PATH] [--clips DIR]
                                  [--visitor] [--toggle on|off]

--visitor pretends every request comes through the Cloudflare tunnel
(no file paths, no review page, only PUBLIC recordings), and --toggle
sets the web_vod feature toggle for that fake server, so both halves of
the access model can be checked from one machine.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aiohttp import web  # noqa: E402

from core import config  # noqa: E402
from core.vod_web import VodWeb  # noqa: E402

PUBLIC = REPO_ROOT / "public"
STATIC = ("theme.css", "auth.js", "hat.png", "favicon-32.png", "favicon.ico",
          "apple-touch-icon.png", "og-image.png", "404.html")


class _FakeServer:
    """Just enough of PublicWebServer for VodWeb: an app, the feature
    toggle, and the loopback check (forced false in --visitor mode)."""

    def __init__(self, toggle: bool = True, visitor: bool = False):
        self.app = web.Application()
        self.toggle = toggle
        self.visitor = visitor

    def _feature_on(self, name: str) -> bool:
        return self.toggle

    def _is_local_admin(self, request) -> bool:
        if self.visitor:
            return False
        return (request.remote or "") in ("127.0.0.1", "::1")


def _static(name: str):
    async def handler(request):
        path = PUBLIC / name
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
    return handler


async def _stub_me(request):
    # auth.js asks who is logged in for the footer chip; nobody, here.
    return web.json_response({"logged_in": False})


async def _stub_stream(request):
    return web.json_response({"is_live": False})


async def _root(request):
    raise web.HTTPFound("/vod")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8078)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--db", type=Path, default=Path(config.VOD_DB_PATH))
    ap.add_argument("--clips", type=Path, default=Path(config.VOD_CLIPS_DIR))
    ap.add_argument("--visitor", action="store_true",
                    help="behave as if every request came through the tunnel")
    ap.add_argument("--toggle", choices=["on", "off"], default="on",
                    help="state of the web_vod feature toggle for this fake server")
    args = ap.parse_args()

    server = _FakeServer(toggle=(args.toggle == "on"), visitor=args.visitor)
    VodWeb(server, db_path=args.db, clips_dir=args.clips).register()
    for name in STATIC:
        server.app.router.add_get(f"/{name}", _static(name))
    server.app.router.add_get("/api/me", _stub_me)
    server.app.router.add_get("/api/stream-status", _stub_stream)
    server.app.router.add_get("/", _root)

    url = f"http://localhost:{args.port}/vod"
    print(f"[vod-dev] serving {url}  db={args.db}  mode={'VISITOR' if args.visitor else 'local'}"
          f"  toggle={'on' if server.toggle else 'off'}")
    if not args.no_open:
        webbrowser.open(url)
    web.run_app(server.app, host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
