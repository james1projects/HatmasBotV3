"""
StreamlootsPlugin
=================
Event hub for Streamloots card redemptions, chest purchases, and gifts.

Streamloots has no official API. This plugin listens to the same
unofficial-but-stable surface MixItUp and Firebot use: the alert
overlay's Server-Sent Events stream at

    https://widgets.streamloots.com/alerts/<STREAMLOOTS_ALERT_ID>/media-stream

Each SSE "data:" line carries one JSON event. Card fields are looked
up BY NAME (never positionally — field order is not guaranteed):
"username", "message", "longMessage", "rarity", "quantity", "giftee".
A gift is a purchase whose fields include "giftee".

Listener-list pattern (same as KillDeathDetector / SmitePlugin):
other plugins subscribe in main.py via add_redemption_listener /
add_purchase_listener / add_gift_listener and receive a normalized
dict. Nothing here knows about Factorio or any other consumer.

Normalized event shapes:
    redemption: {type, card_name, username, message, long_message,
                 rarity, image_url, sound_url, alert_message, fields, raw}
    purchase:   {type, username, quantity, fields, raw}
    gift:       {type, username (gifter), giftee, quantity, fields, raw}

Config: STREAMLOOTS_ALERT_ID in config_local.py (fail closed — plugin
disables itself when empty). Dashboard toggle: "streamloots" gates
dispatch (the connection stays up; events are dropped while off).
Every raw event is appended to data/streamloots_events.jsonl for
debugging and for building card maps from real payloads.

Testing without spending cards: POST /api/action with
action=test_streamloots (handled in core/webserver.py) which calls
simulate_redemption() and pushes a fake card through the same
dispatch path as the real stream.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

from core.config import STREAMLOOTS_ALERT_ID, BASE_DIR

STREAM_URL = "https://widgets.streamloots.com/alerts/{alert_id}/media-stream"
RECONNECT_DELAY_MIN = 5      # seconds, doubles per consecutive failure
RECONNECT_DELAY_MAX = 120
READ_IDLE_SECONDS = 900      # dead-connection watchdog (see _listen_loop)
EVENT_LOG_PATH = Path(BASE_DIR) / "data" / "streamloots_events.jsonl"


class StreamlootsPlugin:
    """SSE listener + event hub for Streamloots."""

    def __init__(self):
        self.bot = None
        self._session: Optional["aiohttp.ClientSession"] = None
        self._task: Optional[asyncio.Task] = None
        self._redemption_listeners: List[Callable] = []
        self._purchase_listeners: List[Callable] = []
        self._gift_listeners: List[Callable] = []
        self.connected = False
        self.config_error = False
        self.events_received = 0
        self.last_event_at: Optional[float] = None
        # Recently seen card names (distinct, newest first, capped).
        # Feeds the /factorio/cards manager so a card can be played
        # once on the Streamloots page and then mapped with one click.
        self.recent_cards: List[Dict[str, Any]] = []

    # ──────────────────────────────────────────────────────────────
    #   PLUGIN LIFECYCLE
    # ──────────────────────────────────────────────────────────────

    def setup(self, bot):
        self.bot = bot

    async def on_ready(self):
        if aiohttp is None:
            print("[Streamloots] aiohttp missing — disabled")
            return
        if not STREAMLOOTS_ALERT_ID:
            print("[Streamloots] STREAMLOOTS_ALERT_ID not set in "
                  "config_local.py — disabled")
            return
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._listen_loop())
        print("[Streamloots] Listening for card events")

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
        self.connected = False

    # ──────────────────────────────────────────────────────────────
    #   LISTENER REGISTRATION (called from main.py)
    # ──────────────────────────────────────────────────────────────

    def add_redemption_listener(self, callback):
        """callback: async def cb(event: dict) — card redemptions."""
        self._redemption_listeners.append(callback)

    def add_purchase_listener(self, callback):
        """callback: async def cb(event: dict) — chest purchases."""
        self._purchase_listeners.append(callback)

    def add_gift_listener(self, callback):
        """callback: async def cb(event: dict) — gifted chests."""
        self._gift_listeners.append(callback)

    # ──────────────────────────────────────────────────────────────
    #   SSE LISTEN LOOP
    # ──────────────────────────────────────────────────────────────

    async def _listen_loop(self):
        url = STREAM_URL.format(alert_id=STREAMLOOTS_ALERT_ID)
        delay = RECONNECT_DELAY_MIN
        while True:
            try:
                # sock_read is a dead-connection watchdog: a half-open
                # TCP connection (wifi blip, server vanished) otherwise
                # looks "connected" forever while cards silently go
                # missing. A genuinely quiet stream trips it too —
                # that's fine, idle reconnects are harmless and are
                # handled below without backoff growth.
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=15,
                                                sock_read=READ_IDLE_SECONDS)
                async with self._session.get(url, timeout=timeout) as resp:
                    if 400 <= resp.status < 500 and resp.status != 429:
                        # Wrong or revoked alert ID — retrying won't fix
                        # a config error. Stop until restart.
                        self.config_error = True
                        print(f"[Streamloots] Stream returned HTTP "
                              f"{resp.status}. STREAMLOOTS_ALERT_ID looks "
                              f"wrong or revoked; giving up until restart.")
                        return
                    if resp.status != 200:
                        print(f"[Streamloots] Stream returned HTTP "
                              f"{resp.status}; retrying in {delay}s")
                        raise ConnectionError(f"HTTP {resp.status}")
                    self.connected = True
                    delay = RECONNECT_DELAY_MIN
                    print("[Streamloots] Stream connected")
                    await self._read_sse(resp)
                # Clean EOF — server closed; reconnect promptly.
                print("[Streamloots] Stream ended; reconnecting")
                self.connected = False
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # Watchdog fired: no data for READ_IDLE_SECONDS (a dead
                # connection is indistinguishable from a quiet stream)
                # or a slow connect. Refresh promptly, no backoff.
                self.connected = False
                print("[Streamloots] No data within idle window; "
                      "refreshing connection")
                try:
                    await asyncio.sleep(RECONNECT_DELAY_MIN)
                except asyncio.CancelledError:
                    raise
            except Exception as e:
                self.connected = False
                print(f"[Streamloots] Stream error: {e}; "
                      f"reconnecting in {delay}s")
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
                delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _read_sse(self, resp):
        """Parse SSE framing: 'data:' lines accumulate until a blank
        line terminates the event. Comment lines (':...') and other
        SSE fields are ignored."""
        data_lines: List[str] = []
        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    await self._handle_payload(payload)
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            # else: comment/keepalive or other SSE field — ignore

    async def _handle_payload(self, payload: str):
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            print(f"[Streamloots] Unparseable event "
                  f"({len(payload)} chars); skipped")
            return
        self.events_received += 1
        self.last_event_at = time.time()
        self._log_raw(event)
        await self._dispatch(event)

    # ──────────────────────────────────────────────────────────────
    #   NORMALIZATION + DISPATCH
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _field_map(data: Dict[str, Any]) -> Dict[str, str]:
        out = {}
        for f in data.get("fields") or []:
            name = f.get("name")
            if name is not None:
                out[name] = f.get("value")
        return out

    def _normalize(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        data = event.get("data") or {}
        etype = data.get("type")
        fields = self._field_map(data)
        if etype == "redemption":
            return {
                "type": "redemption",
                "card_name": data.get("cardName"),
                "username": fields.get("username"),
                "message": fields.get("message"),
                "long_message": fields.get("longMessage"),
                "rarity": fields.get("rarity"),
                "image_url": event.get("imageUrl"),
                "sound_url": event.get("soundUrl"),
                "alert_message": event.get("message"),
                "fields": fields,
                "raw": event,
            }
        if etype == "purchase":
            base = {
                "username": fields.get("username"),
                "quantity": _to_int(fields.get("quantity"), 1),
                "fields": fields,
                "raw": event,
            }
            if fields.get("giftee"):
                base["type"] = "gift"
                base["giftee"] = fields.get("giftee")
            else:
                base["type"] = "purchase"
            return base
        return None

    async def _dispatch(self, event: Dict[str, Any]):
        if self.bot and not self.bot.is_feature_enabled("streamloots"):
            return
        norm = self._normalize(event)
        if norm is None:
            etype = (event.get("data") or {}).get("type")
            print(f"[Streamloots] Ignoring unknown event type: {etype!r}")
            return
        if norm["type"] == "redemption":
            print(f"[Streamloots] Card played: {norm['card_name']!r} "
                  f"by {norm['username']!r} (rarity: {norm['rarity']})")
            self._remember_card(norm)
            listeners = self._redemption_listeners
        elif norm["type"] == "gift":
            print(f"[Streamloots] {norm['username']!r} gifted "
                  f"{norm['quantity']} chest(s) to {norm['giftee']!r}")
            listeners = self._gift_listeners
        else:
            print(f"[Streamloots] {norm['username']!r} bought "
                  f"{norm['quantity']} chest(s)")
            listeners = self._purchase_listeners
        for cb in listeners:
            try:
                await cb(norm)
            except Exception as e:
                print(f"[Streamloots] Listener error "
                      f"({getattr(cb, '__qualname__', cb)}): {e}")

    def _remember_card(self, norm: Dict[str, Any]):
        name = norm.get("card_name")
        if not name:
            return
        self.recent_cards = [c for c in self.recent_cards
                             if c["card_name"] != name]
        self.recent_cards.insert(0, {
            "card_name": name,
            "username": norm.get("username"),
            "seen_at": time.time(),
        })
        del self.recent_cards[20:]

    def _log_raw(self, event: Dict[str, Any]):
        """Append the raw event to data/streamloots_events.jsonl so
        real payloads are available for debugging and card mapping."""
        try:
            EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with EVENT_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"received_at": time.time(),
                                     "event": event}) + "\n")
        except OSError as e:
            print(f"[Streamloots] Event log write failed: {e}")

    # ──────────────────────────────────────────────────────────────
    #   TESTING + STATUS
    # ──────────────────────────────────────────────────────────────

    async def simulate_redemption(self, card_name="Test Card",
                                  username="TestViewer", message="",
                                  rarity="common"):
        """Push a fake card redemption through the real dispatch path.
        Wired to the dashboard via the test_streamloots action."""
        fake = {
            "imageUrl": "", "soundUrl": "",
            "message": f"{username} redeemed {card_name}",
            "data": {
                "type": "redemption",
                "cardName": card_name,
                "fields": [
                    {"name": "username", "value": username},
                    {"name": "message", "value": message},
                    {"name": "rarity", "value": rarity},
                ],
            },
        }
        await self._dispatch(fake)
        return True

    def get_status(self) -> Dict[str, Any]:
        return {
            "configured": bool(STREAMLOOTS_ALERT_ID),
            "connected": self.connected,
            "config_error": self.config_error,
            "events_received": self.events_received,
            "last_event_at": self.last_event_at,
            "listeners": {
                "redemption": len(self._redemption_listeners),
                "purchase": len(self._purchase_listeners),
                "gift": len(self._gift_listeners),
            },
        }


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
