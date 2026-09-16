r"""
tools/videos_admin_devserver.py — open /admin/videos without the bot.

Mounts the real PublicWebServer on an in-memory SQLite database seeded
with a handful of fake uploads (some auto-tagged, some paid out, some
not) and a fake YouTubeRewardsPlugin whose "comment scan" just returns a
number, then hands out a broadcaster session cookie at /dev/login. Every
/api/admin/videos/* route runs the real handlers (core/youtube_admin_web.py).

    python tools\videos_admin_devserver.py [--port 8083] [--offline] [--no-open]

Pages:  http://localhost:8083/dev/login   sets the cookie, lands on /admin/videos
        http://localhost:8083/dev/viewer  a non-broadcaster cookie (page must 404)
--offline makes the fake plugin report "not running" (banner + 503s).
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

import aiosqlite  # noqa: E402
from aiohttp import web  # noqa: E402

from core import config  # noqa: E402
from core import web_session as ws  # noqa: E402
from core import youtube_videos as yv  # noqa: E402
from core.youtube_schema import ensure_youtube_schema  # noqa: E402
import core.public_webserver as pw  # noqa: E402
import core.youtube_admin_web as yaw  # noqa: E402

SECRET = "videos-devserver-secret-0123456789abcdef0123456789"
BROADCASTER = "hatmaster"

# (video_id, title, published_at, god|None, set_by, grants)
SEED = [
    ("dQw4w9WgXcQ", "Full Gameplay: Ymir vs Loki", "2026-09-12T18:00:00Z", "Ymir", "auto", 0),
    ("9bZkp7q19f0", "Full Gameplay: Hou Yi vs Athena", "2026-09-10T18:00:00Z", "Hou Yi", "auto", 14),
    ("kJQP7kiw5Fk", "AH MUZEN CAB is completely busted right now", "2026-09-08T18:00:00Z", None, None, 0),
    ("3JZ_D3ELwOQ", "SMITE 2 duel tier list (September)", "2026-09-06T18:00:00Z", None, None, 0),
    ("e-ORhEE9VVg", "Ranked duel grind night 3 (Loki + Sol)", "2026-09-04T18:00:00Z", None, None, 0),
    ("fJ9rUzIMcZQ", "Full Gameplay: Sylvanus vs Hercules", "2026-09-01T18:00:00Z", "Sylvanus", "manual", 3),
    ("L_jWHffIx5E", "Old highlight montage #4", "2026-08-20T18:00:00Z", None, None, 0),
    ("hT_nvWreIhg", "Full Gameplay: Geb vs Cupid", "2026-08-18T18:00:00Z", "Geb", "web:hatmaster", 0),
]


class FakePlugin:
    def __init__(self, ready: bool):
        self.ready = ready
        self.gods = ["Ymir", "Hou Yi", "Loki", "Ah Muzen Cab", "Sylvanus",
                     "Geb", "Sol", "Athena", "Hercules", "Cupid"]
        self.db = None

    def is_ready(self):
        return self.ready

    def known_gods(self):
        return list(self.gods)

    async def refresh_videos(self, limit=None):
        print("[videos-dev] refresh_videos() called")
        new = await yv.upsert_video(self.db, "ZZZnewvid01",
                                    "Full Gameplay: Loki vs Ymir (fresh upload)",
                                    "2026-09-14T12:00:00Z")
        if new:
            await yv.set_god(self.db, "ZZZnewvid01", "Loki", "auto")
        return {"seen": len(SEED) + 1, "added": int(new), "auto_tagged": int(new)}

    async def scan_video(self, video_id):
        god = await yv.get_god(self.db, video_id)
        print(f"[videos-dev] scan_video({video_id}) -> {god}")
        if god is None:
            return 0
        # Pretend two new commenters showed up; record them like the real scan does.
        n = await yv.grant_count(self.db, video_id)
        for i in range(2):
            await self.db.execute(
                "INSERT OR IGNORE INTO youtube_processed_comments "
                "(yt_video_id, yt_channel_id, comment_id) VALUES (?, ?, ?)",
                (video_id, f"UCdev{n + i}", f"c{n + i}"))
        await self.db.commit()
        return 2


async def seed(db):
    await ensure_youtube_schema(db)
    for vid, title, published, god, set_by, grants in SEED:
        await yv.upsert_video(db, vid, title, published)
        if god:
            await yv.set_god(db, vid, god, set_by, title=title)
        for i in range(grants):
            await db.execute(
                "INSERT INTO youtube_processed_comments "
                "(yt_video_id, yt_channel_id, comment_id) VALUES (?, ?, ?)",
                (vid, f"UCseed{i}", f"seed{i}"))
    await yv.set_skipped(db, "L_jWHffIx5E", True)
    await db.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8083)
    ap.add_argument("--offline", action="store_true",
                    help="fake plugin reports not running")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    origin = f"http://localhost:{args.port}"
    pw.WEB_SESSION_SECRET = SECRET
    pw.WEB_OAUTH_REDIRECT_URI = f"{origin}/auth/twitch/callback"
    pw.TWITCH_CLIENT_ID = "dev_client_id"
    pw.TWITCH_CLIENT_SECRET = "dev_client_secret"
    config.TWITCH_CHANNEL = BROADCASTER
    yaw.AUDIT_LOG = config.DATA_DIR / "youtube_videos_audit_dev.log"

    plugin = FakePlugin(ready=not args.offline)
    bot = types.SimpleNamespace(
        plugins={"youtube_rewards": plugin},
        is_feature_enabled=lambda name: True)
    server = pw.PublicWebServer(bot=bot)
    app = server.app

    def _login(login):
        token = ws.issue("1", login, login.title(), secret=SECRET,
                         user_uuid="dev-" + login)
        resp = web.HTTPFound("/admin/videos")
        resp.set_cookie(ws.SESSION_COOKIE, token, httponly=True,
                        samesite="Strict", path="/")
        return resp

    async def dev_login(request):
        return _login(BROADCASTER)

    async def dev_viewer(request):
        return _login("someviewer")

    app.router.add_get("/dev/login", dev_login)
    app.router.add_get("/dev/viewer", dev_viewer)

    async def on_startup(app):
        db = await aiosqlite.connect(":memory:")
        await seed(db)
        server._db = db
        plugin.db = db

    app.on_startup.append(on_startup)

    print(f"[videos-dev] {origin}/dev/login  (broadcaster = {BROADCASTER}, "
          f"plugin ready = {plugin.ready})")
    if not args.no_open:
        webbrowser.open(f"{origin}/dev/login")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
