"""
core/alert_web.py — dashboard routes for the Sources page and the
Hatmaster Alert Box (docs/ALERT_BOX.md). Mounted by core/webserver.py
(localhost:8069) and by tools/bingo_devserver.py.

    GET  /sources                     the Sources page
    GET  /api/sources                 registry with live client counts
    POST /api/sources/test?key=       emit that source's sample event
    GET  /overlay/alerts?box=main     the alert box browser source
    GET  /alerts/layout?box=main      the layout editor
    GET  /api/alerts/config           full config + kinds catalog
    POST /api/alerts/config           save (JSON body = the config)
    POST /api/alerts/test?kind=&box=  fire a sample alert
    GET  /api/alerts/recent?box=      last alerts
    POST /api/alerts/replay?id=&box=  play one of them again
    GET  /api/alerts/status
    GET|POST /api/alerts/background   the editor's scene screenshot (POST multipart "image")
    POST /api/alerts/background/clear
"""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from core import sources as _sources
from core.alert_box import ANCHORS, kinds_catalog

OVERLAY_DIR = Path(__file__).resolve().parent.parent / "overlays"


def _file(path: Path):
    resp = web.FileResponse(path)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


class AlertWeb:
    def __init__(self, get_alert_box, get_overlay_manager, base: str = _sources.DASHBOARD):
        self._get_box = get_alert_box
        self._get_overlay = get_overlay_manager
        self._base = base

    def register(self, router) -> None:
        router.add_get("/sources", self.handle_sources_page)
        router.add_get("/sounds", self.handle_sounds_page)        # audition every sample, simulate the spin reel
        router.add_get("/api/sounds", self.handle_sounds)
        router.add_get("/api/sources", self.handle_sources)
        router.add_post("/api/sources/test", self.handle_sources_test)
        router.add_get("/api/sources/test", self.handle_sources_test)
        router.add_get("/overlay/alerts", self.handle_alerts_page)
        router.add_get("/alerts/layout", self.handle_layout_page)
        router.add_get("/api/alerts/config", self.handle_config)
        router.add_post("/api/alerts/config", self.handle_config_save)
        router.add_post("/api/alerts/test", self.handle_test)
        router.add_get("/api/alerts/test", self.handle_test)
        router.add_get("/api/alerts/recent", self.handle_recent)
        router.add_post("/api/alerts/replay", self.handle_replay)
        router.add_get("/api/alerts/status", self.handle_status)
        router.add_get("/api/alerts/background", self.handle_background)
        router.add_post("/api/alerts/background", self.handle_background_upload)
        router.add_post("/api/alerts/background/clear", self.handle_background_clear)

    def _box(self):
        b = self._get_box()
        if b is None:
            raise web.HTTPServiceUnavailable(text=json.dumps({"ok": False, "error": "alert box not ready"}),
                                             content_type="application/json")
        return b

    @staticmethod
    def _reply(res, ok_status: int = 200):
        return web.json_response(res, status=ok_status if not isinstance(res, dict) or res.get("ok", True) else 400,
                                 headers={"Cache-Control": "no-store"})

    # ── sources ───────────────────────────────────────────────────────

    async def handle_sources_page(self, request):
        return _file(OVERLAY_DIR / "sources.html")

    async def handle_sounds_page(self, request):
        return _file(OVERLAY_DIR / "soundboard.html")

    async def handle_sounds(self, request):
        """Every sample under assets/sounds (served at /assets/sounds/), so
        the sound board can list them without a hand-kept manifest."""
        from core.config import SOUNDS_DIR
        files = []
        if SOUNDS_DIR.is_dir():
            for f in sorted(SOUNDS_DIR.rglob("*.ogg")):
                rel = f.relative_to(SOUNDS_DIR).as_posix()
                parts = rel.split("/")
                files.append({"path": rel, "pack": "/".join(parts[:-1]), "name": f.stem,
                              "bytes": f.stat().st_size})
        return web.json_response({"base": "/assets/sounds/", "files": files})

    async def handle_sources(self, request):
        return self._reply({"ok": True, "sources": _sources.registry(self._get_box(), self._get_overlay(), self._base)})

    async def handle_sources_test(self, request):
        key = (request.query.get("key") or "").strip()
        if key.startswith("alerts:"):
            box = self._box()
            name = key.split(":", 1)[1]
            cfg = box.box_config(name)
            if cfg is None:
                return self._reply({"ok": False, "error": f"unknown box '{name}'"})
            enabled = [k for k, v in cfg["kinds"].items() if v["enabled"]]
            if not enabled:
                return self._reply({"ok": False, "error": "no kinds enabled in that box"})
            fired = [(await box.test(k, name))["alert"]["id"] for k in enabled[:3]]
            return self._reply({"ok": True, "fired": fired, "kinds": enabled[:3]})
        src = _sources.find(key)
        if src is None:
            return self._reply({"ok": False, "error": f"unknown source '{key}'"})
        if not src.get("test"):
            return self._reply({"ok": False, "error": "that source has no test event"})
        overlay = self._get_overlay()
        if overlay is None:
            return self._reply({"ok": False, "error": "overlay manager not ready"})
        event, data = src["test"]
        await overlay.emit(event, dict(data))
        return self._reply({"ok": True, "event": event, "clients": _sources._clients(overlay, key)})

    # ── alert box ─────────────────────────────────────────────────────

    async def handle_alerts_page(self, request):
        return _file(OVERLAY_DIR / "alerts.html")

    async def handle_layout_page(self, request):
        return _file(OVERLAY_DIR / "alerts_layout.html")

    async def handle_config(self, request):
        box = self._box()
        return self._reply({"ok": True, "config": box.config(), "kinds": kinds_catalog(),
                            "anchors": list(ANCHORS),
                            "status": box.status()})

    async def handle_config_save(self, request):
        box = self._box()
        try:
            raw = await request.json()
        except Exception:
            return self._reply({"ok": False, "error": "body must be JSON"})
        cfg = await box.save_config(raw.get("config", raw) if isinstance(raw, dict) else raw)
        return self._reply({"ok": True, "config": cfg})

    async def handle_test(self, request):
        box = self._box()
        kind = (request.query.get("kind") or "").strip()
        name = (request.query.get("box") or "main").strip()
        data = None
        if request.method == "POST" and request.can_read_body:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    kind = kind or str(body.get("kind") or "")
                    name = str(body.get("box") or name)
                    data = body.get("data")
            except Exception:
                pass
        return self._reply(await box.test(kind, name, data))

    async def handle_recent(self, request):
        box = self._box()
        name = (request.query.get("box") or "main").strip()
        return self._reply({"ok": True, "box": name, "recent": box.recent(name)})

    async def handle_replay(self, request):
        box = self._box()
        try:
            alert_id = int(request.query.get("id") or 0)
        except ValueError:
            alert_id = 0
        return self._reply(await box.replay(alert_id, request.query.get("box") or None))

    async def handle_status(self, request):
        return self._reply({"ok": True, **self._box().status()})

    # ── scene screenshot for the layout editor ────────────────────────

    async def handle_background(self, request):
        path = self._box().background_path()
        if path is None:
            raise web.HTTPNotFound()
        resp = web.FileResponse(path)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    async def handle_background_upload(self, request):
        """multipart field "image" (the editor's file input) or a raw image body."""
        box = self._box()
        data, ext = b"", ""
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            async for part in reader:
                if part.name == "image":
                    data = await part.read(decode=False)
                    ext = (part.filename or "").rsplit(".", 1)[-1] if "." in (part.filename or "") else ""
                    break
        else:
            data = await request.read()
            ext = request.content_type.split("/")[-1]
        return self._reply(box.set_background(data, ext))

    async def handle_background_clear(self, request):
        return self._reply(self._box().clear_background())
