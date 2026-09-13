"""
core/twitch_app.py
==================
A small Twitch Helix client that uses an APP ACCESS token (OAuth
client_credentials) so standalone CLI tools (tools/vod_channels.py) can
list channels and their archive VODs without the running bot.

Why a separate token: Twitch rotates a USER refresh token every time it
is used. core/token_manager.py owns the bot's two user tokens; if a CLI
refreshed one of them behind the bot's back, the bot's in-memory refresh
token would go stale and its next refresh would fail until restart. An
app token has no refresh token at all (it is simply re-minted when it
expires), so this module never imports token_manager and never reads
data/twitch_token.json or data/twitch_broadcaster_token.json.

Everything a Helix endpoint here needs (users, videos, games, streams)
works with an app token; no user scope is involved.

`request` is injectable so tests run with a fake and no network. The
default implementation uses aiohttp, imported lazily.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

RequestFn = Callable[..., Awaitable[Tuple[int, Any]]]

TOKEN_MARGIN_S = 300          # re-mint when less than this is left
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class TwitchError(Exception):
    def __init__(self, status: int, body: Any, message: Optional[str] = None):
        self.status = status
        self.body = body
        super().__init__(message or f"Twitch API error {status}: {_short(body)}")


def _short(body: Any, n: int = 200) -> str:
    try:
        s = body if isinstance(body, str) else json.dumps(body)
    except Exception:
        s = str(body)
    return s[:n]


def parse_duration(s: Optional[str]) -> float:
    """Helix video duration ("3h20m5s", "45m", "7s") -> seconds."""
    if not s:
        return 0.0
    m = _DURATION_RE.match(s.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"unrecognised Twitch duration: {s!r}")
    h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return float(h * 3600 + mi * 60 + sec)


def parse_utc(ts: str) -> datetime:
    """Helix timestamp ("2026-09-01T18:03:12Z", fractional seconds and
    +00:00 tolerated; a bare date means midnight UTC) -> aware UTC."""
    t = ts.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def twitch_created_to_local_iso(created_at: str) -> str:
    """Helix UTC timestamp -> naive local ISO without microseconds, the
    format recordings.recorded_at uses in the VOD index."""
    return parse_utc(created_at).astimezone().replace(microsecond=0, tzinfo=None).isoformat()


class TwitchApp:
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    HELIX = "https://api.twitch.tv/helix/"

    def __init__(self, client_id: str, client_secret: str,
                 cache_path: Optional[Path] = None,
                 request: Optional[RequestFn] = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.cache_path = Path(cache_path) if cache_path else None
        self._request: RequestFn = request or self._aiohttp_request
        self._session = None
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    # ── token ─────────────────────────────────────────────────────────

    def _token_ok(self) -> bool:
        return bool(self._token) and (self._expires_at - time.time()) > TOKEN_MARGIN_S

    def _load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            d = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._token = str(d.get("access_token") or "")
            self._expires_at = float(d.get("expires_at") or 0)
        except Exception:
            self._token, self._expires_at = None, 0.0

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps({"access_token": self._token,
                                       "expires_at": self._expires_at}, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    def _forget_token(self) -> None:
        self._token, self._expires_at = None, 0.0
        if self.cache_path:
            try:
                self.cache_path.unlink()
            except OSError:
                pass

    async def token(self) -> str:
        if self._token_ok():
            return self._token  # type: ignore[return-value]
        self._load_cache()
        if self._token_ok():
            return self._token  # type: ignore[return-value]
        status, body = await self._request(
            "POST", self.TOKEN_URL,
            data={"client_id": self.client_id, "client_secret": self.client_secret,
                  "grant_type": "client_credentials"},
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise TwitchError(status, body, f"could not mint app token ({status}): {_short(body)}")
        self._token = str(body["access_token"])
        self._expires_at = time.time() + float(body.get("expires_in") or 3600)
        self._save_cache()
        return self._token

    # ── requests ──────────────────────────────────────────────────────

    async def _headers(self) -> Dict[str, str]:
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {await self.token()}"}

    async def get(self, path: str, **params: Any) -> dict:
        url = self.HELIX + path.lstrip("/")
        q = {k: (v if isinstance(v, str) else str(v)) for k, v in params.items() if v is not None}
        status, body = await self._request("GET", url, params=q, headers=await self._headers())
        if status == 401:
            self._forget_token()
            status, body = await self._request("GET", url, params=q, headers=await self._headers())
        if status != 200:
            raise TwitchError(status, body)
        return body if isinstance(body, dict) else {}

    async def paginate(self, path: str, max_items: Optional[int] = None, **params: Any) -> List[dict]:
        items: List[dict] = []
        cursor: Optional[str] = None
        while True:
            body = await self.get(path, after=cursor, **params)
            data = body.get("data") or []
            items.extend(data)
            cursor = (body.get("pagination") or {}).get("cursor")
            if not data or not cursor or (max_items is not None and len(items) >= max_items):
                break
        if max_items is not None:
            items = items[:max_items]
        return items

    # ── endpoints ─────────────────────────────────────────────────────

    async def user_by_login(self, login: str) -> Optional[dict]:
        data = (await self.get("users", login=login)).get("data") or []
        return data[0] if data else None

    async def archives(self, user_id: str, max_items: Optional[int] = None,
                       since: Optional[str] = None) -> List[dict]:
        """Past broadcasts, newest first. `since` (date or ISO timestamp)
        stops paging at the first VOD older than it."""
        cutoff = parse_utc(since) if since else None
        items: List[dict] = []
        cursor: Optional[str] = None
        while True:
            body = await self.get("videos", user_id=user_id, type="archive", first=100, after=cursor)
            data = body.get("data") or []
            stop = not data
            for v in data:
                if cutoff is not None:
                    try:
                        if parse_utc(v.get("created_at") or "") < cutoff:
                            stop = True
                            continue
                    except ValueError:
                        pass
                items.append(v)
            cursor = (body.get("pagination") or {}).get("cursor")
            if stop or not cursor or (max_items is not None and len(items) >= max_items):
                break
        if max_items is not None:
            items = items[:max_items]
        return items

    async def game_id(self, name: str = "SMITE 2") -> Optional[str]:
        data = (await self.get("games", name=name)).get("data") or []
        return str(data[0]["id"]) if data else None

    async def live_streams(self, game_id: str, first: int = 50) -> List[dict]:
        return (await self.get("streams", game_id=game_id, first=min(int(first), 100))).get("data") or []

    # ── default transport ─────────────────────────────────────────────

    async def _aiohttp_request(self, method: str, url: str, *, params=None, data=None,
                               headers=None) -> Tuple[int, Any]:
        import aiohttp  # lazy so tests with a fake request never need it
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        async with self._session.request(method, url, params=params, data=data, headers=headers) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {}
            return resp.status, (body if body is not None else {})

    async def close(self) -> None:
        s, self._session = self._session, None
        if s is not None and not s.closed:
            await s.close()
