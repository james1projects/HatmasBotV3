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
import uuid
import os
from datetime import datetime
from aiohttp import web
from pathlib import Path

from core.config import (
    WEB_HOST, WEB_PORT, OVERLAY_DIR, DATA_DIR, BASE_DIR,
    TITLE_TEMPLATE_GOD, TITLE_TEMPLATE_LOBBY,
    SR_PLAYLIST_AUTO_HIDE_SECONDS,
)
from core.overlay_manager import OverlayManager

TTS_AUDIO_DIR = DATA_DIR / "tts_audio"
VOICELINE_DIR = DATA_DIR / "smite_voicelines"
ANIMATION_DIR = DATA_DIR / "smite_animations"
SPACEGAME_DIR = BASE_DIR / "public" / "spacegame"
TTS_AUDIO_DIR.mkdir(exist_ok=True)


class WebServer:
    def __init__(self, bot=None):
        self.bot = bot
        self.app = web.Application()
        self.runner = None
        self.overlay = OverlayManager(self)
        self._state = {
            "now_playing": None,
            "queue": [],
            "snap_active": False,
            "features": {},
            "stats": {},
            "like_event": 0,  # Timestamp of last like — overlay uses this to trigger hearts
            "sound_alert": {"type": None, "timestamp": 0},  # Current sound alert for overlay
            "gamble_result_queue": [],  # Queue of gamble results for visual overlay
            "tts_queue": [],              # Queue of TTS messages for highlighted message overlay
            "kill_event_queue": [],       # Queue of kill/death events for overlay
            "voiceline_event_queue": [],  # Queue of voice line events for overlay
            "youtube_playback": {
                "video_id": None,
                "status": "idle",  # idle | pending | playing
            },
            "smite": {
                "in_match": False,
                "god": None,
                "players": [],
                "match_id": None,
                "match_duration": 0,
            },
            "god_requests": {
                "queue": [],
                "next_god": None,
                "queue_length": 0,
                "mixitup_connected": False,
            },
        }
        # VOD processor state — populated by tools/process_recordings.py
        # via POST endpoints. Read by /api/detector_debug when active
        # (mode flips from "live" to "vod" while a scan is in progress).
        # Stays None when no scan is running, so /detector falls back
        # to live-detector data.
        self._vod_state: "dict | None" = None

        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/", self.handle_control_panel)
        self.app.router.add_get("/overlay/nowplaying", self.handle_now_playing_overlay)
        self.app.router.add_get("/overlay/youtube_player", self.handle_youtube_player_overlay)
        self.app.router.add_get("/overlay/snap", self.handle_snap_overlay)
        self.app.router.add_get("/overlay/god", self.handle_god_overlay)
        self.app.router.add_get("/overlay/sound_alerts", self.handle_sound_alerts_overlay)
        self.app.router.add_get("/api/gamble_queue", self.handle_gamble_queue)
        self.app.router.add_get("/api/tts_queue", self.handle_tts_queue)
        self.app.router.add_get("/api/tts_audio/{filename}", self.handle_tts_audio)
        self.app.router.add_get("/overlay/tts", self.handle_tts_overlay)
        self.app.router.add_get("/api/kill_events", self.handle_kill_event_queue)
        self.app.router.add_get("/api/kill_stats", self.handle_kill_stats)
        self.app.router.add_get("/api/death_count", self.handle_death_count)
        self.app.router.add_get("/overlay/deaths", self.handle_deaths_overlay)
        self.app.router.add_get("/api/voiceline_events", self.handle_voiceline_event_queue)
        self.app.router.add_get("/api/voiceline_audio/{god}/{folder}/{filename}", self.handle_voiceline_audio)
        self.app.router.add_get("/api/voiceline_video/{god}/{filename}", self.handle_voiceline_video)
        self.app.router.add_get("/overlay/voicelines", self.handle_voiceline_overlay)
        self.app.router.add_get("/overlay/spin", self.handle_spin_overlay)
        self.app.router.add_get("/api/suggestions", self.handle_get_suggestions)
        self.app.router.add_get("/api/priority_payments",
                                self.handle_priority_payments)
        self.app.router.add_get("/api/state", self.handle_get_state)
        self.app.router.add_post("/api/state", self.handle_update_state)
        self.app.router.add_post("/api/action", self.handle_action)
        self.app.router.add_get("/ws/overlays", self.handle_overlay_ws)
        # Detector debug viewer — live observability into the KDA + portrait
        # detector pipeline. See plugins/killdetector.py:_build_debug_state.
        self.app.router.add_get("/detector", self.handle_detector_page)
        self.app.router.add_get("/api/detector_debug", self.handle_detector_debug)
        self.app.router.add_get(
            "/api/detector_debug/fullframe.jpg",
            self.handle_detector_fullframe,
        )
        # Raw screenshot WITHOUT baked-in region annotations.
        # Used by the drag-UI work so JS can overlay rectangles on top.
        self.app.router.add_get(
            "/api/detector_debug/fullframe_raw.jpg",
            self.handle_detector_fullframe_raw,
        )
        # Persisted detection-region coordinates (kda, portrait,
        # hud_check, gameplay_check, overlay_check). GET returns
        # current values; POST saves new ones to detector_regions.json.
        self.app.router.add_get(
            "/api/detector_regions",
            self.handle_detector_regions_get,
        )
        self.app.router.add_post(
            "/api/detector_regions",
            self.handle_detector_regions_post,
        )
        self.app.router.add_post(
            "/api/detector_debug/save_snapshot",
            self.handle_detector_save_snapshot,
        )
        # Operator pause/resume the live scan loop. Used by /detector's
        # "Save portrait as reference" workflow to guarantee the frame
        # being displayed matches the frame written to disk.
        self.app.router.add_post(
            "/api/detector_debug/pause",
            self.handle_detector_pause,
        )
        self.app.router.add_post(
            "/api/detector_debug/resume",
            self.handle_detector_resume,
        )
        # Crop the current frame's portrait region and save it to
        # Portrait_Source/<God>.png, then reload the matcher so the new
        # reference takes effect on the next poll. Body: {"god_name":"Ymir"}.
        self.app.router.add_post(
            "/api/detector_debug/save_portrait_reference",
            self.handle_detector_save_portrait_reference,
        )

        # Stream Deck / external-trigger entry point for !spin. Both
        # GET and POST accepted so a Stream Deck "System: Open URL"
        # button works alongside HTTP-plugin POSTs. Returns the spin
        # result JSON so power users can chain it (toast, sound, etc.).
        self.app.router.add_get("/api/spin", self.handle_spin_trigger)
        self.app.router.add_post("/api/spin", self.handle_spin_trigger)

        # VOD processor → /detector dashboard bridge. The processor
        # (tools/process_recordings.py) POSTs its progress here while
        # running so the /detector page can render a batch-progress
        # view alongside the live detector. See bottom of this file
        # for handlers.
        self.app.router.add_post(
            "/api/vod_processor/start", self.handle_vod_start)
        self.app.router.add_post(
            "/api/vod_processor/update", self.handle_vod_update)
        self.app.router.add_post(
            "/api/vod_processor/file_done", self.handle_vod_file_done)
        self.app.router.add_post(
            "/api/vod_processor/event", self.handle_vod_event)
        self.app.router.add_post(
            "/api/vod_processor/kda_failure",
            self.handle_vod_kda_failure)
        self.app.router.add_post(
            "/api/vod_processor/stop", self.handle_vod_stop)
        self.app.router.add_static("/overlays/", OVERLAY_DIR)
        # Serve god icon directories for overlays
        from core.config import CUSTOM_GOD_ICONS_DIR, GOD_ICONS_DIR
        if CUSTOM_GOD_ICONS_DIR.exists():
            self.app.router.add_static("/icons/custom/", CUSTOM_GOD_ICONS_DIR)
        if GOD_ICONS_DIR.exists():
            self.app.router.add_static("/icons/gods/", GOD_ICONS_DIR)

        # Streaming Space Game (Phase 1 prototype). Served over http so the
        # browser actually loads its art — opening index.html as a file://
        # is blocked by the browser's CORS policy. Open it at:
        #     http://localhost:8069/spacegame
        self.app.router.add_get("/spacegame", self.handle_spacegame)
        self.app.router.add_get("/spacegame/", self.handle_spacegame)
        _spacegame_src = SPACEGAME_DIR / "src"
        if _spacegame_src.exists():
            self.app.router.add_static("/spacegame/src/", _spacegame_src)
        _spacegame_assets = SPACEGAME_DIR / "assets"
        if _spacegame_assets.exists():
            self.app.router.add_static("/spacegame/assets/", _spacegame_assets)

    # === ROOT HANDLER ===

    async def handle_control_panel(self, request):
        """Serve the bot's control panel HTML at /. Restored after an
        earlier truncation dropped this method even though the route
        for it stayed registered."""
        html_path = OVERLAY_DIR / "control_panel.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="Control panel not found", status=404)

    async def handle_spacegame(self, request):
        """Serve the Streaming Space Game prototype at /spacegame.

        Redirect the bare /spacegame to /spacegame/ so the page's relative
        asset paths (assets/...) resolve under /spacegame/assets/ rather
        than the server root.
        """
        if not (self.bot and self.bot.is_feature_enabled("spacegame")):
            return web.Response(
                text="Space game is disabled. Flip the 'spacegame' feature "
                     "toggle on the control panel to enable it.",
                status=404)
        if request.path == "/spacegame":
            raise web.HTTPFound("/spacegame/")
        index_path = SPACEGAME_DIR / "index.html"
        if index_path.exists():
            return self._no_cache_file_response(index_path)
        return web.Response(text="Space game not found", status=404)

    # === API HANDLERS ===

    def trigger_like_event(self):
        """Signal the overlay that a like just happened (triggers heart animation)."""
        import time
        ts = time.time()
        self._state["like_event"] = ts
        asyncio.create_task(self.overlay.emit("song_like", {"like_event": ts}))

    def trigger_sound_alert(self, alert_type):
        """Trigger a sound alert for the overlay.
        Types: 'jackpot', 'big_win', 'win', 'loss'"""
        import time
        self._state["sound_alert"] = {
            "type": alert_type,
            "timestamp": time.time(),
        }
        print(f"[WebServer] Sound alert triggered: {alert_type}")

    def trigger_gamble_result(self, result):
        """Push a gamble result onto the queue for the visual overlay.
        result: {type, player, roll, winnings, wager}"""
        import time
        result["timestamp"] = time.time()
        self._state["gamble_result_queue"].append(result)
        # Cap queue at 20 to prevent unbounded growth
        if len(self._state["gamble_result_queue"]) > 20:
            self._state["gamble_result_queue"] = self._state["gamble_result_queue"][-20:]
        asyncio.create_task(self.overlay.emit("gamble_result", result))

    async def handle_gamble_queue(self, request):
        """Return and drain the gamble result queue. The overlay calls this to
        pick up all pending results without missing any."""
        queue = list(self._state["gamble_result_queue"])
        self._state["gamble_result_queue"] = []
        return web.json_response(queue)

    def trigger_tts(self, display_name, message):
        """Generate TTS audio with gTTS and queue it for the overlay."""
        import time
        try:
            from gtts import gTTS
            audio_id = str(uuid.uuid4())[:8]
            filename = f"tts_{audio_id}.mp3"
            filepath = TTS_AUDIO_DIR / filename

            tts = gTTS(text=message, lang='en', slow=False)
            tts.save(str(filepath))

            self._state["tts_queue"].append({
                "user": display_name,
                "message": message,
                "audio": f"/api/tts_audio/{filename}",
                "timestamp": time.time(),
            })
            # Cap queue at 20
            if len(self._state["tts_queue"]) > 20:
                self._state["tts_queue"] = self._state["tts_queue"][-20:]

            # Clean up old audio files (keep last 30)
            self._cleanup_tts_audio()

            print(f"[WebServer] TTS queued: {display_name} — {message[:60]}...")
            asyncio.create_task(self.overlay.emit("tts_message", {
                "user": display_name,
                "message": message,
                "audio": f"/api/tts_audio/{filename}",
            }))
        except Exception as e:
            print(f"[WebServer] TTS generation failed: {e}")

    def _cleanup_tts_audio(self):
        """Remove old TTS audio files to prevent disk bloat."""
        try:
            files = sorted(TTS_AUDIO_DIR.glob("tts_*.mp3"), key=lambda f: f.stat().st_mtime)
            if len(files) > 30:
                for f in files[:-30]:
                    f.unlink(missing_ok=True)
        except Exception:
            pass

    async def handle_tts_queue(self, request):
        """Return and drain the TTS queue. The overlay calls this to
        pick up pending TTS messages."""
        queue = list(self._state["tts_queue"])
        self._state["tts_queue"] = []
        return web.json_response(queue)

    async def handle_tts_audio(self, request):
        """Serve a generated TTS audio file."""
        filename = request.match_info["filename"]
        # Sanitize — only allow expected filenames
        if not filename.startswith("tts_") or not filename.endswith(".mp3"):
            return web.Response(text="Not found", status=404)
        filepath = TTS_AUDIO_DIR / filename
        if filepath.exists():
            return web.FileResponse(filepath, headers={
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-cache",
            })
        return web.Response(text="Not found", status=404)

    def trigger_kill_event(self, event_type, kill_type=None):
        """Push a kill or death event onto the queue for the overlay.
        event_type: 'kill' or 'death'
        kill_type: 'player_kill', 'double_kill', 'triple_kill', etc."""
        import time
        event_data = {
            "event": event_type,
            "kill_type": kill_type,
            "timestamp": time.time(),
        }
        self._state["kill_event_queue"].append(event_data)
        if len(self._state["kill_event_queue"]) > 20:
            self._state["kill_event_queue"] = self._state["kill_event_queue"][-20:]
        # Emit to overlay manager (kill, death, or multikill)
        if event_type == "kill":
            if kill_type and kill_type != "player_kill":
                asyncio.create_task(self.overlay.emit("multikill", event_data))
            else:
                asyncio.create_task(self.overlay.emit("kill", event_data))
        elif event_type == "death":
            asyncio.create_task(self.overlay.emit("death", event_data))

    async def handle_kill_event_queue(self, request):
        """Return and drain the kill event queue for the overlay."""
        queue = list(self._state["kill_event_queue"])
        self._state["kill_event_queue"] = []
        return web.json_response(queue)

    async def handle_kill_stats(self, request):
        """Return current match kill/death stats + detector status for dashboard."""
        if self.bot and "killdetector" in self.bot.plugins:
            kd = self.bot.plugins["killdetector"]
            stats = kd.get_match_stats()
            # Add detector status fields for the dashboard debug panel
            stats["running"] = kd._running
            stats["debug"] = kd._debug
            stats["announce_chat"] = kd._announce_chat
            stats["ocr_available"] = kd._ocr_available
            stats["last_kda"] = list(kd._prev_kda) if kd._prev_kda else None
            # How long ago was the last successful KDA read
            import time
            if kd._prev_kda is not None and hasattr(kd, '_last_kda_read_time'):
                stats["last_read_ago"] = round(time.time() - kd._last_kda_read_time, 1)
            else:
                stats["last_read_ago"] = None
            return web.json_response(stats)
        return web.json_response({
            "kills": 0, "deaths": 0, "assists": 0, "is_dead": False,
            "kill_types": {}, "running": False, "debug": False,
            "announce_chat": False, "ocr_available": False,
            "last_kda": None, "last_read_ago": None, "kda_failures": 0,
        })

    # === DEATH COUNTER HANDLERS ===

    async def handle_death_count(self, request):
        """Return the daily death count for the overlay."""
        if self.bot and "deathcounter" in self.bot.plugins:
            dc = self.bot.plugins["deathcounter"]
            return web.json_response(dc.get_state())
        return web.json_response({"count": 0, "date": None})

    async def handle_deaths_overlay(self, request):
        """Serve the death counter overlay HTML."""
        html_path = OVERLAY_DIR / "deaths.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="Deaths overlay not found", status=404)

    # === VOICE LINE HANDLERS ===

    def trigger_voiceline_event(self, event):
        """Push a voice line event onto the queue for the overlay."""
        self._state["voiceline_event_queue"].append(event)
        if len(self._state["voiceline_event_queue"]) > 10:
            self._state["voiceline_event_queue"] = self._state["voiceline_event_queue"][-10:]
        asyncio.create_task(self.overlay.emit("voiceline_play", event))

    async def handle_voiceline_event_queue(self, request):
        """Return and drain the voice line event queue for the overlay."""
        queue = list(self._state["voiceline_event_queue"])
        self._state["voiceline_event_queue"] = []
        return web.json_response(queue)

    async def handle_voiceline_audio(self, request):
        """Serve a voice line .ogg file."""
        god = request.match_info["god"]
        folder = request.match_info["folder"]
        filename = request.match_info["filename"]
        # Sanitize — only allow .ogg files, no path traversal
        if ".." in filename or "/" in filename or not filename.endswith(".ogg"):
            return web.Response(text="Not found", status=404)
        filepath = VOICELINE_DIR / god / folder / filename
        if filepath.exists():
            return web.FileResponse(filepath, headers={
                "Content-Type": "audio/ogg",
                "Cache-Control": "no-cache",
            })
        return web.Response(text="Not found", status=404)

    async def handle_voiceline_video(self, request):
        """Serve a voice line animation .mp4 file."""
        god = request.match_info["god"]
        filename = request.match_info["filename"]
        if ".." in filename or "/" in filename or not filename.endswith(".mp4"):
            return web.Response(text="Not found", status=404)
        filepath = ANIMATION_DIR / god / filename
        if filepath.exists():
            return web.FileResponse(filepath, headers={
                "Content-Type": "video/mp4",
                "Cache-Control": "no-cache",
            })
        return web.Response(text="Not found", status=404)

    async def handle_voiceline_overlay(self, request):
        """Serve the voice line overlay HTML."""
        html_path = OVERLAY_DIR / "voicelines.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="Voice line overlay not found", status=404)

    async def handle_priority_payments(self, request):
        """Recent $5 priority requests for the control panel table."""
        plugin = self.bot.plugins.get("priority_request") \
            if self.bot else None
        if plugin is None:
            return web.json_response({"payments": []})
        try:
            payments = await plugin.list_payments(limit=50)
        except Exception as e:
            print(f"[WebServer] priority payments read error: {e}")
            payments = []
        return web.json_response({"payments": payments})

    async def handle_get_suggestions(self, request):
        """Return all suggestions for the dashboard."""
        if self.bot and "basic" in self.bot.plugins:
            suggestions = self.bot.plugins["basic"]._suggestions
            return web.json_response(suggestions)
        return web.json_response([])

    async def handle_get_state(self, request):
        state = dict(self._state)
        if self.bot:
            state["features"] = self.bot.features
            state["stats"] = {
                "uptime": self.bot.get_uptime(),
                "commands": self.bot.command_count,
                "plugins": list(self.bot.plugins.keys()),
            }
            # Streamloots hub status for the dashboard
            sl = self.bot.plugins.get("streamloots")
            if sl is not None and hasattr(sl, "get_status"):
                state["streamloots"] = sl.get_status()
            # Factorio integration status for the dashboard
            fp = self.bot.plugins.get("factorio")
            if fp is not None and hasattr(fp, "get_status"):
                state["factorio"] = fp.get_status()
            # Include current title templates and command rotation for the dashboard
            if "smite" in self.bot.plugins:
                import core.config as _cfg
                state["title_templates"] = {
                    "god": _cfg.TITLE_TEMPLATE_GOD,
                    "lobby": _cfg.TITLE_TEMPLATE_LOBBY,
                }
                state["command_rotation"] = self.bot.plugins["smite"].get_rotation_commands()
                state["daily_record"] = {
                    "record": self.bot.plugins["smite"].get_record_string(),
                    "wins": self.bot.plugins["smite"]._session_wins,
                    "losses": self.bot.plugins["smite"]._session_losses,
                }
        state["playlist_auto_hide_seconds"] = SR_PLAYLIST_AUTO_HIDE_SECONDS
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
            if feature == "spacegame":
                # Its commands just (dis)appeared from the catalog —
                # let Discord re-sync its slash commands.
                await self.bot.notify_catalog_changed()
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

        elif action == "set_title_templates":
            god_template = data.get("god_template")
            lobby_template = data.get("lobby_template")
            if "smite" in self.bot.plugins:
                await self.bot.plugins["smite"].set_title_template(
                    god_template=god_template,
                    lobby_template=lobby_template
                )
            return web.json_response({"ok": True})

        elif action == "update_title_now":
            # Manually trigger a title update (e.g., after changing templates)
            if "smite" in self.bot.plugins:
                smite = self.bot.plugins["smite"]
                god_name = smite.current_god["name"] if smite.current_god else None
                await smite._update_stream_title(god_name)
            return web.json_response({"ok": True})

        elif action == "record_result":
            # Manually record a win or loss (updates daily record + title)
            outcome = data.get("outcome", "").lower()
            if outcome in ("win", "loss") and "smite" in self.bot.plugins:
                record = self.bot.plugins["smite"].record_result(outcome)
                # Update title with new record
                smite = self.bot.plugins["smite"]
                god = smite.current_god["name"] if smite.current_god else None
                await smite._update_stream_title(god)
                return web.json_response({"ok": True, "record": record})
            return web.json_response({"error": "Invalid outcome (use 'win' or 'loss')"}, status=400)

        elif action == "test_sound":
            # Test a sound + visual alert from the dashboard
            sound_type = data.get("type", "jackpot")
            self.trigger_sound_alert(sound_type)
            # Also send a test gamble result for the visual popup
            test_results = {
                "jackpot": {"winnings": 50000, "roll": 100, "wager": 1000},
                "big_win": {"winnings": 3000, "roll": 99, "wager": 1000},
                "win":     {"winnings": 2000, "roll": 75, "wager": 1000},
                "loss":    {"winnings": 0, "roll": 23, "wager": 1000},
            }
            test_data = test_results.get(sound_type, test_results["win"])
            self.trigger_gamble_result({
                "type": sound_type,
                "player": "TestUser",
                **test_data
            })
            return web.json_response({"ok": True, "type": sound_type})

        elif action == "add_rotation_command":
            command = data.get("command", "").strip()
            if command and "smite" in self.bot.plugins:
                added = self.bot.plugins["smite"].add_rotation_command(command)
                return web.json_response({"ok": True, "added": added})
            return web.json_response({"error": "Missing command"}, status=400)

        elif action == "remove_rotation_command":
            command = data.get("command", "").strip()
            if command and "smite" in self.bot.plugins:
                removed = self.bot.plugins["smite"].remove_rotation_command(command)
                return web.json_response({"ok": True, "removed": removed})
            return web.json_response({"error": "Missing command"}, status=400)

        elif action == "refund_priority":
            # Manual Stripe refund from the dashboard. Real money —
            # the plugin creates the refund via Stripe's API, marks
            # the local row, and pulls any still-queued entry.
            plugin = self.bot.plugins.get("priority_request") \
                if self.bot else None
            if plugin is None:
                return web.json_response(
                    {"error": "priority_request plugin not loaded"},
                    status=400)
            session_id = (data.get("session_id") or "").strip()
            if not session_id:
                return web.json_response(
                    {"error": "session_id required"}, status=400)
            result = await plugin.refund_session(session_id)
            status = 200 if result.get("ok") else 502
            return web.json_response(result, status=status)

        elif action == "clear_suggestions":
            if "basic" in self.bot.plugins:
                count = len(self.bot.plugins["basic"]._suggestions)
                self.bot.plugins["basic"]._suggestions = []
                self.bot.plugins["basic"]._save_suggestions()
                return web.json_response({"ok": True, "cleared": count})
            return web.json_response({"ok": True, "cleared": 0})

        elif action == "start_kill_detect":
            if "killdetector" in self.bot.plugins:
                kd = self.bot.plugins["killdetector"]
                kd.reset_match_stats()
                await kd.start_detection(manual=True)
                return web.json_response({"ok": True, "status": "started"})
            return web.json_response({"error": "Kill detector not loaded"}, status=400)

        elif action == "stop_kill_detect":
            if "killdetector" in self.bot.plugins:
                kd = self.bot.plugins["killdetector"]
                await kd.stop_detection()
                return web.json_response({"ok": True, "status": "stopped"})
            return web.json_response({"error": "Kill detector not loaded"}, status=400)

        elif action == "kd_toggle_debug":
            if "killdetector" in self.bot.plugins:
                enabled = data.get("enabled", False)
                self.bot.plugins["killdetector"].set_debug(enabled)
                return web.json_response({"ok": True, "debug": enabled})
            return web.json_response({"error": "Kill detector not loaded"}, status=400)

        elif action == "kd_toggle_announce":
            if "killdetector" in self.bot.plugins:
                enabled = data.get("enabled", False)
                self.bot.plugins["killdetector"].set_announce_chat(enabled)
                return web.json_response({"ok": True, "announce_chat": enabled})
            return web.json_response({"error": "Kill detector not loaded"}, status=400)

        elif action == "test_kill":
            kill_type = data.get("kill_type", "player_kill")
            self.trigger_kill_event("kill", kill_type)
            return web.json_response({"ok": True, "kill_type": kill_type})

        elif action == "test_death":
            self.trigger_kill_event("death")
            return web.json_response({"ok": True})

        elif action == "test_tts":
            # Test TTS from the dashboard
            test_user = data.get("user", "TestViewer")
            test_msg = data.get("message", "This is a test of the highlighted message text-to-speech system!")
            self.trigger_tts(test_user, test_msg)
            return web.json_response({"ok": True})

        elif action == "test_streamloots":
            # Simulate a Streamloots card redemption (no real card spent).
            # Optional params: card, user, message, rarity.
            sl = self.bot.plugins.get("streamloots") if self.bot else None
            if not sl:
                return web.json_response(
                    {"ok": False, "error": "streamloots plugin not registered"})
            await sl.simulate_redemption(
                card_name=data.get("card", "Test Card"),
                username=data.get("user", "TestViewer"),
                message=data.get("message", ""),
                rarity=data.get("rarity", "common"),
            )
            return web.json_response({"ok": True})

        elif action == "send_chat":
            msg = data.get("message", "")
            if msg:
                await self.bot.send_chat(msg)
            return web.json_response({"ok": True})

        elif action == "god_donation":
            # MixItUp or external system reports a donation for god token awards
            username = data.get("username", "")
            amount = data.get("amount", 0)
            if username and amount > 0 and "godrequest" in self.bot.plugins:
                asyncio.create_task(
                    self.bot.plugins["godrequest"].award_donation_tokens(username, amount)
                )
            return web.json_response({"ok": True})

        elif action == "god_skip":
            if "godrequest" in self.bot.plugins:
                plugin = self.bot.plugins["godrequest"]
                if plugin.queue:
                    removed = plugin.queue.pop(0)
                    plugin._save_data()
                    plugin._save_history({
                        **removed,
                        "completed_at": datetime.now().isoformat(),
                        "status": "skipped",
                    })
                    asyncio.create_task(plugin._update_obs_display())
                    plugin._update_web_state()
            return web.json_response({"ok": True})

        elif action == "god_clear":
            if "godrequest" in self.bot.plugins:
                plugin = self.bot.plugins["godrequest"]
                plugin.queue.clear()
                plugin._save_data()
                asyncio.create_task(plugin._update_obs_display())
                plugin._update_web_state()
            return web.json_response({"ok": True})

        elif action == "sim_economy":
            # Simulate a full economy match lifecycle from the control panel
            if "economy" not in self.bot.plugins:
                return web.json_response({"error": "Economy plugin not loaded"}, status=400)
            economy = self.bot.plugins["economy"]
            god = data.get("god", "Ymir")
            outcome = data.get("outcome", "win")
            kills = int(data.get("kills", 7))
            deaths = int(data.get("deaths", 3))
            assists = int(data.get("assists", 4))
            speed = float(data.get("speed", 1.0))
            force = bool(data.get("force", False))
            # Run simulation as background task (it takes time with delays)
            async def run_sim():
                result = await economy.simulate_game(
                    god, outcome, kills, deaths, assists, speed, force=force
                )
                print(f"[Economy] Sim result: {result}")
            asyncio.create_task(run_sim())
            return web.json_response({"ok": True, "message": f"Simulating {god} {outcome}..."})

        elif action == "test_overlay":
            # Test individual economy overlay by emitting its trigger event with sample data
            if "economy" not in self.bot.plugins:
                return web.json_response({"error": "Economy plugin not loaded"}, status=400)
            economy = self.bot.plugins["economy"]
            overlay = data.get("overlay", "")

            if overlay == "dividend":
                await economy.emit_test_dividend()
            elif overlay == "leaderboard":
                await economy.emit_test_leaderboard()
            elif overlay == "portfolio":
                await economy.emit_test_portfolio()
            elif overlay == "tradefeed":
                await economy.emit_test_tradefeed()
            elif overlay == "match_end":
                await economy.emit_test_match_end()
            elif overlay == "ticker":
                await economy.emit_test_ticker()
            else:
                return web.json_response({"error": f"Unknown overlay: {overlay}"}, status=400)

            return web.json_response({"ok": True, "message": f"Triggered {overlay}"})

        elif action == "reload_prices":
            if "economy" not in self.bot.plugins:
                return web.json_response({"error": "Economy plugin not loaded"}, status=400)
            economy = self.bot.plugins["economy"]
            result = await economy.reload_prices()
            return web.json_response({"ok": True, **result})

        return web.json_response({"error": f"Unknown action: {action}"}, status=400)

    # === WEBSOCKET HANDLER ===

    async def handle_overlay_ws(self, request):
        """WebSocket endpoint for overlay manager communication."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        overlay_name = request.query.get("name", "")
        if not overlay_name:
            await ws.close(message=b"Missing overlay name")
            return ws

        self.overlay.register_ws(overlay_name, ws)
        print(f"[Overlay] WS connected: {overlay_name}")

        # If this overlay is already marked visible (e.g. always-on overlays
        # like ticker/deaths after an OBS source refresh), re-send the show
        # event with the original data so the overlay can fully restore itself
        if self.overlay._visible.get(overlay_name, False):
            cached_data = self.overlay._last_show_data.get(overlay_name, {})
            await self.overlay._send(overlay_name, "show", cached_data)
            print(f"[Overlay] Re-sent show to {overlay_name} (was already visible)")

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    # Handle messages from overlays (e.g., youtube_player reports)
                    try:
                        data = json.loads(msg.data)
                        action = data.get("data", {}).get("action")
                        if action:
                            # Forward to action handler as if it were a POST
                            await self._handle_ws_action(action, data.get("data", {}))
                    except Exception as e:
                        print(f"[Overlay] WS message error: {e}")
                elif msg.type == web.WSMsgType.ERROR:
                    print(f"[Overlay] WS error: {overlay_name} — {ws.exception()}")
        finally:
            self.overlay.unregister_ws(overlay_name, ws)
            print(f"[Overlay] WS disconnected: {overlay_name}")

        return ws

    async def _handle_ws_action(self, action, data):
        """Handle action messages received via websocket from overlays."""
        if action == "youtube_ended":
            self._state["youtube_playback"]["status"] = "idle"
            self._state["youtube_playback"]["video_id"] = None
            if self.bot and "songrequest" in self.bot.plugins:
                asyncio.create_task(
                    self.bot.plugins["songrequest"].on_youtube_ended()
                )
        elif action == "youtube_started":
            self._state["youtube_playback"]["status"] = "playing"
            if self.bot and "songrequest" in self.bot.plugins:
                asyncio.create_task(
                    self.bot.plugins["songrequest"].on_youtube_started()
                )
        elif action == "youtube_progress":
            progress_ms = data.get("progress_ms", 0)
            if self.bot and "songrequest" in self.bot.plugins:
                asyncio.create_task(
                    self.bot.plugins["songrequest"].on_youtube_progress(progress_ms)
                )

    # === OVERLAY PAGES ===

    @staticmethod
    def _no_cache_file_response(html_path):
        """Serve an HTML file with no-cache headers so OBS browser sources
        always pick up the latest version without manual cache refresh."""
        resp = web.FileResponse(html_path)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    async def handle_now_playing_overlay(self, request):
        html_path = OVERLAY_DIR / "nowplaying.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="Overlay not found", status=404)

    async def handle_youtube_player_overlay(self, request):
        html_path = OVERLAY_DIR / "youtube_player.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="YouTube player overlay not found", status=404)

    async def handle_snap_overlay(self, request):
        html_path = OVERLAY_DIR / "snap.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="Overlay not found", status=404)

    async def handle_god_overlay(self, request):
        html_path = OVERLAY_DIR / "god_overlay.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="God overlay not found", status=404)

    async def handle_spin_overlay(self, request):
        """Browser source for the !spin slot-machine animation."""
        html_path = OVERLAY_DIR / "god_pool_spin.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="Spin overlay not found", status=404)

    async def handle_sound_alerts_overlay(self, request):
        html_path = OVERLAY_DIR / "sound_alerts.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="Sound alerts overlay not found", status=404)

    async def handle_tts_overlay(self, request):
        html_path = OVERLAY_DIR / "tts.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="TTS overlay not found", status=404)

    # === DETECTOR DEBUG VIEWER ===
    # See /detector for the rendered page. The KillDeathDetector plugin
    # populates its `debug_state` dict on every scan; these endpoints just
    # surface that state to the browser. Live detection path is never
    # touched by anything reachable from here.

    def _kd_plugin(self):
        """Convenience: return the killdetector plugin or None."""
        if self.bot and "killdetector" in self.bot.plugins:
            return self.bot.plugins["killdetector"]
        return None

    @staticmethod
    def _pil_to_b64_png(img) -> str | None:
        """PIL.Image -> base64 PNG string for inline JSON delivery.
        Returns None on failure or for None input."""
        if img is None:
            return None
        try:
            import base64
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return None

    async def handle_detector_page(self, request):
        """Serve the detector debug HTML page."""
        html_path = OVERLAY_DIR / "detector.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="Detector page not found", status=404)

    async def handle_detector_debug(self, request):
        """Return the detector's current debug snapshot as JSON.

        When a VOD scan is running (tools/process_recordings.py has
        POSTed to /api/vod_processor/start), this returns the VOD
        progress state with mode="vod" instead — the /detector page
        renders a batch-progress view in that case. When the VOD scan
        finishes (or never started), falls back to the live detector
        state with mode="live".

        Crops (portrait, KDA strip, KDA binary, per-digit) are inlined
        as base64 PNGs so the page can render them with a single fetch.
        Total payload size is typically 10-30 KB.
        """
        # VOD mode takes priority — operator-driven scan in progress.
        if self._vod_state is not None:
            return web.json_response(self._vod_state)

        kd = self._kd_plugin()
        if kd is None:
            return web.json_response({"error": "killdetector_unavailable"},
                                     status=503)

        state = kd.debug_state or {}

        # Build a JSON-safe copy. Replace PIL images with base64 strings
        # so json.dumps doesn't choke. Tuples stay as lists, dicts stay
        # nested, everything else passes through.

        def _serialize(v):
            from PIL import Image as _PILImage
            if isinstance(v, _PILImage.Image):
                return self._pil_to_b64_png(v)
            if isinstance(v, dict):
                return {k: _serialize(vv) for k, vv in v.items()}
            if isinstance(v, list):
                return [_serialize(vv) for vv in v]
            if isinstance(v, tuple):
                return [_serialize(vv) for vv in v]
            return v

        payload = _serialize(state)
        # Tag the payload as live-mode so the /detector page knows to
        # render the live-detector layout. VOD scans push their own
        # state with mode="vod" via the bridge endpoints above.
        payload["mode"] = "live"
        # Expose the operator pause flag so the UI can toggle between
        # "Pause" / "Resume" labels and only enable the save-reference
        # button when scanning is frozen.
        payload["paused"] = bool(getattr(kd, "is_paused", False))
        # Mark which fields are PNG-encoded so the JS knows whether to
        # render via <img src="data:image/png;base64,..."/> or as text.
        # Pull a snapshot of the most-recent screenshot's portrait crop
        # for the "PORTRAIT" panel — extracted on demand here rather
        # than every scan because most callers won't open the page.
        try:
            img = getattr(kd, "_last_screenshot", None)
            if img is not None:
                from core.god_matcher import PORTRAIT_REGION
                x1, y1, x2, y2 = PORTRAIT_REGION
                payload["god"] = payload.get("god") or {}
                payload["god"]["portrait_crop_b64"] = self._pil_to_b64_png(
                    img.crop((x1, y1, x2, y2))
                )
        except Exception:
            pass

        return web.json_response(payload)

    async def handle_detector_fullframe(self, request):
        """Return a fresh full screenshot with the four detection regions
        drawn on top as colored rectangles. JPEG output, no cache headers.
        """
        kd = self._kd_plugin()
        if kd is None:
            return web.Response(text="killdetector unavailable", status=503)

        img = getattr(kd, "_last_screenshot", None)
        if img is None:
            return web.Response(text="no screenshot yet", status=503)

        try:
            from PIL import Image, ImageDraw
            from core.kda_reader import (
                KDA_REGION, HUD_CHECK_REGION,
                GAMEPLAY_CHECK_REGION, OVERLAY_CHECK_REGION,
            )
            from core.god_matcher import PORTRAIT_REGION

            annotated = img.convert("RGB").copy()
            d = ImageDraw.Draw(annotated)

            # Two visual tiers so the user can tell at a glance which
            # boxes are "actively being read from" vs which ones are
            # "scene classifiers that gate the read."
            #
            # Tier 1 (solid, bright, 3px): the regions whose CONTENT
            # gets parsed every frame — KDA digits, portrait histogram.
            # These are the ones that matter if they drift.
            #
            # Tier 2 (dashed, faint, 1px): the regions whose VARIANCE
            # is sampled to decide is_gameplay / overlay_open. They
            # don't need pixel-perfect alignment; the heuristics are
            # tolerant. Dashed reads as "context box" instead of
            # competing with the read regions for attention.
            primary = [
                (KDA_REGION,      "red",  "KDA"),
                (PORTRAIT_REGION, "blue", "PORTRAIT"),
            ]
            secondary = [
                (HUD_CHECK_REGION,      "lime",   "HUD"),
                (GAMEPLAY_CHECK_REGION, "cyan",   "GAMEPLAY"),
                (OVERLAY_CHECK_REGION,  "yellow", "OVERLAY-CHECK"),
            ]

            def _dashed_rect(draw, box, color, dash=12, gap=8, width=1):
                x1, y1, x2, y2 = box
                # Top + bottom edges
                for y in (y1, y2):
                    x = x1
                    while x < x2:
                        x_end = min(x + dash, x2)
                        draw.line([(x, y), (x_end, y)], fill=color, width=width)
                        x = x_end + gap
                # Left + right edges
                for x in (x1, x2):
                    y = y1
                    while y < y2:
                        y_end = min(y + dash, y2)
                        draw.line([(x, y), (x, y_end)], fill=color, width=width)
                        y = y_end + gap

            for (x1, y1, x2, y2), color, label in secondary:
                _dashed_rect(d, (x1, y1, x2, y2), color)
                d.text((x1 + 4, max(0, y1 - 14)), label, fill=color)

            # Primary regions drawn last so their solid strokes sit
            # on top of the dashed lines.
            for (x1, y1, x2, y2), color, label in primary:
                d.rectangle([x1, y1, x2, y2], outline=color, width=3)
                d.text((x1 + 4, max(0, y1 - 14)), label, fill=color)

            import io
            buf = io.BytesIO()
            # Scale down a touch — full 1920x1080 JPEG at q=80 is ~200 KB
            # which is fine, but the page only needs to display in a
            # browser at <600px wide. Save bandwidth + render time.
            annotated.save(buf, format="JPEG", quality=80, optimize=True)
            return web.Response(
                body=buf.getvalue(),
                content_type="image/jpeg",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        except Exception as e:
            return web.Response(text=f"fullframe error: {e}", status=500)

    async def handle_detector_fullframe_raw(self, request):
        """Return the latest screenshot as JPEG with NO region
        annotations baked in. The drag UI overlays HTML rectangles
        on top of this so the rectangles can be moved without
        re-rendering the source image."""
        kd = self._kd_plugin()
        if kd is None:
            return web.Response(text="killdetector unavailable", status=503)
        img = getattr(kd, "_last_screenshot", None)
        if img is None:
            return web.Response(text="no screenshot yet", status=503)
        try:
            import io
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            return web.Response(
                body=buf.getvalue(),
                content_type="image/jpeg",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        except Exception as e:
            return web.Response(text=f"fullframe_raw error: {e}", status=500)

    async def handle_detector_regions_get(self, request):
        """Return the current detection regions as JSON."""
        from core.detector_regions import load_regions, DEFAULTS
        regions = load_regions()
        # Return as plain lists (JSON-friendly) keyed by region name,
        # plus the defaults so the UI can show "reset to default".
        return web.json_response({
            "regions": {k: list(v) for k, v in regions.items()},
            "defaults": {k: list(v) for k, v in DEFAULTS.items()},
        })

    async def handle_detector_regions_post(self, request):
        """Persist updated detection regions to detector_regions.json.

        Body: {"regions": {"kda": [x1,y1,x2,y2], ...}}
        Unknown keys are silently dropped; missing keys keep the
        previously-saved value (we merge with current load).

        Note: changes take effect on NEXT bot restart. The modules
        that consume these constants (kda_reader, god_matcher) load
        them at import time; we don't hot-swap the running plugin.
        """
        from core.detector_regions import load_regions, save_regions
        try:
            body = await request.json()
        except Exception as e:
            return web.json_response(
                {"error": f"invalid JSON body: {e}"}, status=400)
        new_regions = body.get("regions") if isinstance(body, dict) else None
        if not isinstance(new_regions, dict):
            return web.json_response(
                {"error": "expected {regions: {name: [x1,y1,x2,y2], ...}}"},
                status=400)
        # Merge: start from current saved values, overlay the new keys.
        merged = {k: list(v) for k, v in load_regions().items()}
        merged.update(new_regions)
        try:
            saved = save_regions(merged)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response(
                {"error": f"save failed: {e}"}, status=500)
        return web.json_response({
            "saved": saved,
            "note": "changes take effect on next bot restart",
        })

    async def handle_detector_save_snapshot(self, request):
        """Dump the current debug state to data/detector_snapshots/<ts>/.

        Writes:
          fullframe.png    — annotated screenshot with region overlays
          portrait.png     — 80x80 portrait crop
          kda_strip.png    — 92x24 raw KDA strip
          kda_binary.png   — 8x binarized KDA strip (if available)
          state.json       — every serializable field from debug_state
        Returns JSON with the absolute path so the caller can show it
        in a toast / link.
        """
        kd = self._kd_plugin()
        if kd is None:
            return web.json_response({"error": "killdetector_unavailable"},
                                     status=503)

        try:
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%Y%m%d_%H%M%S")
            out_dir = DATA_DIR / "detector_snapshots" / ts
            out_dir.mkdir(parents=True, exist_ok=True)

            state = kd.debug_state or {}

            # Full annotated frame
            img = getattr(kd, "_last_screenshot", None)
            if img is not None:
                from PIL import ImageDraw
                from core.kda_reader import (
                    KDA_REGION, HUD_CHECK_REGION,
                    GAMEPLAY_CHECK_REGION, OVERLAY_CHECK_REGION,
                )
                from core.god_matcher import PORTRAIT_REGION
                annotated = img.convert("RGB").copy()
                d = ImageDraw.Draw(annotated)
                for region, color in [
                    (KDA_REGION, "red"),
                    (PORTRAIT_REGION, "blue"),
                    (HUD_CHECK_REGION, "lime"),
                    (GAMEPLAY_CHECK_REGION, "cyan"),
                    (OVERLAY_CHECK_REGION, "yellow"),
                ]:
                    d.rectangle(list(region), outline=color, width=3)
                annotated.save(out_dir / "fullframe.png", optimize=True)

                # Portrait crop
                try:
                    from core.god_matcher import PORTRAIT_REGION
                    px1, py1, px2, py2 = PORTRAIT_REGION
                    img.crop((px1, py1, px2, py2)).save(
                        out_dir / "portrait.png")
                except Exception:
                    pass

            # KDA crops from the with_details payload
            kda_details = (state.get("kda") or {}).get("details") or {}
            crop = kda_details.get("crop")
            if crop is not None:
                crop.save(out_dir / "kda_strip.png")
            binary = kda_details.get("binary_8x")
            if binary is not None:
                binary.save(out_dir / "kda_binary.png")

            # State JSON — stripped of any PIL images.
            def _strip_images(v):
                from PIL import Image as _PILImage
                if isinstance(v, _PILImage.Image):
                    return "<image:omitted>"
                if isinstance(v, dict):
                    return {k: _strip_images(vv) for k, vv in v.items()}
                if isinstance(v, list):
                    return [_strip_images(vv) for vv in v]
                if isinstance(v, tuple):
                    return [_strip_images(vv) for vv in v]
                return v

            (out_dir / "state.json").write_text(
                json.dumps(_strip_images(state), indent=2, default=str),
                encoding="utf-8",
            )

            return web.json_response({
                "ok": True,
                "path": str(out_dir),
                "timestamp": ts,
            })
        except Exception as e:
            return web.json_response(
                {"error": f"{type(e).__name__}: {e}"},
                status=500,
            )

    async def handle_detector_pause(self, request):
        """Freeze the live scan loop so the dashboard's displayed frame
        and any subsequent save_portrait_reference capture come from the
        exact same screenshot. The bot keeps its OBS connection open and
        the loop keeps spinning at the same cadence — it just no-ops
        every iteration until /resume is called.
        """
        kd = self._kd_plugin()
        if kd is None:
            return web.json_response(
                {"ok": False, "error": "killdetector_unavailable"},
                status=503,
            )
        try:
            info = kd.pause_scanning()
            return web.json_response({"ok": True, **info})
        except Exception as e:
            return web.json_response(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                status=500,
            )

    async def handle_detector_resume(self, request):
        """Un-freeze the live scan loop. The next loop iteration grabs
        a fresh screenshot and resumes normal per-frame processing."""
        kd = self._kd_plugin()
        if kd is None:
            return web.json_response(
                {"ok": False, "error": "killdetector_unavailable"},
                status=503,
            )
        try:
            info = kd.resume_scanning()
            return web.json_response({"ok": True, **info})
        except Exception as e:
            return web.json_response(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                status=500,
            )

    async def handle_detector_save_portrait_reference(self, request):
        """Capture the current frame's portrait region and persist it as
        a Portrait_Source/<God>.png reference. The matcher reloads
        inline so the next detector poll already benefits from the new
        fingerprint — the borderline confidence (e.g. Ymir at 0.798)
        that prompted the save usually jumps to ~1.0 on the next frame.

        Body: {"god_name": "Ymir"}
        Returns the matcher's new icon_count and a base64 preview of
        what got written so the dashboard can show a confirmation.
        """
        kd = self._kd_plugin()
        if kd is None:
            return web.json_response(
                {"ok": False, "error": "killdetector_unavailable"},
                status=503,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        god_name = (body.get("god_name") or "").strip()
        if not god_name:
            return web.json_response(
                {"ok": False, "error": "god_name is required"},
                status=400,
            )
        try:
            result = await kd.save_portrait_reference(god_name)
            status = 200 if result.get("ok") else 400
            return web.json_response(result, status=status)
        except Exception as e:
            return web.json_response(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                status=500,
            )

    # === SPIN TRIGGER (Stream Deck friendly) ===

    async def handle_spin_trigger(self, request):
        """Trigger a !spin without going through Twitch chat.

        Designed for Stream Deck: bind a button to
            http://localhost:8069/api/spin
        with the Stream Deck "Website" action (background access on).
        Or use BarRaider's HTTP Request plugin for a POST.

        Accepts both GET and POST so the simplest Stream Deck setups
        work without a third-party HTTP plugin. The endpoint is bound
        to localhost only (the dashboard webserver, not the public
        webserver), so it's not reachable from the internet.

        Responds with the same dict ``GodPoolPlugin.do_spin`` returns —
        callers can chain on the result (e.g. fire a toast, sound, etc.)
        or just ignore it.
        """
        pool = (self.bot.plugins.get("god_pool")
                if self.bot and hasattr(self.bot, "plugins") else None)
        if pool is None or not hasattr(pool, "do_spin"):
            return web.json_response(
                {"ok": False, "reason": "god_pool_unavailable"},
                status=503,
            )

        # Optional "triggered_by" override via query string or body
        # so power users can label different Stream Deck buttons
        # (e.g. one for "Stream Deck", one for "Mobile Stream Deck").
        triggered_by = request.query.get("source") or "Stream Deck"
        if request.method == "POST":
            try:
                body = await request.json()
                if isinstance(body, dict) and body.get("source"):
                    triggered_by = str(body["source"])
            except Exception:
                pass  # body optional, fall back to query / default

        try:
            result = await pool.do_spin(triggered_by=triggered_by)
        except Exception as e:
            return web.json_response(
                {"ok": False, "reason": f"exception:{type(e).__name__}:{e}"},
                status=500,
            )

        status = 200 if result.get("ok") else 409
        return web.json_response(result, status=status)

    # === VOD PROCESSOR DASHBOARD BRIDGE ===
    # tools/process_recordings.py POSTs to these endpoints as it runs.
    # The /detector page polls /api/detector_debug, which returns this
    # VOD state when a scan is active (mode="vod"). When no scan is
    # running, /api/detector_debug falls back to the live detector
    # state and /detector renders its existing live-mode layout.
    #
    # All four endpoints are tolerant of missing fields — the
    # processor pushes its best snapshot each tick; the dashboard
    # renders whatever's present.

    async def handle_vod_start(self, request):
        """Processor starting a batch run. Body is the initial state."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        import time as _t
        self._vod_state = {
            "mode": "vod",
            "state": "running",
            "started_at": _t.time(),
            "total_files": int(body.get("total_files", 0)),
            "current_file_idx": 0,
            "current_file_name": None,
            "current_file_progress": 0.0,
            "current_scan_t": 0.0,
            "current_duration": 0.0,
            "files_completed": [],
            # Rolling buffer of the most recent VodDetector log lines —
            # populated by /api/vod_processor/event. Newest-first when
            # rendered. Cap at VOD_EVENT_BUFFER_MAX so memory stays
            # bounded across long batches.
            "events": [],
            # Rolling buffer of the most recent KDA reader failures —
            # populated by /api/vod_processor/kda_failure. Each entry
            # carries the raw KDA strip + binarised diagnostic image as
            # base64 so the /detector page can render exactly what the
            # reader saw at the moment of failure. Cap at VOD_KDA_FAIL_MAX.
            "kda_failures": [],
            "elapsed_sec": 0.0,
            "eta_sec": None,
            "args": body.get("args", {}),
        }
        return web.json_response({"ok": True})

    async def handle_vod_update(self, request):
        """Per-tick progress update from inside detector.detect()."""
        if self._vod_state is None:
            await self.handle_vod_start(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        import time as _t
        s = self._vod_state
        s["current_file_idx"] = int(
            body.get("current_file_idx", s["current_file_idx"]))
        s["current_file_name"] = body.get(
            "current_file_name", s["current_file_name"])
        s["current_scan_t"] = float(body.get("current_scan_t", 0.0))
        s["current_duration"] = float(body.get("current_duration", 0.0))
        if s["current_duration"] > 0:
            s["current_file_progress"] = min(
                1.0, s["current_scan_t"] / s["current_duration"])
        else:
            s["current_file_progress"] = 0.0
        s["elapsed_sec"] = _t.time() - s["started_at"]
        n_done = len(s["files_completed"])
        if n_done > 0 and s["total_files"] > n_done:
            per_file = s["elapsed_sec"] / n_done
            remaining = s["total_files"] - n_done
            s["eta_sec"] = per_file * remaining
        return web.json_response({"ok": True})

    async def handle_vod_file_done(self, request):
        """One file finished. Append its summary to files_completed."""
        if self._vod_state is None:
            await self.handle_vod_start(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        entry = {
            "name": body.get("name", "?"),
            "status": body.get("status", "moved"),
            "god": body.get("god"),
            "kills": int(body.get("kills", 0)),
            "deaths": int(body.get("deaths", 0)),
            "assists": int(body.get("assists", 0)),
            "duration_sec": float(body.get("duration_sec", 0.0)),
            "output_path": body.get("output_path"),
            "error": body.get("error"),
        }
        self._vod_state["files_completed"].append(entry)
        return web.json_response({"ok": True})

    async def handle_vod_event(self, request):
        """Append a VodDetector log message to the events buffer."""
        VOD_EVENT_BUFFER_MAX = 200
        if self._vod_state is None:
            await self.handle_vod_start(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        import time as _t
        entry = {
            "ts": float(body.get("ts", _t.time())),
            "level": str(body.get("level", "info")),
            "msg": str(body.get("msg", "")),
            "file": body.get("file"),
        }
        events = self._vod_state.setdefault("events", [])
        events.append(entry)
        if len(events) > VOD_EVENT_BUFFER_MAX:
            del events[: len(events) - VOD_EVENT_BUFFER_MAX]
        return web.json_response({"ok": True})

    async def handle_vod_kda_failure(self, request):
        """Store a KDA reader failure (with debug crops) for the dashboard."""
        VOD_KDA_FAIL_MAX = 20
        if self._vod_state is None:
            await self.handle_vod_start(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        import time as _t
        entry = {
            "ts": float(body.get("ts", _t.time())),
            "video_t": body.get("video_t"),
            "source_resolution": body.get("source_resolution"),
            "kind": body.get("kind", "read_failure"),
            "failure_reason": body.get("failure_reason"),
            "prev_kda": body.get("prev_kda"),
            "read_kda": body.get("read_kda"),
            "groups": body.get("groups"),
            "crop_b64": body.get("crop_b64"),
            "binary_b64": body.get("binary_b64"),
            "elapsed_ms": float(body.get("elapsed_ms", 0.0)),
            "file": body.get("file"),
        }
        fails = self._vod_state.setdefault("kda_failures", [])
        fails.append(entry)
        if len(fails) > VOD_KDA_FAIL_MAX:
            del fails[: len(fails) - VOD_KDA_FAIL_MAX]
        return web.json_response({"ok": True})

    async def handle_vod_stop(self, request):
        """Processor finished a batch run (cleanly or via abort).
        Clears the VOD-mode override so the /detector page falls back
        to the live detector view on the next poll. Idempotent."""
        was = self._vod_state is not None
        self._vod_state = None
        return web.json_response({"ok": True, "was_active": was})

    def update_now_playing(self, data):
        old = self._state.get("now_playing")
        self._state["now_playing"] = data
        if data and data.get("is_playing"):
            # Detect song change (new title or was not playing)
            old_title = old.get("title") if old else None
            if old_title != data.get("title"):
                asyncio.create_task(self.overlay.emit("song_change", data))
            else:
                # Same song, just send update for progress etc.
                asyncio.create_task(self.overlay.emit("song_update", data))
        elif not data or not data.get("is_playing"):
            asyncio.create_task(self.overlay.emit("song_stopped", data))

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
        asyncio.create_task(self.overlay.emit("youtube_play", {"video_id": video_id}))

    def update_smite_state(self, data):
        """Update the Smite match/god state for the overlay."""
        old = self._state.get("smite", {})
        self._state["smite"] = data
        # Emit god_detected when god info appears
        if data.get("god") and (not old.get("god") or old["god"].get("name") != data["god"].get("name")):
            asyncio.create_task(self.overlay.emit("god_detected", data))
        # Emit match_end_confirmed when match state clears
        if old.get("in_match") and not data.get("in_match"):
            asyncio.create_task(self.overlay.emit("match_end_confirmed", data))

    def clear_youtube_playback(self):
        """Stop YouTube playback (e.g., on skip)."""
        self._state["youtube_playback"] = {
            "video_id": None,
            "status": "idle",
        }
        asyncio.create_task(self.overlay.emit("youtube_ended"))

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
        print(f"[WebServer] God Overlay:      http://{WEB_HOST}:{WEB_PORT}/overlay/god")
        print(f"[WebServer] TTS Overlay:      http://{WEB_HOST}:{WEB_PORT}/overlay/tts")
        print(f"[WebServer] Overlay WS:       ws://{WEB_HOST}:{WEB_PORT}/ws/overlays")
        # Emit bot_ready after a brief delay to let overlays connect
        asyncio.create_task(self._emit_bot_ready())

    async def _emit_bot_ready(self):
        """Emit bot_ready after a brief delay to let overlays connect via WS."""
        await asyncio.sleep(3)
        await self.overlay.emit("bot_ready")

    async def stop(self):
        await self.overlay.emit("bot_shutdown")
        await self.overlay.shutdown()
        if self.runner:
            await self.runner.cleanup()
