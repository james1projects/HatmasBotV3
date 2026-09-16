"""
core/youtube_admin_web.py — /admin/videos: which god each upload pays out
=========================================================================

    GET  /admin/videos                       the page
    GET  /api/admin/videos                   every known upload + counts
    GET  /api/admin/videos/gods              roster for the picker
    POST /api/admin/videos/refresh           walk the whole uploads playlist
    POST /api/admin/videos/{id}/god  {god}   tag (or change) + pay commenters now
    POST /api/admin/videos/{id}/skip         "no share for this video"
    POST /api/admin/videos/{id}/unskip       back to the queue
    POST /api/admin/videos/{id}/rescan       pay any new commenters now

Gate: the broadcaster's Twitch login only, exactly like /admin/events —
anyone else gets the styled 404 on every route. Writes also pass the
origin + IP rate-limit guards and are appended to
data/youtube_videos_audit.log.

The YouTube I/O (refresh, comment scans) goes through the running
YouTubeRewardsPlugin (bot.plugins["youtube_rewards"]). Reads and the god
mapping itself are plain SQLite via core/youtube_videos.py, so the page
still renders when the plugin is off; only the actions report an error.

Design: docs/YOUTUBE_VIDEO_ADMIN.md
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from aiohttp import web

from core import god_roster as _roster
from core import youtube_videos as _yv
from core.youtube_parser import load_known_gods
from core.youtube_schema import ensure_youtube_schema

BASE_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DIR = BASE_DIR / "public"
DATA_DIR = BASE_DIR / "data"
AUDIT_LOG = DATA_DIR / "youtube_videos_audit.log"

_NO_STORE = {"Cache-Control": "no-store"}
_NOT_RUNNING = ("The YouTube scanner is not running (youtube_rewards "
                "plugin off, or YOUTUBE_API_KEY / YOUTUBE_CHANNEL_ID "
                "missing). Tags are saved; grants happen on its next scan.")


class YouTubeAdminWeb:
    def __init__(self, server):
        self.server = server
        self._schema_ready = False

    def register(self) -> None:
        r = self.server.app.router
        r.add_get("/admin/videos", self.handle_page)
        r.add_get("/api/admin/videos", self.handle_list)
        r.add_get("/api/admin/videos/gods", self.handle_gods)
        r.add_post("/api/admin/videos/refresh", self.handle_refresh)
        r.add_post("/api/admin/videos/{video_id}/god", self.handle_set_god)
        r.add_post("/api/admin/videos/{video_id}/skip", self.handle_skip)
        r.add_post("/api/admin/videos/{video_id}/unskip", self.handle_unskip)
        r.add_post("/api/admin/videos/{video_id}/rescan", self.handle_rescan)

    # ── plumbing ──────────────────────────────────────────────────────

    def _plugin(self):
        bot = getattr(self.server, "bot", None)
        plugins = (getattr(bot, "plugins", None) or {}) if bot else {}
        return plugins.get("youtube_rewards")

    def _plugin_ready(self) -> bool:
        p = self._plugin()
        fn = getattr(p, "is_ready", None)
        return bool(fn()) if callable(fn) else False

    async def _db(self):
        db = getattr(self.server, "_db", None)
        if db is None:
            return None
        if not self._schema_ready:
            # Defense in depth: the economy plugin normally creates these
            # tables, but the page must work even before its first boot
            # after this feature shipped.
            await ensure_youtube_schema(db)
            self._schema_ready = True
        return db

    def _ident(self, request: web.Request) -> dict:
        """Broadcaster session or the styled 404 — never a 401/403, so
        the page's existence is not revealed."""
        fn = getattr(self.server, "_broadcaster_identity", None)
        ident = fn(request) if callable(fn) else None
        if ident is None:
            raise web.HTTPNotFound()
        return ident

    def _write_guards(self, request: web.Request) -> tuple:
        """(ident, error_response). Same order as the events admin:
        session+broadcaster -> origin -> rate limit."""
        ident = self._ident(request)
        origin_ok = getattr(self.server, "_origin_ok", None)
        if callable(origin_ok) and not origin_ok(request):
            return None, self._err("Bad origin.", 403)
        rate_ok = getattr(self.server, "_ip_rate_ok", None)
        if callable(rate_ok) and not rate_ok(request):
            return None, self._err("Too many requests.", 429)
        return ident, None

    @staticmethod
    def _err(message: str, status: int = 400, **extra) -> web.Response:
        body = {"ok": False, "error": message}
        body.update(extra)
        return web.json_response(body, status=status, headers=_NO_STORE)

    @staticmethod
    def _ok(**body) -> web.Response:
        body.setdefault("ok", True)
        return web.json_response(body, headers=_NO_STORE)

    def _video_id(self, request: web.Request) -> Optional[str]:
        vid = str(request.match_info.get("video_id") or "").strip()
        return vid if _yv.valid_video_id(vid) else None

    def _known_gods(self) -> List[str]:
        """Roster union icon library: the live wiki roster catches a god
        that shipped this week, the icon stems catch anything the
        scanner itself would accept."""
        names = set()
        try:
            names.update(_roster.names())
        except Exception as e:  # roster file unreadable — icons still work
            print(f"[YouTubeAdmin] roster unavailable: {e}")
        p = self._plugin()
        fn = getattr(p, "known_gods", None)
        if callable(fn):
            names.update(fn() or [])
        else:
            names.update(load_known_gods(BASE_DIR))
        return sorted(names)

    @staticmethod
    def _icon(name: str) -> str:
        return "/god-icon/" + name.lower().replace(" ", "-").replace("'", "")

    def _audit(self, login: str, action: str, video_id: str, detail: str = ""):
        line = (f"{datetime.now().isoformat(timespec='seconds')} | {login} | "
                f"{action} | {video_id} | {detail}\n")
        print(f"[YouTubeAdmin] {line.strip()}")
        try:
            AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            print(f"[YouTubeAdmin] audit write failed: {e}")

    # ── handlers ──────────────────────────────────────────────────────

    async def handle_page(self, request: web.Request):
        self._ident(request)
        path = PUBLIC_DIR / "videos_admin.html"
        if not path.exists():
            return web.Response(text="Videos admin page missing.", status=500)
        return web.FileResponse(path, headers=_NO_STORE)

    async def handle_list(self, request: web.Request):
        ident = self._ident(request)
        db = await self._db()
        if db is None:
            return self._err("Database unavailable.", 503)
        videos = await _yv.list_videos(db, self._known_gods())
        counts = {_yv.STATUS_UNCATEGORIZED: 0, _yv.STATUS_CATEGORIZED: 0,
                  _yv.STATUS_SKIPPED: 0}
        for v in videos:
            counts[v["status"]] += 1
            v["god_icon"] = self._icon(v["god"]) if v.get("god") else None
        return self._ok(user=ident.get("login"),
                        plugin_ready=self._plugin_ready(),
                        counts=counts, videos=videos)

    async def handle_gods(self, request: web.Request):
        self._ident(request)
        gods = [{"name": g, "icon": self._icon(g)} for g in self._known_gods()]
        return self._ok(gods=gods)

    async def handle_refresh(self, request: web.Request):
        ident, err = self._write_guards(request)
        if err is not None:
            return err
        plugin = self._plugin()
        if not self._plugin_ready():
            return self._err(_NOT_RUNNING, 503)
        try:
            result = await plugin.refresh_videos()
        except Exception as e:
            return self._err(f"YouTube refresh failed: {e}", 502)
        self._audit(ident.get("login") or "?", "refresh", "-",
                    f"seen={result.get('seen')} added={result.get('added')} "
                    f"auto_tagged={result.get('auto_tagged')}")
        return self._ok(**result)

    async def handle_set_god(self, request: web.Request):
        ident, err = self._write_guards(request)
        if err is not None:
            return err
        vid = self._video_id(request)
        if vid is None:
            return self._err("Bad video id.")
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._err("Bad JSON.")
        if not isinstance(body, dict):
            return self._err("Bad JSON.")
        god = _yv.resolve_god(str(body.get("god") or ""), self._known_gods())
        if god is None:
            return self._err("Unknown god. Pick one from the list.")
        db = await self._db()
        if db is None:
            return self._err("Database unavailable.", 503)

        current = await _yv.get_god(db, vid)
        action = "tagged"
        if current is not None:
            if current == god:
                action = "unchanged"
            else:
                grants = await _yv.grant_count(db, vid)
                if grants > 0:
                    return self._err(
                        f"{grants} viewer(s) already hold shares of {current} "
                        f"from this video. Reassigning a paid-out video is "
                        f"not supported yet.", 409, grants=grants,
                        current=current)
                action = "changed"

        login = ident.get("login") or "?"
        if action != "unchanged":
            await _yv.set_god(db, vid, god, f"web:{login}")
            self._audit(login, action, vid,
                        f"{current or '-'} -> {god}" if current else god)

        granted = 0
        warning = None
        plugin = self._plugin()
        if self._plugin_ready():
            try:
                granted = await plugin.scan_video(vid)
            except Exception as e:
                warning = f"Saved, but the comment scan failed: {e}"
        else:
            warning = _NOT_RUNNING
        if granted:
            self._audit(login, "granted", vid, f"{granted} x {god}")
        return self._ok(god=god, action=action, granted=granted,
                        warning=warning)

    async def handle_skip(self, request: web.Request):
        return await self._toggle_skip(request, True)

    async def handle_unskip(self, request: web.Request):
        return await self._toggle_skip(request, False)

    async def _toggle_skip(self, request: web.Request, skipped: bool):
        ident, err = self._write_guards(request)
        if err is not None:
            return err
        vid = self._video_id(request)
        if vid is None:
            return self._err("Bad video id.")
        db = await self._db()
        if db is None:
            return self._err("Database unavailable.", 503)
        if skipped:
            grants = await _yv.grant_count(db, vid)
            if grants > 0:
                current = await _yv.get_god(db, vid)
                return self._err(
                    f"{grants} viewer(s) already hold shares of {current} "
                    f"from this video; it can't be skipped.", 409,
                    grants=grants)
        await _yv.set_skipped(db, vid, skipped)
        self._audit(ident.get("login") or "?",
                    "skipped" if skipped else "unskipped", vid)
        return self._ok(status=_yv.STATUS_SKIPPED if skipped
                        else _yv.STATUS_UNCATEGORIZED)

    async def handle_rescan(self, request: web.Request):
        ident, err = self._write_guards(request)
        if err is not None:
            return err
        vid = self._video_id(request)
        if vid is None:
            return self._err("Bad video id.")
        db = await self._db()
        if db is None:
            return self._err("Database unavailable.", 503)
        god = await _yv.get_god(db, vid)
        if god is None:
            return self._err("This video has no god yet.")
        plugin = self._plugin()
        if not self._plugin_ready():
            return self._err(_NOT_RUNNING, 503)
        try:
            granted = await plugin.scan_video(vid)
        except Exception as e:
            return self._err(f"Comment scan failed: {e}", 502)
        self._audit(ident.get("login") or "?", "rescan", vid,
                    f"{granted} x {god}")
        return self._ok(god=god, granted=granted)
