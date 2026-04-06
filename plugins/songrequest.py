"""
Song Request Plugin
====================
Dual Spotify/YouTube song requests with queue management,
likes system, blacklist, vote-skip, and Now Playing overlay.

YouTube songs play through a separate OBS browser source
(youtube_player.html) while Spotify is paused, then Spotify
resumes automatically when the YouTube song ends.
"""

import asyncio
import json
import re
import time
import aiohttp
import base64
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from core.config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI,
    SPOTIFY_SCOPES, SR_MAX_PER_USER, SR_MAX_PER_SUB, SR_MAX_DURATION_MS,
    SR_VOTESKIP_THRESHOLD, SR_QUEUE_FILE, SR_HISTORY_FILE, SR_LIKES_FILE,
    SR_BLACKLIST_FILE, SR_STATE_FILE, DATA_DIR
)


class SongRequestPlugin:
    def __init__(self):
        self.bot = None
        self.session = None
        self.queue = []
        self.current_song = None
        self.likes = {}
        self.user_likes = {}
        self.blacklist = []           # [{key, title, artist, uri?, url?, blacklisted_by, blacklisted_at}]
        self.spotify_token = None
        self.spotify_refresh = None
        self.spotify_token_expiry = 0
        self._monitor_task = None
        self._yt_executor = ThreadPoolExecutor(max_workers=2)

        # Playback state tracking
        self._skip_on_play = None
        self._youtube_playing = False
        self._youtube_done = False
        self._youtube_started_at = 0
        self._next_pushed = False

        # Vote-skip tracking (reset each song)
        self._voteskip_users = set()

        # Spotify error resilience
        self._spotify_errors = 0
        self._spotify_connected = True
        self._spotify_notified_disconnect = False

        # Cached playback info for wait-time estimates (updated by monitor)
        self._current_remaining_ms = 0

        self._load_data()

    # === DATA PERSISTENCE ===

    def _load_data(self):
        if SR_LIKES_FILE.exists():
            with open(SR_LIKES_FILE) as f:
                data = json.load(f)
                self.likes = data.get("likes", {})
                self.user_likes = data.get("user_likes", {})

        if SR_QUEUE_FILE.exists():
            with open(SR_QUEUE_FILE) as f:
                self.queue = json.load(f)

        if SR_BLACKLIST_FILE.exists():
            with open(SR_BLACKLIST_FILE) as f:
                self.blacklist = json.load(f)

        # Restore current_song from last session
        if SR_STATE_FILE.exists():
            try:
                with open(SR_STATE_FILE) as f:
                    state = json.load(f)
                    self.current_song = state.get("current_song")
                    self._youtube_playing = state.get("youtube_playing", False)
            except Exception:
                pass

    def _save_data(self):
        with open(SR_LIKES_FILE, "w") as f:
            json.dump({"likes": self.likes, "user_likes": self.user_likes}, f, indent=2)
        with open(SR_QUEUE_FILE, "w") as f:
            json.dump(self.queue, f, indent=2)

    def _save_blacklist(self):
        with open(SR_BLACKLIST_FILE, "w") as f:
            json.dump(self.blacklist, f, indent=2)

    def _save_state(self):
        """Persist current_song so the overlay can restore after a restart."""
        state = {
            "current_song": self.current_song,
            "youtube_playing": self._youtube_playing,
        }
        with open(SR_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

    def _save_history(self, song):
        history = []
        if SR_HISTORY_FILE.exists():
            with open(SR_HISTORY_FILE) as f:
                history = json.load(f)
        history.append({**song, "played_at": datetime.now().isoformat()})
        with open(SR_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)

    @staticmethod
    def _song_key(title, artist):
        return f"{title}_{artist}".lower().strip()

    # === SETUP ===

    def setup(self, bot):
        self.bot = bot
        bot.register_command("sr", self.cmd_sr)
        bot.register_command("skip", self.cmd_skip, mod_only=True)
        bot.register_command("wrongsong", self.cmd_wrongsong)
        bot.register_command("songlist", self.cmd_songlist)
        bot.register_command("song", self.cmd_song)
        bot.register_command("like", self.cmd_like)
        bot.register_command("mysongs", self.cmd_mysongs)
        bot.register_command("toprequester", self.cmd_toprequester)
        bot.register_command("voteskip", self.cmd_voteskip)
        bot.register_command("blacklistsong", self.cmd_blacklistsong, mod_only=True)
        bot.register_command("songstatus", self.cmd_songstatus)
        bot.register_command("topsongs", self.cmd_topsongs)

    async def on_ready(self):
        self.session = aiohttp.ClientSession()
        await self._load_spotify_token()

        # If we have a restored current_song, push it to the overlay
        if self.current_song and self.bot.web_server:
            likes_key = self._song_key(self.current_song["title"], self.current_song["artist"])
            like_count = self.likes.get(likes_key, {}).get("count", 0)
            source = self.current_song.get("source", "spotify")
            self.bot.web_server.update_now_playing({
                "title": self.current_song["title"],
                "artist": self.current_song["artist"],
                "requester": self.current_song["requester"],
                "album_art": self.current_song.get("album_art"),
                "duration_ms": self.current_song.get("duration_ms", 0),
                "likes": like_count,
                "started_at": time.time(),
                "source": source,
                "is_playing": True,
                "progress_ms": 0,
                "last_updated": time.time(),
            })

        self._monitor_task = asyncio.create_task(self._playback_monitor())

    # === SPOTIFY AUTH ===

    async def _load_spotify_token(self):
        token_file = DATA_DIR / "spotify_token.json"
        if token_file.exists():
            with open(token_file) as f:
                data = json.load(f)
                self.spotify_token = data.get("access_token")
                self.spotify_refresh = data.get("refresh_token")
                self.spotify_token_expiry = data.get("expiry", 0)

        if self.spotify_token and time.time() > self.spotify_token_expiry:
            await self._refresh_spotify_token()

    async def _refresh_spotify_token(self):
        if not self.spotify_refresh:
            print("[SongRequest] No Spotify refresh token available")
            return

        auth_str = base64.b64encode(
            f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
        ).decode()

        async with self.session.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {auth_str}"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.spotify_refresh,
            }
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                self.spotify_token = data["access_token"]
                self.spotify_token_expiry = time.time() + data.get("expires_in", 3600) - 60

                token_data = {
                    "access_token": self.spotify_token,
                    "refresh_token": self.spotify_refresh,
                    "expiry": self.spotify_token_expiry,
                }
                with open(DATA_DIR / "spotify_token.json", "w") as f:
                    json.dump(token_data, f)
                print("[SongRequest] Spotify token refreshed")
            else:
                print(f"[SongRequest] Spotify refresh failed: {resp.status}")

    async def _spotify_headers(self):
        if time.time() > self.spotify_token_expiry:
            await self._refresh_spotify_token()
        return {"Authorization": f"Bearer {self.spotify_token}"}

    # === SONG SEARCH ===

    async def _search_spotify(self, query):
        headers = await self._spotify_headers()
        params = {"q": query, "type": "track", "limit": 1}
        async with self.session.get(
            "https://api.spotify.com/v1/search",
            headers=headers, params=params
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                tracks = data.get("tracks", {}).get("items", [])
                if tracks:
                    track = tracks[0]
                    return {
                        "title": track["name"],
                        "artist": ", ".join(a["name"] for a in track["artists"]),
                        "url": track["external_urls"]["spotify"],
                        "uri": track["uri"],
                        "source": "spotify",
                        "duration_ms": track["duration_ms"],
                        "album_art": track["album"]["images"][0]["url"] if track["album"]["images"] else None,
                    }
        return None

    def _yt_dlp_extract(self, query, is_url=False):
        """Run yt-dlp extraction in a thread (blocking I/O)."""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
            "socket_timeout": 15,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_term = query if is_url else f"ytsearch1:{query}"
            info = ydl.extract_info(search_term, download=False)

            if info and "entries" in info:
                entries = list(info["entries"])
                if entries:
                    return entries[0]
                return None
            return info

    async def _search_youtube(self, query):
        try:
            loop = asyncio.get_running_loop()
            data = await asyncio.wait_for(
                loop.run_in_executor(self._yt_executor, self._yt_dlp_extract, query, False),
                timeout=20,
            )
            if data:
                return {
                    "title": data.get("title", "Unknown"),
                    "artist": data.get("uploader", data.get("channel", "Unknown")),
                    "url": data.get("webpage_url", ""),
                    "source": "youtube",
                    "duration_ms": (data.get("duration", 0) or 0) * 1000,
                    "album_art": data.get("thumbnail", None),
                }
        except Exception as e:
            print(f"[SongRequest] YouTube search error: {e}")
        return None

    def _is_spotify_url(self, text):
        return "open.spotify.com" in text or "spotify:" in text

    def _is_youtube_url(self, text):
        return "youtube.com" in text or "youtu.be" in text

    async def _resolve_spotify_url(self, url):
        match = re.search(r"track/([a-zA-Z0-9]+)", url)
        if not match:
            return None
        track_id = match.group(1)

        headers = await self._spotify_headers()
        async with self.session.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers=headers
        ) as resp:
            if resp.status == 200:
                track = await resp.json()
                return {
                    "title": track["name"],
                    "artist": ", ".join(a["name"] for a in track["artists"]),
                    "url": track["external_urls"]["spotify"],
                    "uri": track["uri"],
                    "source": "spotify",
                    "duration_ms": track["duration_ms"],
                    "album_art": track["album"]["images"][0]["url"] if track["album"]["images"] else None,
                }
        return None

    async def _resolve_youtube_url(self, url):
        try:
            loop = asyncio.get_running_loop()
            data = await asyncio.wait_for(
                loop.run_in_executor(self._yt_executor, self._yt_dlp_extract, url, True),
                timeout=20,
            )
            if data:
                return {
                    "title": data.get("title", "Unknown"),
                    "artist": data.get("uploader", data.get("channel", "Unknown")),
                    "url": data.get("webpage_url", url),
                    "source": "youtube",
                    "duration_ms": (data.get("duration", 0) or 0) * 1000,
                    "album_art": data.get("thumbnail", None),
                }
        except Exception as e:
            print(f"[SongRequest] YouTube resolve error: {e}")
        return None

    # === YOUTUBE HELPERS ===

    def _extract_youtube_id(self, url):
        """Extract the 11-character video ID from a YouTube URL."""
        match = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', url or "")
        return match.group(1) if match else None

    async def _start_youtube_playback(self, song):
        """Begin playing a YouTube song through the YouTube player overlay."""
        video_id = self._extract_youtube_id(song.get("url", ""))
        if not video_id:
            print(f"[SongRequest] Could not extract YouTube video ID from: {song.get('url')}")
            if self.queue and self.queue[0] is song:
                removed = self.queue.pop(0)
                self._save_data()
                if self.bot.web_server:
                    self.bot.web_server.update_queue(
                        [{"title": s["title"], "artist": s["artist"],
                          "requester": s["requester"]} for s in self.queue]
                    )
                await self.bot.send_chat(
                    f"Could not play: {removed['title']} (invalid YouTube URL) - skipping"
                )
            return

        await self._pause_spotify()

        if self.queue and self.queue[0] is song:
            self.current_song = self.queue.pop(0)
        else:
            self.current_song = song
        self._save_data()
        self._save_state()
        self._save_history(self.current_song)
        self._voteskip_users.clear()

        self._youtube_playing = True
        self._youtube_done = False
        self._youtube_started_at = time.time()

        if self.bot.web_server:
            self.bot.web_server.set_youtube_playback(video_id)

            likes_key = self._song_key(self.current_song["title"], self.current_song["artist"])
            like_count = self.likes.get(likes_key, {}).get("count", 0)

            self.bot.web_server.update_now_playing({
                "title": self.current_song["title"],
                "artist": self.current_song["artist"],
                "requester": self.current_song["requester"],
                "album_art": self.current_song.get("album_art"),
                "duration_ms": self.current_song.get("duration_ms", 0),
                "likes": like_count,
                "started_at": time.time(),
                "source": "youtube",
                "is_playing": True,
                "progress_ms": 0,
                "last_updated": time.time(),
            })

            self.bot.web_server.update_queue(
                [{"title": s["title"], "artist": s["artist"],
                  "requester": s["requester"]} for s in self.queue]
            )

        likes_key = self._song_key(self.current_song["title"], self.current_song["artist"])
        like_count = self.likes.get(likes_key, {}).get("count", 0)

        await self.bot.send_chat(
            f"♫ Now playing: {self.current_song['title']} by "
            f"{self.current_song['artist']} | "
            f"Requested by: {self.current_song['requester']} | ♡ {like_count}"
        )

    async def on_youtube_ended(self):
        self._youtube_done = True

    async def on_youtube_started(self):
        self._youtube_started_at = time.time()
        if self.bot.web_server:
            now_playing = self.bot.web_server._state.get("now_playing")
            if now_playing:
                now_playing["started_at"] = time.time()
                now_playing["is_playing"] = True
                now_playing["last_updated"] = time.time()

    async def on_youtube_progress(self, progress_ms):
        if self.bot.web_server and self._youtube_playing:
            now_playing = self.bot.web_server._state.get("now_playing")
            if now_playing:
                now_playing["progress_ms"] = progress_ms
                now_playing["is_playing"] = True
                now_playing["last_updated"] = time.time()
            # Cache remaining time for wait estimates
            if self.current_song:
                duration = self.current_song.get("duration_ms", 0)
                self._current_remaining_ms = max(0, duration - progress_ms)

    # === SPOTIFY PLAYBACK CONTROL ===

    async def _add_to_spotify_queue(self, uri):
        headers = await self._spotify_headers()
        async with self.session.post(
            f"https://api.spotify.com/v1/me/player/queue?uri={uri}",
            headers=headers
        ) as resp:
            return resp.status in (200, 204)

    async def _get_current_playback(self):
        headers = await self._spotify_headers()
        async with self.session.get(
            "https://api.spotify.com/v1/me/player/currently-playing",
            headers=headers
        ) as resp:
            if resp.status == 200:
                return await resp.json()
        return None

    async def _skip_track(self):
        headers = await self._spotify_headers()
        async with self.session.post(
            "https://api.spotify.com/v1/me/player/next",
            headers=headers
        ) as resp:
            return resp.status in (200, 204)

    async def _pause_spotify(self):
        headers = await self._spotify_headers()
        async with self.session.put(
            "https://api.spotify.com/v1/me/player/pause",
            headers=headers
        ) as resp:
            return resp.status in (200, 204)

    async def _resume_spotify(self):
        headers = await self._spotify_headers()
        async with self.session.put(
            "https://api.spotify.com/v1/me/player/play",
            headers=headers
        ) as resp:
            return resp.status in (200, 204)

    # === VALIDATION HELPERS ===

    def _is_blacklisted(self, song):
        """Check if a song is on the blacklist."""
        key = self._song_key(song["title"], song["artist"])
        song_uri = song.get("uri", "")
        song_url = song.get("url", "")
        yt_id = self._extract_youtube_id(song_url) if song.get("source") == "youtube" else None

        for entry in self.blacklist:
            if entry["key"] == key:
                return True
            if song_uri and entry.get("uri") == song_uri:
                return True
            if yt_id and entry.get("youtube_id") == yt_id:
                return True
        return False

    def _is_duplicate_in_queue(self, song):
        """Check if a song with the same title+artist is already in the queue."""
        key = self._song_key(song["title"], song["artist"])
        for queued in self.queue:
            if self._song_key(queued["title"], queued["artist"]) == key:
                return True
        return False

    def _estimate_wait_ms(self):
        """Estimate wait time for the last song in queue using cached playback data."""
        wait_ms = sum(s.get("duration_ms", 0) for s in self.queue[:-1])
        wait_ms += self._current_remaining_ms
        return wait_ms

    # === COMMANDS ===

    async def cmd_sr(self, message, args, whisper=False):
        if not self.bot.is_feature_enabled("song_requests"):
            await self.bot.send_reply(message, "Song requests are currently closed.", whisper)
            return

        if not args:
            await self.bot.send_reply(message, "Usage: !sr <song name or URL>", whisper)
            return

        username = message.chatter.name.lower()
        is_sub = hasattr(message.chatter, "subscriber") and message.chatter.subscriber
        max_songs = SR_MAX_PER_SUB if is_sub else SR_MAX_PER_USER

        user_in_queue = sum(1 for s in self.queue if s.get("requester", "").lower() == username)
        if user_in_queue >= max_songs:
            await self.bot.send_reply(
                message,
                f"You already have {user_in_queue} songs in the queue (max: {max_songs}).",
                whisper
            )
            return

        # Resolve the song
        song = None
        if self._is_spotify_url(args):
            song = await self._resolve_spotify_url(args)
        elif self._is_youtube_url(args):
            song = await self._resolve_youtube_url(args)
        else:
            song = await self._search_spotify(args)
            if not song:
                song = await self._search_youtube(args)

        if not song:
            await self.bot.send_reply(message, "Couldn't find that song.", whisper)
            return

        # --- Validation checks ---

        # Duration cap
        duration_ms = song.get("duration_ms", 0)
        if duration_ms > SR_MAX_DURATION_MS:
            max_min = SR_MAX_DURATION_MS // 60000
            song_min = duration_ms // 60000
            await self.bot.send_reply(
                message,
                f"That song is {song_min} min long (max: {max_min} min).",
                whisper
            )
            return

        # Blacklist check
        if self._is_blacklisted(song):
            await self.bot.send_reply(message, "That song is blacklisted.", whisper)
            return

        # Duplicate check (only checks current queue, not history)
        if self._is_duplicate_in_queue(song):
            await self.bot.send_reply(
                message, f"{song['title']} is already in the queue.", whisper
            )
            return

        # --- Add to queue ---
        song["requester"] = message.chatter.name
        song["requester_id"] = str(message.chatter.id) if hasattr(message.chatter, "id") else ""
        song["requested_at"] = datetime.now().isoformat()

        self.queue.append(song)
        self._save_data()

        # Smart wait time: queue durations + remaining time on current song
        position = len(self.queue)
        wait_ms = self._estimate_wait_ms()
        wait_min = max(1, round(wait_ms / 60000)) if wait_ms > 30000 else 0

        source_label = "Spotify" if song["source"] == "spotify" else "YouTube"
        wait_text = f"~{wait_min} min wait" if wait_min > 0 else "up next"
        await self.bot.send_reply(
            message,
            f"Added: {song['title']} by {song['artist']} [{source_label}] | "
            f"Position: #{position} | {wait_text}",
            whisper
        )

        if self.bot.web_server:
            self.bot.web_server.update_queue(
                [{"title": s["title"], "artist": s["artist"],
                  "requester": s["requester"]} for s in self.queue]
            )

    async def cmd_skip(self, message, args, whisper=False):
        if self.current_song or self._youtube_playing:
            await self.skip_current()
            await self.bot.send_reply(message, "Song skipped.", whisper)
        elif self.queue:
            next_song = self.queue[0]
            source_label = "YouTube" if next_song["source"] == "youtube" else "Spotify"
            if next_song["source"] == "youtube":
                await self._start_youtube_playback(next_song)
            elif next_song["source"] == "spotify" and next_song.get("uri"):
                await self._add_to_spotify_queue(next_song["uri"])
                next_song["pushed_to_spotify"] = True
                await self._skip_track()
            await self.bot.send_reply(
                message,
                f"Skipped to: {next_song['title']} by {next_song['artist']} [{source_label}]",
                whisper
            )
        else:
            await self._skip_track()
            await self.bot.send_reply(message, "Playlist song skipped.", whisper)

    async def _transition_to_next(self):
        """After a song ends, transition to whatever is next."""
        if self.queue:
            next_song = self.queue[0]
            if next_song["source"] == "youtube":
                await self._start_youtube_playback(next_song)
                return
            elif next_song["source"] == "spotify" and next_song.get("uri"):
                await self._add_to_spotify_queue(next_song["uri"])
                next_song["pushed_to_spotify"] = True
                await self._skip_track()
                if self.bot.web_server:
                    self.bot.web_server.update_now_playing(None)
                return

        await self._resume_spotify()
        if self.bot.web_server:
            self.bot.web_server.update_now_playing(None)

    async def skip_current(self):
        """Skip the current song, whether Spotify or YouTube."""
        if not self.current_song:
            return

        was_youtube = self._youtube_playing

        if self._youtube_playing:
            if self.bot.web_server:
                self.bot.web_server.clear_youtube_playback()
            self._youtube_playing = False
            self._youtube_done = False
        else:
            await self._skip_track()

        self.current_song = None
        self._next_pushed = False
        self._voteskip_users.clear()
        self._save_state()

        if self.bot.web_server:
            self.bot.web_server.update_now_playing(None)
            self.bot.web_server.update_queue(
                [{"title": s["title"], "artist": s["artist"],
                  "requester": s["requester"]} for s in self.queue]
            )

        if was_youtube:
            await self._transition_to_next()

    async def cmd_wrongsong(self, message, args, whisper=False):
        username = message.chatter.name.lower()
        for i in range(len(self.queue) - 1, -1, -1):
            if self.queue[i].get("requester", "").lower() == username:
                removed = self.queue.pop(i)
                self._save_data()

                if self.bot.web_server:
                    self.bot.web_server.update_queue(
                        [{"title": s["title"], "artist": s["artist"],
                          "requester": s["requester"]} for s in self.queue]
                    )

                if removed.get("pushed_to_spotify"):
                    self._skip_on_play = removed.get("uri") or removed.get("title", "").lower()
                    await self.bot.send_reply(
                        message,
                        f"Removed: {removed['title']} by {removed['artist']} "
                        f"(will auto-skip if it plays)",
                        whisper
                    )
                else:
                    await self.bot.send_reply(
                        message,
                        f"Removed: {removed['title']} by {removed['artist']}",
                        whisper
                    )
                return
        await self.bot.send_reply(message, "No songs found to remove.", whisper)

    async def cmd_songlist(self, message, args, whisper=False):
        if not self.queue:
            await self.bot.send_reply(message, "The queue is empty.", whisper)
            return

        songs = []
        for i, s in enumerate(self.queue[:5]):
            source_tag = "YT" if s.get("source") == "youtube" else "SP"
            songs.append(f"{i+1}. {s['title']} [{source_tag}] ({s['requester']})")

        remaining = len(self.queue) - 5
        text = " | ".join(songs)
        if remaining > 0:
            text += f" | +{remaining} more"
        await self.bot.send_reply(message, text, whisper)

    async def cmd_song(self, message, args, whisper=False):
        if self.current_song:
            likes_key = self._song_key(self.current_song["title"], self.current_song["artist"])
            like_count = self.likes.get(likes_key, {}).get("count", 0)
            source_label = "YouTube" if self.current_song.get("source") == "youtube" else "Spotify"
            await self.bot.send_reply(
                message,
                f"Now playing: {self.current_song['title']} by "
                f"{self.current_song['artist']} [{source_label}] | Requested by: "
                f"{self.current_song.get('requester', 'playlist')} | "
                f"♡ {like_count}",
                whisper
            )
        else:
            playback = await self._get_current_playback()
            if playback and playback.get("item"):
                track = playback["item"]
                title = track["name"]
                artist = ", ".join(a["name"] for a in track["artists"])
                await self.bot.send_reply(
                    message, f"Now playing: {title} by {artist} (playlist)", whisper
                )
            else:
                await self.bot.send_reply(message, "Nothing is playing right now.", whisper)

    async def cmd_like(self, message, args, whisper=False):
        if not self.current_song:
            await self.bot.send_reply(message, "No requested song is playing right now.", whisper)
            return

        username = message.chatter.name.lower()
        likes_key = self._song_key(self.current_song["title"], self.current_song["artist"])

        if likes_key not in self.likes:
            self.likes[likes_key] = {
                "count": 0, "users": [],
                "requester": self.current_song.get("requester", ""),
                "title": self.current_song["title"],
                "artist": self.current_song["artist"],
            }

        if username in self.likes[likes_key]["users"]:
            await self.bot.send_reply(message, "You already liked this song!", whisper)
            return

        self.likes[likes_key]["count"] += 1
        self.likes[likes_key]["users"].append(username)

        requester = self.current_song.get("requester", "").lower()
        if requester:
            self.user_likes[requester] = self.user_likes.get(requester, 0) + 1

        self._save_data()

        count = self.likes[likes_key]["count"]
        await self.bot.send_chat(
            f"♡ {message.chatter.name} liked this song! ({count} total likes)"
        )

        if self.bot.web_server:
            now_playing = self.bot.web_server._state.get("now_playing", {})
            if now_playing:
                now_playing["likes"] = count
                self.bot.web_server.update_now_playing(now_playing)

    async def cmd_mysongs(self, message, args, whisper=False):
        username = message.chatter.name.lower()
        total_likes = self.user_likes.get(username, 0)

        best_song = None
        best_count = 0
        for key, data in self.likes.items():
            if data.get("requester", "").lower() == username:
                if data["count"] > best_count:
                    best_count = data["count"]
                    best_song = data

        if best_song:
            await self.bot.send_reply(
                message,
                f"You have {total_likes} total likes! Most liked: "
                f"{best_song['title']} by {best_song['artist']} ({best_count} likes)",
                whisper
            )
        else:
            await self.bot.send_reply(
                message, f"You have {total_likes} total likes!", whisper
            )

    async def cmd_toprequester(self, message, args, whisper=False):
        if not self.user_likes:
            await self.bot.send_reply(message, "No likes yet!", whisper)
            return

        top = sorted(self.user_likes.items(), key=lambda x: x[1], reverse=True)[:3]
        leaderboard = " | ".join(
            f"{i+1}. {name}: {count} likes" for i, (name, count) in enumerate(top)
        )
        await self.bot.send_reply(message, f"Top requesters: {leaderboard}", whisper)

    # === NEW COMMANDS ===

    async def cmd_voteskip(self, message, args, whisper=False):
        if not self.current_song:
            await self.bot.send_reply(message, "No requested song is playing.", whisper)
            return

        username = message.chatter.name.lower()
        if username in self._voteskip_users:
            await self.bot.send_reply(message, "You already voted to skip.", whisper)
            return

        self._voteskip_users.add(username)
        votes = len(self._voteskip_users)
        needed = SR_VOTESKIP_THRESHOLD

        if votes >= needed:
            await self.bot.send_chat(
                f"Vote skip passed ({votes}/{needed})! Skipping: "
                f"{self.current_song['title']}"
            )
            await self.skip_current()
        else:
            await self.bot.send_chat(
                f"♫ {message.chatter.name} voted to skip ({votes}/{needed})"
            )

    async def cmd_blacklistsong(self, message, args, whisper=False):
        # If args provided, treat as a search to blacklist a specific song
        # If no args, blacklist whatever is currently playing
        song = None
        if args:
            # Allow mods to blacklist by name
            song = await self._search_spotify(args)
            if not song:
                song = await self._search_youtube(args)
        elif self.current_song:
            song = self.current_song
        else:
            # Try to get current Spotify playback
            playback = await self._get_current_playback()
            if playback and playback.get("item"):
                track = playback["item"]
                song = {
                    "title": track["name"],
                    "artist": ", ".join(a["name"] for a in track["artists"]),
                    "uri": track.get("uri", ""),
                    "url": "",
                    "source": "spotify",
                }

        if not song:
            await self.bot.send_reply(message, "No song to blacklist.", whisper)
            return

        key = self._song_key(song["title"], song["artist"])

        # Check if already blacklisted
        if any(e["key"] == key for e in self.blacklist):
            await self.bot.send_reply(
                message, f"{song['title']} is already blacklisted.", whisper
            )
            return

        entry = {
            "key": key,
            "title": song["title"],
            "artist": song["artist"],
            "blacklisted_by": message.chatter.name,
            "blacklisted_at": datetime.now().isoformat(),
        }
        if song.get("uri"):
            entry["uri"] = song["uri"]
        if song.get("source") == "youtube" and song.get("url"):
            yt_id = self._extract_youtube_id(song["url"])
            if yt_id:
                entry["youtube_id"] = yt_id

        self.blacklist.append(entry)
        self._save_blacklist()

        await self.bot.send_chat(
            f"🚫 {song['title']} by {song['artist']} has been blacklisted."
        )

        # If this song is currently playing, skip it
        if (self.current_song and
                self._song_key(self.current_song["title"], self.current_song["artist"]) == key):
            await self.skip_current()

    async def cmd_songstatus(self, message, args, whisper=False):
        username = message.chatter.name.lower()

        # Find all of this user's songs in queue
        user_songs = []
        for i, s in enumerate(self.queue):
            if s.get("requester", "").lower() == username:
                # Calculate wait: sum of durations ahead of this position + current remaining
                wait_ms = sum(q.get("duration_ms", 0) for q in self.queue[:i])
                wait_ms += self._current_remaining_ms
                wait_min = max(1, round(wait_ms / 60000)) if wait_ms > 30000 else 0
                wait_text = f"~{wait_min} min" if wait_min > 0 else "up next"
                user_songs.append(f"#{i+1} {s['title']} ({wait_text})")

        if not user_songs:
            await self.bot.send_reply(message, "You have no songs in the queue.", whisper)
            return

        await self.bot.send_reply(
            message, "Your songs: " + " | ".join(user_songs), whisper
        )

    async def cmd_topsongs(self, message, args, whisper=False):
        if not self.likes:
            await self.bot.send_reply(message, "No likes yet!", whisper)
            return

        # Sort all songs by like count
        sorted_songs = sorted(
            self.likes.values(),
            key=lambda x: x.get("count", 0),
            reverse=True
        )[:5]

        leaderboard = " | ".join(
            f"{i+1}. {s['title']} by {s['artist']} ({s['count']} likes)"
            for i, s in enumerate(sorted_songs) if s.get("count", 0) > 0
        )

        if leaderboard:
            await self.bot.send_reply(message, f"Top songs: {leaderboard}", whisper)
        else:
            await self.bot.send_reply(message, "No likes yet!", whisper)

    # === PLAYBACK MONITOR ===

    async def _playback_monitor(self):
        """
        Monitor Spotify playback and coordinate YouTube playback.
        Includes error resilience with backoff and reconnection notifications.
        """
        last_track_id = None

        while True:
            try:
                if not self.bot.is_feature_enabled("song_requests"):
                    await asyncio.sleep(5)
                    continue

                # === YOUTUBE MODE ===
                if self._youtube_playing:
                    if self._youtube_done:
                        self._youtube_done = False
                        self._youtube_playing = False
                        self.current_song = None
                        self._voteskip_users.clear()
                        self._save_state()

                        if self.bot.web_server:
                            self.bot.web_server.clear_youtube_playback()

                        await self._transition_to_next()

                    else:
                        if (self._youtube_started_at and
                                time.time() - self._youtube_started_at > 60):
                            yt_state = {}
                            if self.bot.web_server:
                                yt_state = self.bot.web_server._state.get("youtube_playback", {})
                            if yt_state.get("status") != "playing":
                                print("[SongRequest] YouTube player timeout - skipping")
                                self._youtube_done = True
                                continue

                    await asyncio.sleep(1)
                    continue

                # === SPOTIFY MODE ===
                playback = await self._get_current_playback()

                # --- Spotify error resilience ---
                if playback is None:
                    self._spotify_errors += 1
                    if self._spotify_errors >= 3 and not self._spotify_notified_disconnect:
                        self._spotify_connected = False
                        self._spotify_notified_disconnect = True
                        print("[SongRequest] Spotify connection lost")
                        await self.bot.send_chat(
                            "⚠ Spotify connection lost — song requests paused until reconnected."
                        )
                    # Backoff: poll slower when disconnected
                    sleep_time = 15 if self._spotify_errors >= 3 else 3
                    # Still check for YouTube songs even without Spotify
                    if self.queue and self.queue[0]["source"] == "youtube":
                        await self._start_youtube_playback(self.queue[0])
                    await asyncio.sleep(sleep_time)
                    continue

                # Connection restored
                if not self._spotify_connected:
                    self._spotify_connected = True
                    self._spotify_notified_disconnect = False
                    print("[SongRequest] Spotify connection restored")
                    await self.bot.send_chat("✓ Spotify reconnected — song requests are back!")
                self._spotify_errors = 0

                if not playback.get("item"):
                    if self.queue and self.queue[0]["source"] == "youtube":
                        await self._start_youtube_playback(self.queue[0])
                    await asyncio.sleep(3)
                    continue

                track = playback["item"]
                track_id = track.get("id") or track.get("uri")
                track_uri = track.get("uri", "")
                progress_ms = playback.get("progress_ms", 0)
                duration_ms = track.get("duration_ms", 0)
                remaining_ms = duration_ms - progress_ms
                is_playing = playback.get("is_playing", False)

                # Cache remaining time for wait estimates
                if self.current_song:
                    self._current_remaining_ms = max(0, remaining_ms)
                else:
                    # Playlist song — still useful for first-in-queue wait estimate
                    self._current_remaining_ms = max(0, remaining_ms)

                # Update overlay progress
                if self.bot.web_server and self.current_song:
                    now_playing = self.bot.web_server._state.get("now_playing")
                    if now_playing:
                        now_playing["progress_ms"] = progress_ms
                        now_playing["is_playing"] = is_playing
                        now_playing["last_updated"] = time.time()

                # --- Detect track change ---
                if track_id != last_track_id:
                    last_track_id = track_id
                    self._next_pushed = False

                    if self._skip_on_play:
                        if (track_uri == self._skip_on_play or
                                track["name"].lower() == self._skip_on_play):
                            print(f"[SongRequest] Auto-skipping removed song: {track['name']}")
                            await self._skip_track()
                            self._skip_on_play = None
                            continue

                    await self._on_track_change(track, playback)

                # --- Smart queue pushing: 30 seconds before end ---
                if (remaining_ms < 30000 and remaining_ms > 0
                        and not self._next_pushed and self.queue):
                    next_song = self.queue[0]
                    if next_song["source"] == "spotify" and next_song.get("uri"):
                        success = await self._add_to_spotify_queue(next_song["uri"])
                        if success:
                            next_song["pushed_to_spotify"] = True
                            self._next_pushed = True
                            print(f"[SongRequest] Queued next: {next_song['title']}")
                    elif next_song["source"] == "youtube":
                        self._next_pushed = True

            except Exception as e:
                print(f"[SongRequest Monitor] Error: {e}")
                self._spotify_errors += 1

            await asyncio.sleep(3)

    async def _on_track_change(self, track, playback=None):
        """Called when the current Spotify track changes."""
        track_name = track["name"].lower()
        track_uri = track.get("uri", "")

        if self.queue:
            queued = self.queue[0]

            if queued["source"] == "youtube":
                await self._pause_spotify()
                await self._start_youtube_playback(queued)
                return

            if (queued["source"] == "spotify" and
                (queued.get("uri") == track_uri or
                 queued["title"].lower() in track_name)):

                self.current_song = self.queue.pop(0)
                self._save_data()
                self._save_state()
                self._save_history(self.current_song)
                self._voteskip_users.clear()

                likes_key = self._song_key(self.current_song["title"], self.current_song["artist"])
                like_count = self.likes.get(likes_key, {}).get("count", 0)

                await self.bot.send_chat(
                    f"♫ Now playing: {self.current_song['title']} by "
                    f"{self.current_song['artist']} | "
                    f"Requested by: {self.current_song['requester']} | ♡ {like_count}"
                )

                if self.bot.web_server:
                    progress_ms = playback.get("progress_ms", 0) if playback else 0
                    is_playing = playback.get("is_playing", True) if playback else True

                    self.bot.web_server.update_now_playing({
                        "title": self.current_song["title"],
                        "artist": self.current_song["artist"],
                        "requester": self.current_song["requester"],
                        "album_art": self.current_song.get("album_art"),
                        "duration_ms": self.current_song.get("duration_ms", 0),
                        "likes": like_count,
                        "started_at": time.time(),
                        "source": "spotify",
                        "progress_ms": progress_ms,
                        "is_playing": is_playing,
                        "last_updated": time.time(),
                    })

                    self.bot.web_server.update_queue(
                        [{"title": s["title"], "artist": s["artist"],
                          "requester": s["requester"]} for s in self.queue]
                    )
            else:
                if self.current_song:
                    self.current_song = None
                    self._voteskip_users.clear()
                    self._save_state()
                    if self.bot.web_server:
                        self.bot.web_server.update_now_playing(None)
        else:
            if self.current_song:
                self.current_song = None
                self._voteskip_users.clear()
                self._save_state()
                if self.bot.web_server:
                    self.bot.web_server.update_now_playing(None)

    async def cleanup(self):
        if self._monitor_task:
            self._monitor_task.cancel()
        if self.session:
            await self.session.close()
        self._yt_executor.shutdown(wait=False)
        self._save_state()
