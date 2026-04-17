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
from core.overlay_manager import OverlayManager

TTS_AUDIO_DIR = DATA_DIR / "tts_audio"
VOICELINE_DIR = DATA_DIR / "smite_voicelines"
ANIMATION_DIR = DATA_DIR / "smite_animations"
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
        self.app.router.add_get("/api/suggestions", self.handle_get_suggestions)
        self.app.router.add_get("/api/state", self.handle_get_state)
        self.app.router.add_post("/api/state", self.handle_update_state)
        self.app.router.add_post("/api/action", self.handle_action)
        self.app.router.add_get("/ws/overlays", self.handle_overlay_ws)
        self.app.router.add_static("/overlays/", OVERLAY_DIR)
        # Serve god icon directories for overlays
        from core.config import CUSTOM_GOD_ICONS_DIR, GOD_ICONS_DIR
        if CUSTOM_GOD_ICONS_DIR.exists():
            self.app.router.add_static("/icons/custom/", CUSTOM_GOD_ICONS_DIR)
        if GOD_ICONS_DIR.exists():
            self.app.router.add_static("/icons/gods/", GOD_ICONS_DIR)

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

    # === CONTROL PANEL ===

    async def handle_control_panel(self, request):
        html_path = OVERLAY_DIR / "control_panel.html"
        if html_path.exists():
            return self._no_cache_file_response(html_path)
        return web.Response(text="Control panel not found", status=404)

    # === STATE MANAGEMENT ===

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
