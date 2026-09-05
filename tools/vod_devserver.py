"""
tools/vod_devserver.py — run hatmaster.tv/vod against the real index
without starting the bot.

Mounts core/vod_web.py's routes (the exact handlers the public server
uses) on a bare aiohttp app plus the few static files the page needs,
so search, thumbnails, and on-demand clip rendering can be exercised
end to end from a browser. Port 8078 (see .claude/launch.json "vod-dev").

    python tools\vod_devserver.py [--port 8078] [--no-open]
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

from core.vod_web import VodWeb  # noqa: E402

PUBLIC = REPO_ROOT / "public"
STATIC = ("theme.css", "auth.js", "hat.png", "favicon-32.png", "favicon.ico",
          "apple-touch-icon.png", "og-image.png", "404.html")


class _FakeServer:
    """Just enough of PublicWebServer for VodWeb: an app and the
    feature toggle (always on here)."""

    def __init__(self):
        self.app = web.Application()

    def _feature_on(self, name: str) -> bool:
        return True


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
    args = ap.parse_args()

    server = _FakeServer()
    VodWeb(server).register()
    for name in STATIC:
        server.app.router.add_get(f"/{name}", _static(name))
    server.app.router.add_get("/api/me", _stub_me)
    server.app.router.add_get("/api/stream-status", _stub_stream)
    server.app.router.add_get("/", _root)

    url = f"http://localhost:{args.port}/vod"
    print(f"[vod-dev] serving {url}")
    if not args.no_open:
        webbrowser.open(url)
    web.run_app(server.app, host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
