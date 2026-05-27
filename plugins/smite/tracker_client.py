"""
plugins/smite/tracker_client.py
================================
HTTP wrapper for tracker.gg's internal Smite 2 API.

tracker.gg sits behind Cloudflare bot protection that checks TLS
fingerprints, so plain `aiohttp` 403s. We use `curl_cffi` (which
impersonates Chrome's TLS handshake) for the actual GETs, and run
the sync calls in a ThreadPoolExecutor (`self._cffi_executor`,
created in SmitePlugin.__init__) so they don't block the asyncio loop.

Endpoint primitives provided by this mixin:
  * `_cffi_get(url)` / `_tracker_get(url)` — low-level GET wrappers.
  * `_fetch_live_match()`         — current match snapshot
  * `_fetch_profile()`            — default ranked conquest profile
  * `_fetch_profile_for_mode()`   — per-gamemode profile aggregates
  * `_fetch_summary()`            — lightweight rank/SR data
  * `_fetch_match_detail()`       — full post-game match data

All cache hits go through `self.cache` (a `core.cache.Cache` set up in
__init__) with SMITE2_CACHE_TTL. force_refresh=True bypasses cache and
appends `?forceCollect=true` to the URL where supported.
"""

from __future__ import annotations

import asyncio

from core.config import (
    SMITE2_PLATFORM, SMITE2_PLATFORM_ID, SMITE2_TRACKER_BASE,
    SMITE2_LIVE_URL, SMITE2_SUMMARY_URL, SMITE2_MATCH_URL,
    SMITE2_CACHE_TTL,
)


class _TrackerClientMixin:
    """
    Mixed into SmitePlugin. Reads/writes:
      self._cffi_session    curl_cffi Session (set up in on_ready)
      self._cffi_executor   ThreadPoolExecutor for the sync HTTP calls
      self.cache            core.cache.Cache for short-lived dedup
    """

    def _cffi_get(self, url):
        """Synchronous GET via curl_cffi (runs in executor thread)."""
        resp = self._cffi_session.get(url)
        if resp.status_code == 200:
            return resp.json()
        return None

    async def _tracker_get(self, url):
        """Async wrapper: run curl_cffi GET in a thread."""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._cffi_executor, self._cffi_get, url
            )
        except Exception as e:
            print(f"[Smite] Tracker fetch error: {e}")
            return None

    async def _fetch_live_match(self):
        """
        GET /matches/steam/{id}/live
        Returns match data when in game, None when not.
        """
        url = f"{SMITE2_LIVE_URL}/{SMITE2_PLATFORM}/{SMITE2_PLATFORM_ID}/live"
        return await self._tracker_get(url)

    async def _fetch_profile(self, force_refresh=False):
        """
        GET /profile/steam/{id}[?forceCollect=true]
        Returns full profile with god stats, gamemodes, etc.
        Defaults to ranked conquest mode — for a multi-mode pull use
        _fetch_profile_for_mode().
        """
        cache_key = "profile"
        if not force_refresh:
            cached = self.cache.get(cache_key, ttl=SMITE2_CACHE_TTL)
            if cached:
                return cached

        url = f"{SMITE2_TRACKER_BASE}/{SMITE2_PLATFORM}/{SMITE2_PLATFORM_ID}"
        if force_refresh:
            url += "?forceCollect=true"

        data = await self._tracker_get(url)
        if data:
            self.cache.set(cache_key, data)
        return data

    async def _fetch_profile_for_mode(self, mode, force_refresh=False):
        """
        GET /profile/steam/{id}/segments/god?gamemode=<mode>&season=

        This is tracker.gg's per-gamemode per-god aggregates endpoint —
        the same one their web UI hits when you click a gamemode filter
        on the gods tab. Distinct from /profile/steam/{id} which only
        returns the default-mode top-level profile.

        Empty `season=` returns all-seasons. Used by get_god_aggregates
        to pull stats per mode and combine.
        """
        cache_key = f"profile:{mode}"
        if not force_refresh:
            cached = self.cache.get(cache_key, ttl=SMITE2_CACHE_TTL)
            if cached:
                return cached

        url = (f"{SMITE2_TRACKER_BASE}/{SMITE2_PLATFORM}/{SMITE2_PLATFORM_ID}"
               f"/segments/god?gamemode={mode}&season=")
        if force_refresh:
            url += "&forceCollect=true"

        data = await self._tracker_get(url)
        if data:
            self.cache.set(cache_key, data)
        return data

    async def _fetch_summary(self):
        """
        GET /profile/steam/{id}/summary
        Lightweight rank/SR data.
        """
        cache_key = "summary"
        cached = self.cache.get(cache_key, ttl=SMITE2_CACHE_TTL)
        if cached:
            return cached

        url = f"{SMITE2_SUMMARY_URL}/{SMITE2_PLATFORM}/{SMITE2_PLATFORM_ID}/summary"
        data = await self._tracker_get(url)
        if data:
            self.cache.set(cache_key, data)
        return data

    async def _fetch_match_detail(self, match_id):
        """
        GET /matches/{match_id}?authlevel=user
        Full post-game match data.
        """
        url = f"{SMITE2_MATCH_URL}/{match_id}?authlevel=user"
        return await self._tracker_get(url)
