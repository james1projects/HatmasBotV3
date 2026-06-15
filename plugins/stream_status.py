"""
StreamStatusPlugin
==================
Polls Twitch Helix every 60 seconds to track whether the broadcaster
is live. On state change (offline → live or live → offline), emits
`stream_live` / `stream_offline` via the overlay manager so the public
webserver can cache and serve via /api/stream-status.

Why a poll instead of EventSub: keeps it self-contained, doesn't need
a new EventSub subscription, and 60s latency is fine for a website
indicator. EventSub stream.online/stream.offline could replace the
poll later if we want sub-second latency.
"""

import asyncio
from typing import Any, Dict, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

from core.config import TWITCH_CHANNEL


POLL_INTERVAL = 60  # seconds
INITIAL_DELAY = 5   # let token manager warm up first


class StreamStatusPlugin:
    """Poll Twitch /helix/streams and broadcast state changes."""

    def __init__(self, token_manager=None, web_server=None):
        self.bot = None
        self.token_manager = token_manager
        self.web_server = web_server  # for overlay.emit
        self._session: Optional["aiohttp.ClientSession"] = None
        self._task: Optional[asyncio.Task] = None
        self._is_live: bool = False
        self._info: Dict[str, Any] = {
            "is_live": False,
            "channel": TWITCH_CHANNEL,
        }
        # Plugin listeners (killdetector convention). Called with the
        # stream info dict on live/offline transitions. NOTE: a bot
        # restart mid-stream fires the live transition again ("first
        # detection"), so consumers needing once-per-day semantics
        # must dedupe themselves (the Discord announcer does).
        self._live_listeners = []
        self._offline_listeners = []

    def setup(self, bot):
        self.bot = bot

    def add_live_listener(self, coro):
        """Register an async callable(info: dict) for the live transition."""
        self._live_listeners.append(coro)

    def add_offline_listener(self, coro):
        """Register an async callable(info: dict) for the offline transition."""
        self._offline_listeners.append(coro)

    async def _dispatch(self, listeners, info):
        for listener in listeners:
            try:
                await listener(info)
            except Exception as e:
                print(f"[StreamStatus] listener error: {e}")

    async def on_ready(self):
        if aiohttp is None:
            print("[StreamStatus] aiohttp missing — disabled")
            return
        if not self.token_manager:
            print("[StreamStatus] no token_manager — disabled")
            return
        if not TWITCH_CHANNEL or TWITCH_CHANNEL == "YOUR_CHANNEL":
            print("[StreamStatus] TWITCH_CHANNEL not configured — disabled")
            return

        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._poll_loop())
        print(f"[StreamStatus] Polling /helix/streams for {TWITCH_CHANNEL!r} "
              f"every {POLL_INTERVAL}s")

    async def cleanup(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None

    # ──────────────────────────────────────────────────────────────
    #   PUBLIC API (read by PublicWebServer's /api/stream-status)
    # ──────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Latest known stream status. Always returns a dict; safe to
        call before the first poll completes (returns is_live=false).

        Also enriches with the broadcaster's current_god by peeking at
        the smite plugin's in-memory state. This way the website can
        show 'Currently playing: <god>' without needing a separate
        endpoint, and the value updates between status polls.
        """
        out = dict(self._info)  # defensive copy
        out["current_god"] = self._read_current_god()
        return out

    def _read_current_god(self) -> Optional[str]:
        """Best-effort read of smite.current_god['name']."""
        if self.bot is None:
            return None
        plugins = getattr(self.bot, "plugins", None)
        if plugins is None:
            return None
        smite = plugins.get("smite") if hasattr(plugins, "get") else None
        if smite is None:
            return None
        cg = getattr(smite, "current_god", None)
        if isinstance(cg, dict):
            return cg.get("name")
        if isinstance(cg, str):
            return cg
        return None

    # ──────────────────────────────────────────────────────────────
    #   POLL LOOP
    # ──────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        try:
            await asyncio.sleep(INITIAL_DELAY)
        except asyncio.CancelledError:
            raise
        while True:
            try:
                await self._check_status()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[StreamStatus] Poll error: {e}")
            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _check_status(self):
        url = (f"https://api.twitch.tv/helix/streams"
               f"?user_login={TWITCH_CHANNEL}")

        try:
            headers = await self.token_manager.get_bot_headers()
        except Exception as e:
            print(f"[StreamStatus] couldn't get bot headers: {e}")
            return

        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 401:
                # Token expired — refresh and retry once.
                try:
                    refreshed = await self.token_manager.handle_401("bot")
                except Exception as e:
                    print(f"[StreamStatus] token refresh raised: {e}")
                    return
                if not refreshed:
                    return
                headers = await self.token_manager.get_bot_headers()
                async with self._session.get(url, headers=headers) as retry:
                    if retry.status != 200:
                        return
                    data = await retry.json()
            elif resp.status == 200:
                data = await resp.json()
            else:
                # Some other failure (rate limit, 5xx). Skip this cycle.
                return

        items = data.get("data") or []
        if items:
            stream = items[0]
            new_live = True
            new_info = {
                "is_live": True,
                "channel": TWITCH_CHANNEL,
                "title": stream.get("title"),
                "game": stream.get("game_name"),
                "game_id": stream.get("game_id"),
                "viewer_count": stream.get("viewer_count"),
                "started_at": stream.get("started_at"),
                "thumbnail_url": stream.get("thumbnail_url"),
            }
        else:
            new_live = False
            new_info = {
                "is_live": False,
                "channel": TWITCH_CHANNEL,
            }

        was_live = self._is_live
        self._is_live = new_live
        self._info = new_info

        # Emit on state change (or first detection) so the public
        # webserver and any other listeners can react.
        if new_live and not was_live:
            print(f"[StreamStatus] Stream is LIVE — "
                  f"{new_info.get('title') or '(no title)'}")
            await self._emit("stream_live", new_info)
            await self._dispatch(self._live_listeners, new_info)
        elif was_live and not new_live:
            print("[StreamStatus] Stream went OFFLINE")
            await self._emit("stream_offline", new_info)
            await self._dispatch(self._offline_listeners, new_info)

    async def _emit(self, event_name: str, data: Dict[str, Any]):
        if not self.web_server:
            return
        overlay = getattr(self.web_server, "overlay", None)
        if overlay is None:
            return
        try:
            await overlay.emit(event_name, data)
        except Exception as e:
            print(f"[StreamStatus] emit({event_name}) failed: {e}")
