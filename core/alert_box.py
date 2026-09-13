"""
core/alert_box.py — the Hatmaster Alert Box (docs/ALERT_BOX.md).

One full-canvas OBS browser source (`/overlay/alerts?box=main`) that plays
the bot's transient overlays as *alert kinds*: gamble, tts, voiceline,
cocaster, dividend, match_end, leaderboard, tradefeed, portfolio, spin and
the bingo events. James configures, per box and per kind: enabled, lane
(a queue; kinds in one lane play one at a time, lanes play concurrently),
duration cap, sound on/off, volume, and where on the 1920x1080 canvas the
alert sits (x, y, w, h, anchor). Config lives in data/alerts.json, edited
by the dashboard layout editor (overlays/alerts_layout.html).

Server side this class is small on purpose: it listens to every
OverlayManager.emit, maps event -> kinds, and pushes one `alert` message
per enabled (box, kind) to that box's websocket clients
(`alerts:<box>`). Timing, queues and rendering live in the browser
(overlays/alerts.html + overlays/alerts/<kind>.js) so a stalled socket
never stutters what is on screen. The last RECENT alerts per box are kept
for the layout page's Recent list and its Replay button.

Every migrated kind starts DISABLED: folding the old overlays changes
nothing on stream until James flips a kind on and removes the old source
in OBS. The old routes stay served meanwhile (TODO(alertbox) markers).
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

CANVAS_W, CANVAS_H = 1920, 1080
RECENT = 50
ANCHORS = ("top-left", "top-center", "top-right", "center-left", "center", "center-right",
           "bottom-left", "bottom-center", "bottom-right")
DEFAULT_LANES = {"audio": {"max_queue": 20}, "economy": {"max_queue": 10},
                 "bingo": {"max_queue": 10}, "caption": {"max_queue": 5}, "feed": {"max_queue": 20}}
BACKGROUND_NAME = "alerts_background"        # data/alerts_background.<ext>: the editor's scene screenshot
BACKGROUND_EXTS = ("png", "jpg", "jpeg", "webp")

# kind -> definition. `events` are the OverlayManager events that produce
# this kind; `legacy` is the overlay it replaces (its route stays served
# until James has switched); `sample` feeds the Test / preview buttons.
KINDS: Dict[str, dict] = {
    "gamble": {
        "label": "Gamble result", "events": ["gamble_result"], "legacy": "sound_alerts",
        "lane": "audio", "duration": 8, "sound": True, "enabled": False,
        "x": 1500, "y": 880, "w": 380, "h": 160, "anchor": "bottom-right",
        "sample": {"player": "Dyna", "type": "jackpot", "roll": 100, "wager": 500, "winnings": 5000},
    },
    "tts": {
        "label": "Text to speech", "events": ["tts_message"], "legacy": "tts",
        "lane": "audio", "duration": 30, "sound": True, "enabled": False,
        "x": 660, "y": 860, "w": 600, "h": 180, "anchor": "bottom-center",
        "sample": {"user": "Dyna", "message": "Hatmaster, the Loki is behind you. He is always behind you.",
                   "audio_url": ""},
    },
    "voiceline": {
        "label": "Voiceline", "events": ["voiceline_play"], "legacy": "voicelines",
        "lane": "audio", "duration": 20, "sound": True, "enabled": False,
        "x": 40, "y": 700, "w": 420, "h": 340, "anchor": "bottom-left",
        "sample": {"god": "Loki", "type": "god_taunt", "audio_url": "", "video_url": "", "requested_by": "Dyna"},
    },
    "cocaster": {
        "label": "Co-caster line", "events": ["cocaster_line"], "legacy": "cocaster",
        "lane": "caption", "duration": 9, "sound": False, "enabled": False,
        "x": 40, "y": 920, "w": 1200, "h": 100, "anchor": "bottom-left",
        "sample": {"text": "Triple kill, and the hat stays on.", "event": {"kind": "multikill"}},
    },
    "dividend": {
        "label": "Dividend paid", "events": ["dividend_paid"], "legacy": "economy_dividend",
        "lane": "economy", "duration": 10, "sound": False, "enabled": False,
        "x": 1460, "y": 40, "w": 420, "h": 180, "anchor": "top-right",
        "sample": {"god": "Ymir", "god_name": "Ymir", "holders": 7, "rate": 0.05, "total_hats": 1250},
    },
    "match_end": {
        "label": "Match end recap", "events": ["match_end_economy"], "legacy": "economy_match_end",
        "lane": "economy", "duration": 20, "sound": False, "enabled": False,
        "x": 700, "y": 340, "w": 520, "h": 400, "anchor": "center",
        "sample": {"god": "Ymir", "outcome": "win", "kda": [7, 2, 11], "old_price": 120, "new_price": 138,
                   "change_pct": 15.0, "streak": 2, "mode": "Conquest", "free_shares": {"shares_each": 1, "viewer_count": 14},
                   "movers": [{"name": "Loki", "change_pct": -4.2}, {"name": "Ra", "change_pct": 2.1}, {"name": "Ymir", "change_pct": 15.0}]},
    },
    "leaderboard": {
        "label": "Leaderboard", "events": ["leaderboard_update"], "legacy": "economy_leaderboard",
        "lane": "economy", "duration": 30, "sound": False, "enabled": False,
        "x": 1460, "y": 260, "w": 420, "h": 520, "anchor": "top-right",
        "sample": {"leaderboard": [
            {"username": "dyna", "portfolio_value": 12400, "change_pct": 4.2, "rank_change": 1, "top_gods": ["Ymir", "Loki"]},
            {"username": "moolan", "portfolio_value": 9800, "change_pct": -1.1, "rank_change": -1, "top_gods": ["Ra"]},
            {"username": "grover", "portfolio_value": 7100, "change_pct": 0.4, "rank_change": 0, "top_gods": ["Sylvanus", "Ymir"]}]},
    },
    "tradefeed": {
        "label": "Trade feed", "events": ["trade_executed"], "legacy": "economy_tradefeed",
        "lane": "economy", "duration": 15, "sound": False, "enabled": False,
        "x": 40, "y": 40, "w": 420, "h": 110, "anchor": "top-left",
        "sample": {"type": "buy", "username": "dyna", "display_name": "Dyna", "god": "Ymir", "shares": 3,
                   "price": 138, "total": 414},
    },
    "tradefeed_rolling": {
        "label": "Trade feed (rolling)", "events": ["trade_executed", "dividend_paid"], "legacy": "economy_tradefeed",
        "lane": "feed", "duration": 15, "sound": False, "enabled": False,
        "x": 40, "y": 170, "w": 320, "h": 340, "anchor": "top-left",
        "sample": {"type": "buy", "username": "dyna", "display_name": "Dyna", "god": "Ymir", "shares": 3,
                   "price": 138, "total": 414},
    },
    "portfolio": {
        "label": "Portfolio", "events": ["portfolio_requested"], "legacy": "economy_portfolio",
        "lane": "economy", "duration": 15, "sound": False, "enabled": False,
        "x": 40, "y": 300, "w": 460, "h": 400, "anchor": "top-left",
        "sample": {"username": "dyna", "display_name": "Dyna", "hat_balance": 2200, "total_value": 12400,
                   "total_pnl": 1650, "profile_image_url": "",
                   "holdings": [{"god_name": "Ymir", "shares": 12, "avg_cost": 120, "price": 138, "value": 1656, "pnl": 210, "pnl_pct": 14.5},
                                {"god_name": "Loki", "shares": 4, "avg_cost": 105, "price": 95, "value": 380, "pnl": -40, "pnl_pct": -9.5}]},
    },
    "spin": {
        "label": "God pool spin", "events": ["god_pool_spin"], "legacy": "god_pool_spin",
        "lane": "economy", "duration": 14, "sound": True, "enabled": False,
        "x": 510, "y": 290, "w": 900, "h": 500, "anchor": "center",
        "sample": {"candidates": [{"god": "Ymir", "votes": 3}, {"god": "Loki", "votes": 2}, {"god": "Ra", "votes": 1}],
                   "chosen": "Ymir", "chosen_aspect": False, "chosen_votes": 3, "toast": ""},
    },
    "bingo_open": {
        "label": "Bingo: round opened", "events": ["bingo_open"], "legacy": None,
        "lane": "bingo", "duration": 8, "sound": False, "enabled": True,
        "x": 1400, "y": 40, "w": 480, "h": 150, "anchor": "top-right",
        "sample": {"round_id": 7, "pot": 500, "cards": 0, "players": 0},
    },
    "bingo_call": {
        "label": "Bingo: square called", "events": ["bingo_call"], "legacy": None,
        "lane": "bingo", "duration": 6, "sound": False, "enabled": False,
        "x": 1400, "y": 40, "w": 480, "h": 130, "anchor": "top-right",
        "sample": {"round_id": 7, "pot": 525, "cards": 9, "players": 6,
                   "call": {"event_id": "no_mana", "label": "Says \"no mana\"", "source": "deck"},
                   "changed_cards": [1, 2, 3], "leaders": [{"display": "Dyna", "to_bingo": 1}]},
    },
    "bingo_claim": {
        "label": "Bingo: claimed", "events": ["bingo_claim", "bingo_win"], "legacy": None,
        "lane": "bingo", "duration": 12, "sound": True, "enabled": True,
        "x": 1380, "y": 300, "w": 500, "h": 240, "anchor": "top-right",
        "sample": {"round_id": 7, "pot": 525, "cards": 9, "players": 6,
                   "winner": {"login": "dyna", "display": "Dyna", "card_id": 3, "prize": 525, "paid": True}},
    },
    "bingo_closed": {
        "label": "Bingo: round closed", "events": ["bingo_closed"], "legacy": None,
        "lane": "bingo", "duration": 6, "sound": False, "enabled": False,
        "x": 1400, "y": 40, "w": 480, "h": 130, "anchor": "top-right",
        "sample": {"round_id": 7, "pot": 525, "cards": 9, "players": 6, "reason": "manual", "winner": None},
    },
}

KIND_FIELDS = ("enabled", "lane", "duration", "sound", "volume", "x", "y", "w", "h", "anchor")


def _kind_defaults(kind: str) -> dict:
    d = KINDS[kind]
    return {"enabled": bool(d["enabled"]), "lane": d["lane"], "duration": int(d["duration"]),
            "sound": bool(d["sound"]), "volume": 100, "x": d["x"], "y": d["y"], "w": d["w"], "h": d["h"],
            "anchor": d["anchor"]}


def default_config() -> dict:
    return {"boxes": {"main": {"volume": 100, "kinds": {k: _kind_defaults(k) for k in KINDS}}},
            "lanes": copy.deepcopy(DEFAULT_LANES)}


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def validate_config(raw: Any) -> dict:
    """Coerce whatever is on disk / posted into a full config: every box
    gets every kind (missing ones take the defaults), numbers are clamped
    to the canvas, unknown kinds and lanes are dropped."""
    out = default_config()
    if not isinstance(raw, dict):
        return out
    boxes = raw.get("boxes") if isinstance(raw.get("boxes"), dict) else {}
    result_boxes: Dict[str, dict] = {}
    for name, box in boxes.items():
        name = str(name).strip().lower()[:32]
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            continue
        kinds_in = box.get("kinds", {}) if isinstance(box, dict) else {}
        kinds_out = {}
        for kind in KINDS:
            base = _kind_defaults(kind)
            k = kinds_in.get(kind) if isinstance(kinds_in, dict) else None
            if isinstance(k, dict):
                base["enabled"] = bool(k.get("enabled", base["enabled"]))
                lane = str(k.get("lane") or base["lane"]).strip().lower()[:24]
                base["lane"] = lane if lane.replace("_", "").isalnum() else base["lane"]
                base["duration"] = _clamp(k.get("duration"), 1, 600, base["duration"])
                base["sound"] = bool(k.get("sound", base["sound"]))
                base["volume"] = _clamp(k.get("volume"), 0, 100, base["volume"])
                base["w"] = _clamp(k.get("w"), 40, CANVAS_W, base["w"])
                base["h"] = _clamp(k.get("h"), 30, CANVAS_H, base["h"])
                base["x"] = _clamp(k.get("x"), 0, CANVAS_W - base["w"], base["x"])
                base["y"] = _clamp(k.get("y"), 0, CANVAS_H - base["h"], base["y"])
                anchor = str(k.get("anchor") or base["anchor"])
                base["anchor"] = anchor if anchor in ANCHORS else base["anchor"]
            kinds_out[kind] = base
        result_boxes[name] = {"volume": _clamp(box.get("volume") if isinstance(box, dict) else None, 0, 100, 100),
                              "kinds": kinds_out}
    if result_boxes:
        out["boxes"] = result_boxes
    if "main" not in out["boxes"]:
        out["boxes"]["main"] = default_config()["boxes"]["main"]
    lanes_in = raw.get("lanes") if isinstance(raw.get("lanes"), dict) else {}
    lanes = copy.deepcopy(DEFAULT_LANES)
    for name, lane in lanes_in.items():
        name = str(name).strip().lower()[:24]
        if name and name.replace("_", "").isalnum() and isinstance(lane, dict):
            lanes[name] = {"max_queue": _clamp(lane.get("max_queue"), 1, 200, 10)}
    # every lane a kind refers to exists
    for box in out["boxes"].values():
        for k in box["kinds"].values():
            lanes.setdefault(k["lane"], {"max_queue": 10})
    out["lanes"] = lanes
    return out


def kinds_catalog() -> List[dict]:
    """What the layout page and the sources page show about each kind."""
    return [{"kind": k, "label": d["label"], "events": list(d["events"]), "legacy": d["legacy"],
             "defaults": _kind_defaults(k), "sample": d["sample"]} for k, d in KINDS.items()]


class AlertBox:
    def __init__(self, overlay_manager, path: Path | str):
        self.overlay = overlay_manager
        self.path = Path(path)
        self._config = self._load()
        self._recent: Dict[str, List[dict]] = {}
        self._seq = 0
        self.stats = {"alerts": 0, "errors": 0, "last_error": ""}
        if overlay_manager is not None:
            overlay_manager.add_event_listener(self.on_event)

    # ── config ────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if self.path.exists():
                return validate_config(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"[AlertBox] could not read {self.path}: {e}; using defaults")
        cfg = default_config()
        self._write(cfg)
        return cfg

    def _write(self, cfg: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except OSError as e:
            self._error(f"could not write {self.path}: {e}")

    def config(self) -> dict:
        return copy.deepcopy(self._config)

    async def save_config(self, raw: Any) -> dict:
        """Validate, write, and push the new placements to every connected
        box so the change shows on the next alert without a reload."""
        self._config = validate_config(raw)
        self._write(self._config)
        for box in self._config["boxes"]:
            await self._push_config(box)
        return self.config()

    def boxes(self) -> List[str]:
        return list(self._config["boxes"].keys())

    def box_config(self, box: str) -> Optional[dict]:
        b = self._config["boxes"].get(box)
        if b is None:
            return None
        return {"box": box, "volume": int(b.get("volume", 100)), "kinds": copy.deepcopy(b["kinds"]),
                "lanes": copy.deepcopy(self._config["lanes"]), "canvas": {"w": CANVAS_W, "h": CANVAS_H}}

    async def _push_config(self, box: str) -> None:
        cfg = self.box_config(box)
        if cfg is not None and self.overlay is not None:
            await self.overlay.broadcast(f"alerts:{box}", "alerts_config", cfg)

    async def on_connect(self, box: str) -> None:
        """A box page connected (or reconnected): give it its config."""
        await self._push_config(box)

    # ── alerts ────────────────────────────────────────────────────────

    def _error(self, msg: str) -> None:
        self.stats["errors"] += 1
        self.stats["last_error"] = msg
        print(f"[AlertBox] {msg}")

    @staticmethod
    def kinds_for_event(event: str) -> List[str]:
        return [k for k, d in KINDS.items() if event in d["events"]]

    def _build(self, box: str, kind: str, data: Any, event: str, test: bool = False) -> dict:
        self._seq += 1
        bcfg = self._config["boxes"][box]
        kc = bcfg["kinds"][kind]
        box_vol = int(bcfg.get("volume", 100))
        return {"id": self._seq, "box": box, "kind": kind, "event": event, "ts": time.time(), "test": test,
                "data": data if data is not None else {},
                "lane": kc["lane"], "duration": kc["duration"], "sound": kc["sound"],
                # effective volume = the kind's slider scaled by the box's master slider
                "volume": int(round(kc["volume"] * box_vol / 100)), "kind_volume": kc["volume"], "box_volume": box_vol,
                "placement": {"x": kc["x"], "y": kc["y"], "w": kc["w"], "h": kc["h"], "anchor": kc["anchor"]}}

    async def _send(self, alert: dict) -> None:
        box = alert["box"]
        self._recent.setdefault(box, []).append(alert)
        del self._recent[box][:-RECENT]
        self.stats["alerts"] += 1
        if self.overlay is not None:
            try:
                await self.overlay.broadcast(f"alerts:{box}", "alert", alert)
            except Exception as e:
                self._error(f"send {alert['kind']} to {box}: {e}")

    async def on_event(self, event: str, data: Any) -> None:
        """OverlayManager listener: one alert per enabled (box, kind)."""
        for kind in self.kinds_for_event(event):
            for box, bcfg in self._config["boxes"].items():
                if bcfg["kinds"][kind]["enabled"]:
                    await self._send(self._build(box, kind, data, event))

    async def test(self, kind: str, box: str = "main", data: Any = None) -> dict:
        """Fire a sample alert at one box, enabled or not (it is a test)."""
        if kind not in KINDS:
            return {"ok": False, "error": f"unknown kind '{kind}'"}
        if box not in self._config["boxes"]:
            return {"ok": False, "error": f"unknown box '{box}'"}
        alert = self._build(box, kind, data if data is not None else copy.deepcopy(KINDS[kind]["sample"]),
                            KINDS[kind]["events"][0], test=True)
        await self._send(alert)
        return {"ok": True, "alert": alert, "clients": self._clients(box)}

    async def replay(self, alert_id: int, box: Optional[str] = None) -> dict:
        for b, items in self._recent.items():
            if box and b != box:
                continue
            for a in items:
                if a["id"] == int(alert_id):
                    again = self._build(b, a["kind"], a["data"], a["event"], test=True)
                    await self._send(again)
                    return {"ok": True, "alert": again}
        return {"ok": False, "error": "alert not in the recent list"}

    def recent(self, box: str = "main", limit: int = 20) -> List[dict]:
        items = self._recent.get(box, [])
        return [{k: v for k, v in a.items() if k != "data"} | {"summary": _summary(a)}
                for a in items[-limit:]][::-1]

    # ── scene screenshot (the layout editor's background) ─────────────

    def background_path(self) -> Optional[Path]:
        for ext in BACKGROUND_EXTS:
            cand = self.path.parent / f"{BACKGROUND_NAME}.{ext}"
            if cand.exists():
                return cand
        return None

    def set_background(self, data: bytes, ext: str) -> dict:
        ext = ext.lower().lstrip(".")
        if ext == "jpeg":
            ext = "jpg"
        if ext not in BACKGROUND_EXTS:
            return {"ok": False, "error": "png, jpg or webp only"}
        if not data or len(data) > 20 * 1024 * 1024:
            return {"ok": False, "error": "image missing or over 20 MB"}
        self.clear_background()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            (self.path.parent / f"{BACKGROUND_NAME}.{ext}").write_bytes(data)
        except OSError as e:
            return {"ok": False, "error": f"could not save: {e}"}
        return {"ok": True, "bytes": len(data), "ext": ext}

    def clear_background(self) -> dict:
        removed = 0
        for ext in BACKGROUND_EXTS:
            cand = self.path.parent / f"{BACKGROUND_NAME}.{ext}"
            try:
                if cand.exists():
                    cand.unlink()
                    removed += 1
            except OSError:
                pass
        return {"ok": True, "removed": removed}

    def _clients(self, box: str) -> int:
        try:
            return int(self.overlay.client_count(f"alerts:{box}")) if self.overlay is not None else 0
        except Exception:
            return 0

    def status(self) -> dict:
        return {"boxes": {b: {"clients": self._clients(b),
                              "enabled": [k for k, v in cfg["kinds"].items() if v["enabled"]]}
                          for b, cfg in self._config["boxes"].items()},
                "lanes": copy.deepcopy(self._config["lanes"]), "kinds": kinds_catalog(),
                "file": str(self.path), "background": self.background_path() is not None, **self.stats}


def _summary(alert: dict) -> str:
    """One line for the Recent list."""
    d = alert.get("data") or {}
    kind = alert["kind"]
    try:
        if kind == "gamble":
            return f"{d.get('player')} {d.get('type')} roll {d.get('roll')}"
        if kind == "tts":
            return f"{d.get('user')}: {str(d.get('message') or '')[:60]}"
        if kind == "voiceline":
            return f"{d.get('god')} {str(d.get('line') or '')[:50]}"
        if kind == "cocaster":
            return str(d.get("text") or "")[:70]
        if kind == "dividend":
            return f"{d.get('god_name') or d.get('god')} paid {d.get('total_hats')} Hats"
        if kind == "match_end":
            return f"{d.get('god')} {d.get('outcome')} {d.get('kda')}"
        if kind in ("tradefeed", "tradefeed_rolling"):
            return f"{d.get('display_name') or d.get('username')} {d.get('type')} {d.get('shares')} {d.get('god')}"
        if kind == "portfolio":
            return f"{d.get('display_name') or d.get('username')} portfolio"
        if kind == "spin":
            return f"spin: {d.get('chosen')}"
        if kind == "bingo_call":
            return f"call: {(d.get('call') or {}).get('label')}"
        if kind == "bingo_claim":
            w = d.get("winner") or {}
            return f"BINGO {w.get('display')} {w.get('prize')} Hats"
        if kind == "bingo_open":
            return f"round {d.get('round_id')} opened, pot {d.get('pot')}"
        if kind == "bingo_closed":
            return f"round {d.get('round_id')} closed ({d.get('reason')})"
    except Exception:
        pass
    return kind
