"""
Local Web Server
=================
Serves overlay HTML files and the control panel.
Also provides a JSON API for real-time overlay data updates.

Endpoints:
  GET  /                        — Control panel
  GET  /overlay/nowplaying      — Now Playing overlay (450x120, OBS browser source)
  GET  /overlay/youtube_player  — YouTube audio player (OBS browser source, hidden)
  GET  /overlay/snap            — Snap overlay
  GET  /api/state               — Full JSON state (now_playing, queue, features, etc.)
  POST /api/state               — Partial state update
  POST /api/action              — Trigger actions (skip, snap, youtube_ended, etc.)
"""

import asyncio
import json
from aiohttp import web
from pathlib import Path

from core.config import WEB_HOST, WEB_PORT, OVERLAY_DIR


class WebServer:
    def __init__(self, bot=None):
        self.bot = bot
        self.app = web.Application()
        self.runner = None
        self._state = {
            "now_playing": None,
            "queue": [],
            "snap_active": False,
            "features": {},
            "stats": {},
            "youtube_playback": {
                "video_id": None,
                "status": "idle",  # idle | pending | playing
            },
        }
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/", self.handle_control_panel)
        self.app.router.add_get("/overlay/nowplaying", self.handle_now_playing_overlay)
        self.app.router.add_get("/overlay/youtube_player", self.handle_youtube_player_overlay)
        self.app.router.add_get("/overlay/snap", self.handle_snap_overlay)
        self.app.router.add_get("/api/state", self.handle_get_state)
        self.app.router.add_post("/api/state", self.handle_update_state)
        self.app.router.add_post("/api/action", self.handle_action)
        self.app.router.add_static("/overlays/", OVERLAY_DIR)

    # === API HANDLERS ===

    async def handle_get_state(self, request):
        state = dict(self._state)
        if self.bot:
            state["features"] = self.bot.features
            state["stats"] = {
                "uptime": self.bot.get_uptime(),
                "commands": self.bot.command_count,
                "plugins": list(self.bot.plugins.keys()),
            }
        return web.json_response(state)

    async def handle_update_state(self, request):
        data = await request.json()
        self._state.update(data)
        return web.json_response({"ok": True})

    async def handle_action(self, request):
        data = await request.json()
        action = data.get("action")

        if not self.bot:
            return web.json_response({"error": "Bot not connected"}, status=500)

        if action == "toggle_feature":
            feature = data.get("feature")
            enabled = data.get("enabled", True)
            self.bot.set_feature(feature, enabled)
            return web.json_response({"ok": True, "feature": feature, "enabled": enabled})

        elif action == "skip_song":
            if "songrequest" in self.bot.plugins:
                await self.bot.plugins["songrequest"].skip_current()
            return web.json_response({"ok": True})

        elif action == "youtube_ended":
            # YouTube player reports that the video finished
            self._state["youtube_playback"]["status"] = "idle"
            self._state["youtube_playback"]["video_id"] = None
            if "songrequest" in self.bot.plugins:
                asyncio.create_task(
                    self.bot.plugins["songrequest"].on_youtube_ended()
                )
            return web.json_response({"ok": True})

        elif action == "youtube_started":
            # YouTube player confirms it started playing
            self._state["youtube_playback"]["status"] = "playing"
            if "songrequest" in self.bot.plugins:
                asyncio.create_task(
                    self.bot.plugins["songrequest"].on_youtube_started()
                )
            return web.json_response({"ok": True})

        elif action == "youtube_progress":
            # YouTube player sends progress updates
            progress_ms = data.get("progress_ms", 0)
            if "songrequest" in self.bot.plugins:
                asyncio.create_task(
                    self.bot.plugins["songrequest"].on_youtube_progress(progress_ms)
                )
            return web.json_response({"ok": True})

        elif action == "snap":
            if "snap" in self.bot.plugins:
                await self.bot.plugins["snap"].execute_snap()
            return web.json_response({"ok": True})

        elif action == "go_live":
            if "obs" in self.bot.plugins:
                await self.bot.plugins["obs"].go_live_sequence()
            return web.json_response({"ok": True})

        elif action == "stop_stream":
            if "obs" in self.bot.plugins:
                await self.bot.plugins["obs"].stop_stream_sequence()
            return web.json_response({"ok": True})

        elif action == "resolve_prediction":
            outcome = data.get("outcome")  # "win" or "loss"
            if "smite" in self.bot.plugins:
                await self.bot.plugins["smite"].resolve_prediction(outcome)
            return web.json_response({"ok": True})

        elif action == "send_chat":
            msg = data.get("message", "")
            if msg:
                await self.bot.send_chat(msg)
            return web.json_response({"ok": True})

        return web.json_response({"error": f"Unknown action: {action}"}, status=400)

    # === OVERLAY PAGES ===

    async def handle_now_playing_overlay(self, request):
        html_path = OVERLAY_DIR / "nowplaying.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Overlay not found", status=404)

    async def handle_youtube_player_overlay(self, request):
        html_path = OVERLAY_DIR / "youtube_player.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="YouTube player overlay not found", status=404)

    async def handle_snap_overlay(self, request):
        html_path = OVERLAY_DIR / "snap.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Overlay not found", status=404)

    # === CONTROL PANEL ===

    async def handle_control_panel(self, request):
        html_path = OVERLAY_DIR / "control_panel.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Control panel not found", status=404)

    # === STATE MANAGEMENT ===

    def update_now_playing(self, data):
        self._state["now_playing"] = data

    def update_queue(self, queue):
        self._state["queue"] = queue

    def set_snap_active(self, active):
        self._state["snap_active"] = active

    def set_youtube_playback(self, video_id):
        """Signal the YouTube player overlay to start playing a video."""
        self._state["youtube_playback"] = {
            "video_id": video_id,
            "status": "pending",
        }

    def clear_youtube_playback(self):
        """Stop YouTube playback (e.g., on skip)."""
        self._state["youtube_playback"] = {
            "video_id": None,
            "status": "idle",
        }

    # === SERVER LIFECYCLE ===

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, WEB_HOST, WEB_PORT)
        await site.start()
        print(f"[WebServer] Running at http://{WEB_HOST}:{WEB_PORT}")
        print(f"[WebServer] Control panel:    http://{WEB_HOST}:{WEB_PORT}/")
        print(f"[WebServer] Now Playing:      http://{WEB_HOST}:{WEB_PORT}/overlay/nowplaying")
        print(f"[WebServer] YouTube Player:   http://{WEB_HOST}:{WEB_PORT}/overlay/youtube_player")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
