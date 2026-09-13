"""
core/sources.py — the registry of OBS browser sources the bot serves.

One list, used by the dashboard's /sources page (URLs, sizes, connected
clients, Test buttons) and by tools/gen_readme_sources.py for the README
list, so the two cannot drift. Groups:

  alert_box   the Hatmaster Alert Box (core/alert_box.py); one entry per box
  persistent  overlays that sit on screen and update (ruled by
              core/overlay_rules.json)
  legacy      transient overlays folded into the alert box; still served so
              nothing breaks the day James switches.
              TODO(alertbox): remove once every kind is enabled in the box.

The economy overlays have no /overlay/<name> handler: they are served by the
/overlays/ static mount (README and HATMASBOT.md used to list /overlay/economy_*
URLs that were never registered).

`key` is the overlay name the page connects with (OverlayManager.client_count);
`test` is the event + sample data the Test button emits through the real
overlay manager. Alert-box rows test through AlertBox.test instead.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.alert_box import KINDS

DASHBOARD = "http://localhost:8069"

SOURCES: List[Dict[str, Any]] = [
    # ── persistent ────────────────────────────────────────────────────
    {"key": "now_playing", "group": "persistent", "name": "Now playing", "path": "/overlay/nowplaying",
     "size": (520, 140), "what": "Current Spotify track with the requester; hides when the queue empties.",
     "test": ("song_change", {"title": "Test track", "artist": "Hatmas", "requested_by": "Dyna", "album_art": ""})},
    {"key": "god_overlay", "group": "persistent", "name": "God overlay", "path": "/overlay/god",
     "size": (420, 120), "what": "The god detected in the lobby, from detection to match end.",
     "test": ("god_detected", {"god": "Ymir", "confidence": 0.98})},
    {"key": "deaths", "group": "persistent", "name": "Death counter", "path": "/overlay/deaths",
     "size": (240, 90), "what": "Deaths this stream; always on while the bot runs.",
     "test": ("bot_ready", {})},
    {"key": "economy_ticker", "group": "persistent", "name": "Economy ticker", "path": "/overlays/economy_ticker.html",
     "size": (1920, 48), "what": "Scrolling god stock prices; always on while the bot runs.",
     "test": ("bot_ready", {})},
    {"key": "economy_match_live", "group": "persistent", "name": "Live match panel", "path": "/overlays/economy_match_live.html",
     "size": (420, 160), "what": "The current god's price moving with the K/D/A during a match.",
     "test": ("economy_god_detected", {"god": "Ymir", "price": 120})},
    {"key": "youtube_player", "group": "persistent", "name": "YouTube player", "path": "/overlay/youtube_player",
     "size": (640, 360), "what": "Plays viewer-requested YouTube videos.",
     "test": None},
    {"key": "bingo", "group": "persistent", "name": "Bingo status", "path": "/overlays/bingo.html",
     "size": (400, 260), "what": "Pot, cards in play, last call, closest player, winner. Visible while a round is open.",
     "test": ("bingo_call", {"round_id": 7, "pot": 525, "cards": 9, "players": 6,
                             "call": {"label": "Says \"no mana\""}, "leaders": [{"display": "Dyna", "to_bingo": 1}]}),
     "settings": "/bingo"},
    {"key": "bingo_cards", "group": "persistent", "name": "Bingo cards", "path": "/overlay/bingo_cards",
     "size": (420, 520), "what": "Rotates through the cards of viewers who ticked \"Show my card on stream\".",
     "test": None, "settings": "/bingo"},
    # ── legacy (folded into the alert box) ────────────────────────────
    {"key": "sound_alerts", "group": "legacy", "name": "Sound & gamble alerts", "path": "/overlay/sound_alerts",
     "size": (420, 200), "what": "Gamble results with the dice roll and sounds.", "kind": "gamble"},
    {"key": "tts", "group": "legacy", "name": "TTS", "path": "/overlay/tts",
     "size": (640, 220), "what": "Highlighted-message text to speech.", "kind": "tts"},
    {"key": "voicelines", "group": "legacy", "name": "Voicelines", "path": "/overlay/voicelines",
     "size": (480, 360), "what": "Redeemed god voicelines with the clip.", "kind": "voiceline"},
    {"key": "cocaster", "group": "legacy", "name": "Co-caster line", "path": "/overlays/cocaster.html",
     "size": (1280, 120), "what": "Lower-third caption on multikills and deaths.", "kind": "cocaster"},
    {"key": "economy_dividend", "group": "legacy", "name": "Dividend paid", "path": "/overlays/economy_dividend.html",
     "size": (440, 200), "what": "A god's dividend payout.", "kind": "dividend"},
    {"key": "economy_match_end", "group": "legacy", "name": "Match end recap", "path": "/overlays/economy_match_end.html",
     "size": (560, 420), "what": "Result, K/D/A, price move and the movers.", "kind": "match_end"},
    {"key": "economy_leaderboard", "group": "legacy", "name": "Leaderboard", "path": "/overlays/economy_leaderboard.html",
     "size": (440, 540), "what": "Top portfolios on request.", "kind": "leaderboard"},
    {"key": "economy_tradefeed", "group": "legacy", "name": "Trade feed", "path": "/overlays/economy_tradefeed.html",
     "size": (440, 120), "what": "Buys and sells as they happen.", "kind": "tradefeed"},
    {"key": "economy_portfolio", "group": "legacy", "name": "Portfolio", "path": "/overlays/economy_portfolio.html",
     "size": (480, 420), "what": "A viewer's holdings on request.", "kind": "portfolio"},
    {"key": "god_pool_spin", "group": "legacy", "name": "Spin wheel", "path": "/overlay/spin",
     "size": (960, 540), "what": "The god pool spin animation.", "kind": "spin"},
]


def registry(alert_box=None, overlay_manager=None, base: str = DASHBOARD) -> List[dict]:
    """The sources with live client counts and full URLs, alert boxes first."""
    rows: List[dict] = []
    if alert_box is not None:
        for box in alert_box.boxes():
            cfg = alert_box.box_config(box) or {"kinds": {}}
            enabled = [k for k, v in cfg["kinds"].items() if v["enabled"]]
            rows.append({"key": f"alerts:{box}", "group": "alert_box", "name": f"Hatmaster Alert Box ({box})",
                         "path": f"/overlay/alerts?box={box}", "url": f"{base}/overlay/alerts?box={box}",
                         "size": [1920, 1080],
                         "what": (f"{len(enabled)} of {len(KINDS)} kinds enabled: " + ", ".join(
                             KINDS[k]["label"] for k in enabled)) if enabled else "No kinds enabled yet.",
                         "clients": _clients(overlay_manager, f"alerts:{box}"),
                         "health": health_of(overlay_manager, f"alerts:{box}"),
                         "settings": f"/alerts/layout?box={box}", "test": bool(enabled), "kinds": enabled})
    for s in SOURCES:
        row = {k: v for k, v in s.items() if k != "test"}
        row["url"] = base + s["path"]
        row["size"] = list(s["size"])
        row["clients"] = _clients(overlay_manager, s["key"])
        row["health"] = health_of(overlay_manager, s["key"])
        row["test"] = s.get("test") is not None
        if s["group"] == "legacy":
            row["replaced_by"] = KINDS[s["kind"]]["label"]
        rows.append(row)
    return rows


def find(key: str) -> Optional[dict]:
    for s in SOURCES:
        if s["key"] == key:
            return s
    return None


DOWN_AFTER = 60      # seconds without a client, after having had one, before a source is "down"


def health_of(overlay_manager, key: str, now: Optional[float] = None) -> dict:
    """One of: never (no client has ever connected since the bot started),
    ok (connected), down (had a client, none for DOWN_AFTER s), reconnecting
    (dropped less than DOWN_AFTER s ago). Plus how long ago it was last
    sent anything."""
    import time as _t
    now = _t.time() if now is None else now
    h = {"clients": 0, "connected_at": None, "disconnected_at": None, "sent_at": None}
    try:
        if overlay_manager is not None:
            h.update(overlay_manager.health(key))
    except Exception:
        pass
    if h["clients"]:
        state = "ok"
    elif h["connected_at"] is None:
        state = "never"
    elif h["disconnected_at"] and now - h["disconnected_at"] < DOWN_AFTER:
        state = "reconnecting"
    else:
        state = "down"
    return {"state": state, "clients": h["clients"],
            "sent_ago": None if h["sent_at"] is None else max(0, int(now - h["sent_at"])),
            "down_for": None if state != "down" or not h["disconnected_at"] else int(now - h["disconnected_at"])}


def _clients(overlay_manager, key: str) -> int:
    try:
        return int(overlay_manager.client_count(key)) if overlay_manager is not None else 0
    except Exception:
        return 0


def readme_lines(base: str = DASHBOARD) -> List[str]:
    """Markdown bullets for README's OBS browser sources section."""
    out = [f"- **Hatmaster Alert Box** (gamble, TTS, voicelines, economy, spin, bingo alerts; configure at "
           f"`{base}/alerts/layout`): `{base}/overlay/alerts?box=main` (1920x1080, full scene)"]
    for s in SOURCES:
        if s["group"] == "persistent":
            out.append(f"- **{s['name']}**: `{base}{s['path']}` ({s['size'][0]}x{s['size'][1]})")
    out.append(f"- Legacy sources (folded into the alert box, still served for now): "
               + ", ".join(f"`{s['path']}`" for s in SOURCES if s["group"] == "legacy"))
    out.append(f"- The full list with live status: `{base}/sources`")
    return out
