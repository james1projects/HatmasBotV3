"""
Song Request Plugin
====================
Dual Spotify/YouTube song requests with queue management,
likes system, and Now Playing overlay integration.
"""

import asyncio
import json
import re
import time
import aiohttp
import base64
from datetime import datetime
from pathlib import Path

from core.config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI,
    SPOTIFY_SCOPES, SR_MAX_PER_USER, SR_MAX_PER_SUB,
    SR_QUEUE_FILE, SR_HISTORY_FILE, SR_LIKES_FILE, DATA_DIR
)


class SongRequestPlugin:
    def __init__(self):
        self.bot = None
        self.session = None
        self.queue = []  # [{title, artist, url, source, requester, requester_id, duration_ms, album_art}]
        self.current_song = None
        self.likes = {}  # {song_key: {count, users, requester, title, artist}}
        self.user_likes = {}  # {username: total_likes_received}
        self.spotify_token = None
        self.spotify_refresh = None
        self.spotify_token_expiry = 0
        self._monitor_task = None
        self._load_data()

    def _load_data(self):
        if SR_LIKES_FILE.exists():
            with open(SR_LIKES_FILE) as f:
                data = json.load(f)
                self.likes = data.get("likes", {})
                self.user_likes = data.get("user_likes", {})

        if SR_QUEUE_FILE.exists():
            with open(SR_QUEUE_FILE) as f:
                self.queue = json.load(f)

    def _save_data(self):
        with open(SR_LIKES_FILE, "w") as f:
            json.dump({"likes": self.likes, "user_likes": self.user_likes}, f, indent=2)
        with open(SR_QUEUE_FILE, "w") as f:
            json.dump(self.queue, f, indent=2)

    def _save_history(self, song):
        history = []
        if SR_HISTORY_FILE.exists():
            with open(SR_HISTORY_FILE) as f:
                history = json.load(f)
        history.append({**song, "played_at": datetime.now().isoformat()})
        with open(SR_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)

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

    async def on_ready(self):
        self.session = aiohttp.ClientSession()
        await self._load_spotify_token()
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

    async def _search_youtube(self, query):
        # Use yt-dlp for YouTube search (no API key needed)
        try:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "--dump-json", "--no-download",
                f"ytsearch1:{query}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            if stdout:
                data = json.loads(stdout.decode())
                return {
                    "title": data.get("title", "Unknown"),
                    "artist": data.get("uploader", "Unknown"),
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
        # Extract track ID from URL
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
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "--dump-json", "--no-download", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            if stdout:
                data = json.loads(stdout.decode())
                return {
                    "title": data.get("title", "Unknown"),
                    "artist": data.get("uploader", "Unknown"),
                    "url": url,
                    "source": "youtube",
                    "duration_ms": (data.get("duration", 0) or 0) * 1000,
                    "album_art": data.get("thumbnail", None),
                }
        except Exception as e:
            print(f"[SongRequest] YouTube resolve error: {e}")
        return None

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

        # Check queue limit
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
            # Search Spotify first, then YouTube
            song = await self._search_spotify(args)
            if not song:
                song = await self._search_youtube(args)

        if not song:
            await self.bot.send_reply(message, "Couldn't find that song.", whisper)
            return

        # Add requester info
        song["requester"] = message.chatter.name
        song["requester_id"] = str(message.chatter.id) if hasattr(message.chatter, "id") else ""
        song["requested_at"] = datetime.now().isoformat()

        # Add to internal queue only - NOT to Spotify yet
        # Spotify queue addition happens in the monitor when timing is right
        self.queue.append(song)
        self._save_data()

        # Calculate estimated wait
        position = len(self.queue)
        wait_ms = sum(s.get("duration_ms", 0) for s in self.queue[:-1])
        wait_min = int(wait_ms / 60000)

        await self.bot.send_reply(
            message,
            f"Added: {song['title']} by {song['artist']} | "
            f"Position: #{position} | ~{wait_min} min wait",
            whisper
        )

        # Update web server state
        if self.bot.web_server:
            self.bot.web_server.update_queue(
                [{"title": s["title"], "artist": s["artist"],
                  "requester": s["requester"]} for s in self.queue]
            )

    async def cmd_skip(self, message, args, whisper=False):
        await self.skip_current()
        await self.bot.send_reply(message, "Song skipped.", whisper)

    async def skip_current(self):
        if self.current_song:
            if self.current_song["source"] == "spotify":
                await self._skip_track()
            self.current_song = None

    async def cmd_wrongsong(self, message, args, whisper=False):
        username = message.chatter.name.lower()
        # Find the last song this user requested
        for i in range(len(self.queue) - 1, -1, -1):
            if self.queue[i].get("requester", "").lower() == username:
                removed = self.queue.pop(i)
                self._save_data()

                # If this song was already pushed to Spotify queue, mark it for auto-skip
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
            songs.append(f"{i+1}. {s['title']} ({s['requester']})")

        remaining = len(self.queue) - 5
        text = " | ".join(songs)
        if remaining > 0:
            text += f" | +{remaining} more"
        await self.bot.send_reply(message, text, whisper)

    async def cmd_song(self, message, args, whisper=False):
        if self.current_song:
            likes_key = f"{self.current_song['title']}_{self.current_song['artist']}".lower()
            like_count = self.likes.get(likes_key, {}).get("count", 0)
            await self.bot.send_reply(
                message,
                f"Now playing: {self.current_song['title']} by "
                f"{self.current_song['artist']} | Requested by: "
                f"{self.current_song.get('requester', 'playlist')} | "
                f"♡ {like_count}",
                whisper
            )
        else:
            # Try to get current Spotify playback
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
        likes_key = f"{self.current_song['title']}_{self.current_song['artist']}".lower()

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

        # Track user's received likes
        requester = self.current_song.get("requester", "").lower()
        if requester:
            self.user_likes[requester] = self.user_likes.get(requester, 0) + 1

        self._save_data()

        count = self.likes[likes_key]["count"]
        await self.bot.send_chat(
            f"♡ {message.chatter.name} liked this song! "
            f"({count} total likes)"
        )

        # Update overlay
        if self.bot.web_server:
            now_playing = self.bot.web_server._state.get("now_playing", {})
            if now_playing:
                now_playing["likes"] = count
                self.bot.web_server.update_now_playing(now_playing)

    async def cmd_mysongs(self, message, args, whisper=False):
        username = message.chatter.name.lower()
        total_likes = self.user_likes.get(username, 0)

        # Find their most liked song
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

    # === PLAYBACK MONITOR ===

    async def _playback_monitor(self):
        """
        Monitor Spotify playback. Key logic:
        - Only push next song to Spotify queue when current song has <30s left
        - Track what's been pushed so wrongsong can handle the edge case
        - Auto-skip songs that were removed after being pushed to Spotify
        """
        last_track_id = None
        self._next_pushed = False  # Whether we've pushed the next song to Spotify
        self._skip_on_play = None  # URI/title to auto-skip if it starts playing

        while True:
            try:
                if not self.bot.is_feature_enabled("song_requests"):
                    await asyncio.sleep(5)
                    continue

                playback = await self._get_current_playback()
                if not playback or not playback.get("item"):
                    await asyncio.sleep(3)
                    continue

                track = playback["item"]
                track_id = track.get("id") or track.get("uri")
                track_uri = track.get("uri", "")
                progress_ms = playback.get("progress_ms", 0)
                duration_ms = track.get("duration_ms", 0)
                remaining_ms = duration_ms - progress_ms

                # --- Detect track change ---
                if track_id != last_track_id:
                    last_track_id = track_id
                    self._next_pushed = False  # Reset push flag for new track

                    # Check if this new track should be auto-skipped
                    if self._skip_on_play:
                        skip_target = self._skip_on_play
                        if (track_uri == skip_target or
                                track["name"].lower() == skip_target):
                            print(f"[SongRequest] Auto-skipping removed song: {track['name']}")
                            await self._skip_track()
                            self._skip_on_play = None
                            continue

                    # Check if this is a song from our queue that started playing
                    await self._on_track_change(track)

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

            except Exception as e:
                print(f"[SongRequest Monitor] Error: {e}")

            await asyncio.sleep(3)

    async def _on_track_change(self, track):
        """Called when the current Spotify track changes."""
        track_name = track["name"].lower()
        track_uri = track.get("uri", "")

        # Check if this is a queued song that just started
        if self.queue:
            queued = self.queue[0]
            if (queued["source"] == "spotify" and
                (queued.get("uri") == track_uri or
                 queued["title"].lower() in track_name)):

                # This queued song is now playing
                self.current_song = self.queue.pop(0)
                self._save_data()
                self._save_history(self.current_song)

                # Announce in chat
                likes_key = f"{self.current_song['title']}_{self.current_song['artist']}".lower()
                like_count = self.likes.get(likes_key, {}).get("count", 0)

                await self.bot.send_chat(
                    f"♫ Now playing: {self.current_song['title']} by "
                    f"{self.current_song['artist']} | "
                    f"Requested by: {self.current_song['requester']} | ♡ {like_count}"
                )

                # Update overlay
                if self.bot.web_server:
                    self.bot.web_server.update_now_playing({
                        "title": self.current_song["title"],
                        "artist": self.current_song["artist"],
                        "requester": self.current_song["requester"],
                        "album_art": self.current_song.get("album_art"),
                        "duration_ms": self.current_song.get("duration_ms", 0),
                        "likes": like_count,
                        "started_at": time.time(),
                    })
            else:
                # Not a queued song, playlist is playing
                if self.current_song:
                    self.current_song = None
                    if self.bot.web_server:
                        self.bot.web_server.update_now_playing(None)

    async def cleanup(self):
        if self._monitor_task:
            self._monitor_task.cancel()
        if self.session:
            await self.session.close()
