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
    WEB_HOST, WEB_PORT, OVERLAY_DIR, DATA_DIR,
    TITLE_TEMPLATE_GOD, TITLE_TEMPLATE_LOBBY,
    SR_PLAYLIST_AUTO_HIDE_SECONDS,
)

TTS_AUDIO_DIR = DATA_DIR / "tts_audio"
TTS_AUDIO_DIR.mkdir(exist_ok=True)


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
            "like_event": 0,  # Timestamp of last like — overlay uses this to trigger hearts
            "sound_alert": {"type": None, "timestamp": 0},  # Current sound alert for overlay
            "gamble_result_queue": [],  # Queue of gamble results for visual overlay
            "tts_queue": [],              # Queue of TTS messages for highlighted message overlay
            "kill_event_queue": [],       # Queue of kill/death events for overlay
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
        self.app.router.add_get("/overlay/kills", self.handle_kills_overlay)
        self.app.router.add_get("/api/suggestions", self.handle_get_suggestions)
        self.app.router.add_get("/api/state", self.handle_get_state)
        self.app.router.add_post("/api/state", self.handle_update_state)
        self.app.router.add_post("/api/action", self.handle_action)
        self.app.router.add_static("/overlays/", OVERLAY_DIR)

    # === API HANDLERS ===

    def trigger_like_event(self):
        """Signal the overlay that a like just happened (triggers heart animation)."""
        import time
        self._state["like_event"] = time.time()

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
        self._state["kill_event_queue"].append({
            "event": event_type,
            "kill_type": kill_type,
            "timestamp": time.time(),
        })
        if len(self._state["kill_event_queue"]) > 20:
            self._state["kill_event_queue"] = self._state["kill_event_queue"][-20:]

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

    async def handle_kills_overlay(self, request):
        """Serve the kill/death overlay HTML."""
        html_path = OVERLAY_DIR / "kills.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Kill overlay not found", status=404)

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
            # Include current title templates and command rotation for the dashboard
            if "smite" in self.bot.plugins:
                from core.config import TITLE_TEMPLATE_GOD, TITLE_TEMPLATE_LOBBY
                state["title_templates"] = {
                    "god": TITLE_TEMPLATE_GOD,
                    "lobby": TITLE_TEMPLATE_LOBBY,
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

    async def handle_god_overlay(self, request):
        html_path = OVERLAY_DIR / "god_overlay.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="God overlay not found", status=404)

    async def handle_sound_alerts_overlay(self, request):
        html_path = OVERLAY_DIR / "sound_alerts.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Sound alerts overlay not found", status=404)

    async def handle_tts_overlay(self, request):
        html_path = OVERLAY_DIR / "tts.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="TTS overlay not found", status=404)

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

    def update_smite_state(self, data):
        """Update the Smite match/god state for the overlay."""
        self._state["smite"] = data

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
        print(f"[WebServer] God Overlay:      http://{WEB_HOST}:{WEB_PORT}/overlay/god")
        print(f"[WebServer] TTS Overlay:      http://{WEB_HOST}:{WEB_PORT}/overlay/tts")
        print(f"[WebServer] Kill Overlay:     http://{WEB_HOST}:{WEB_PORT}/overlay/kills")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
