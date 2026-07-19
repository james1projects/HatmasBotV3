"""Stdlib stub of the public webserver for front-end verification.

Serves public/ pages + canned JSON for every API the landing and
market pages call, so both pages can be rendered and their JS
exercised without starting the bot. Port 8071.
"""
import json
import http.server
from pathlib import Path

PUBLIC = Path(r"C:\Projects\HatmasBot\public")
PORT = 8071

GODS = {
    "gods": [
        {"name": "Ymir", "slug": "ymir", "price": 142.5, "games": 12,
         "wins": 8, "losses": 4, "kda_total": [80, 40, 60],
         "sparkline": [120, 125, 131, 128, 136, 142.5]},
        {"name": "Atlas", "slug": "atlas", "price": 98.0, "games": 5,
         "wins": 2, "losses": 3, "kda_total": [30, 25, 40],
         "sparkline": [110, 105, 101, 99, 98]},
        {"name": "Achilles", "slug": "achilles", "price": 100.0,
         "games": 0, "wins": 0, "losses": 0, "kda_total": [0, 0, 0],
         "sparkline": []},
    ]
}
LEADERBOARD = {
    "leaderboard": [
        {"rank": 1, "display_name": "ViewerOne", "platform": "twitch",
         "holdings_count": 4, "total_value": 5230,
         "url": "/twitch/viewerone"},
        {"rank": 2, "display_name": "TuberTwo", "platform": "youtube",
         "holdings_count": 2, "total_value": 3100,
         "url": "/yt/UC123"},
    ]
}
EVENTS = {
    "events": [
        {"kind": "match", "outcome": "win", "god": "Ymir",
         "delta": 4.2, "kda": [12, 3, 9],
         "ts": "2026-07-01 01:00:00"},
        {"kind": "trade", "trade": "buy", "actor": "ViewerOne",
         "god": "Ymir", "shares": 2.0, "price": 140,
         "ts": "2026-07-01 00:50:00"},
        {"kind": "dividend", "god": "Atlas", "total": 120,
         "holders": 6, "ts": "2026-06-30 23:00:00"},
    ]
}
SOCIAL_YT = {
    "videos": [
        {"video_id": "dQw4w9WgXcQ", "title": "Full Gameplay - Ymir",
         "thumbnail_url": "/hat.png",
         "published_at": "2026-06-29T12:00:00Z"},
    ]
}
SOCIAL_BSKY = {
    "profile": {"display_name": "Hatmaster",
                "handle": "hatmasteryt.bsky.social",
                "url": "https://bsky.app/profile/x", "avatar": ""},
    "handle": "hatmasteryt.bsky.social",
    "posts": [
        {"url": "https://bsky.app/x", "text": "Test post",
         "created_at": "2026-06-30T10:00:00Z", "reply_count": 1,
         "repost_count": 2, "like_count": 3, "image": ""},
    ],
    "cursor": "",
}

GOD_DETAIL = {
    "name": "Ymir",
    "slug": "ymir",
    "icon_url": "/god-icon/ymir",
    "price": 142.5,
    "lifetime": {"games": 12, "wins": 8, "losses": 4, "winrate": 0.667,
                 "kills": 80, "deaths": 40, "assists": 60, "kda_avg": 2.75},
    "history": [
        {"price": p, "event": "tick",
         "timestamp": f"2026-06-30 {10 + i // 6:02d}:{(i * 10) % 60:02d}:00"}
        for i, p in enumerate([120, 122, 125, 124, 131, 128, 126, 130,
                               136, 133, 138, 140, 142.5])
    ],
    "recent_matches": [
        {"match_id": "m3", "outcome": "win", "kills": 12, "deaths": 3,
         "assists": 9, "price_change": 4.2, "source": "live",
         "timestamp": "2026-06-30 12:00:00"},
        {"match_id": "m2", "outcome": "loss", "kills": 4, "deaths": 8,
         "assists": 11, "price_change": -3.1, "source": "live",
         "timestamp": "2026-06-30 11:00:00"},
        {"match_id": "m1", "outcome": "win", "kills": 9, "deaths": 2,
         "assists": 14, "price_change": 3.8, "source": "backfill",
         "timestamp": "2026-06-29 21:00:00"},
    ],
    "top_holders": {
        "all": [
            {"platform": "twitch", "name": "ViewerOne", "shares": 12.5,
             "value": 1781.25, "avg_cost": 118.0},
            {"platform": "youtube", "name": "TuberTwo", "shares": 6.25,
             "value": 890.63, "avg_cost": 0.0, "channel_id": "UC123"},
            {"platform": "twitch", "name": "ThirdFan", "shares": 2.0,
             "value": 285.0, "avg_cost": 131.0},
        ],
        "twitch": [
            {"platform": "twitch", "name": "ViewerOne", "shares": 12.5,
             "value": 1781.25, "avg_cost": 118.0},
            {"platform": "twitch", "name": "ThirdFan", "shares": 2.0,
             "value": 285.0, "avg_cost": 131.0},
        ],
        "youtube": [
            {"platform": "youtube", "name": "TuberTwo", "shares": 6.25,
             "value": 890.63, "avg_cost": 0.0, "channel_id": "UC123"},
        ],
    },
    "breakdown": {"base": 100, "winrate_pct": 18.4,
                  "winrate_contribution": 11.2, "volume_pct": 16.7,
                  "volume_contribution": 16.7, "kda_pct": 8.8,
                  "kda_contribution": 5.4, "confidence": 0.61},
}
PORTFOLIO = {
    "display_name": "ViewerOne",
    "total_cost": 1620.0,
    "rank": 1,
    "total_traders": 17,
    "holdings": [
        {"god": "Ymir", "shares": 12.5, "avg_cost": 118.0,
         "price": 142.5, "value": 1781.25},
        {"god": "Atlas", "shares": 3.0, "avg_cost": 104.0,
         "price": 98.0, "value": 294.0},
    ],
    "recent": [
        {"type": "buy", "god": "Ymir", "shares": 2.0, "price": 140,
         "ts": "2026-07-01 00:50:00"},
        {"type": "dividend", "god": "Ymir", "total": 85,
         "ts": "2026-06-30 23:00:00"},
        {"type": "free_share", "god": "Atlas", "shares": 1.0,
         "ts": "2026-06-30 20:00:00"},
    ],
}

ROUTES = {
    "/api/stream-status": {"is_live": False},
    "/api/gods": GODS,
    "/api/leaderboard": LEADERBOARD,
    "/api/recent-events": EVENTS,
    "/api/search": {"results": [
        {"display_name": "ViewerOne", "platform": "twitch",
         "url": "/twitch/viewerone"}]},
    "/api/social/youtube": SOCIAL_YT,
    "/api/social/tiktok": {"profile_url":
                           "https://www.tiktok.com/@awfulmasterhat"},
    "/api/social/bluesky": SOCIAL_BSKY,
    # Logged-in + trading-enabled so the buy/sell trade card renders.
    # login matches the portfolio the trade card gates on (/twitch/ymir_fan).
    "/api/me": {"logged_in": True, "login": "ymir_fan", "name": "Ymir_Fan",
                "img": "", "login_available": True, "uid": "12345",
                "prov": "tw", "yt_login_available": True,
                "yt_linked": True, "yt_linked_to": None,
                "trading_enabled": True, "market_open": True},
    "/api/me/balance": {"login": "ymir_fan", "balance": 8400,
                        "market_open": True},
    "/api/prices": {"prices": {"Ymir": 142.5, "Atlas": 98.0,
                               "Achilles": 100.0}},
    # Aspect-capable gods (real subset): gates the community page's
    # "Aspect" checkboxes. Ymir deliberately absent so the hidden
    # state is exercisable too.
    "/api/aspects": {"gods": ["Achilles", "Atlas"], "total": 2},
    # Queue + pool with one aspect entry each so the ASPECT pills and
    # icon badges render in dev.
    "/api/community": {
        "god_queue": [
            {"position": 1, "god": "Achilles", "requester": "ymir_fan",
             "requested_at": "2026-07-01T00:00:00", "token_spent": False,
             "source": "paid_priority", "message": "for the boys",
             "use_aspect": True},
            {"position": 2, "god": "Ymir", "requester": "frostfan",
             "requested_at": "2026-07-01T00:05:00", "token_spent": True,
             "source": "paid", "message": None, "use_aspect": False},
        ],
        "queue_total": 2,
        "god_pool": [
            {"god": "Atlas", "use_aspect": True, "added_by": "ymir_fan",
             "vote_count": 3, "added_at": "2026-07-01 00:00:00"},
            {"god": "Atlas", "use_aspect": False, "added_by": "frostfan",
             "vote_count": 1, "added_at": "2026-07-01 00:01:00"},
        ],
        "pool_total": 2,
    },
}

PAGES = {
    "/": "landing.html",
    "/market": "market.html",
    "/community": "community.html",
    "/priority-success": "priority-success.html",
    "/privacy": "privacy.html",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[stub]", fmt % args)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception:
            body = {}
        if path == "/api/trade":
            prices = ROUTES["/api/prices"]["prices"]
            god = body.get("god", "Ymir")
            price = prices.get(god, 100.0)
            bal = ROUTES["/api/me/balance"]["balance"]
            action = body.get("action", "buy")
            amt = body.get("amount")
            owned0 = next((h["shares"] for h in PORTFOLIO["holdings"]
                           if h["god"].lower() == god.lower()), 0.0)
            if isinstance(amt, str) and amt.strip().lower() == "all":
                hats = bal if action == "buy" else round(owned0 * price)
            else:
                try:
                    hats = int(str(amt).replace(",", ""))
                except Exception:
                    hats = 0
            shares = round(hats / price, 4) if price else 0
            owned = owned0 + shares if action == "buy" else max(0.0, owned0 - shares)
            self._json({"ok": True, "action": action, "god": god,
                        "shares": shares, "price": price, "total": hats,
                        "balance": bal, "holding_shares": round(owned, 4)})
            return
        if path == "/api/nominate":
            # Stateless stub: first nomination of the session succeeds,
            # repeats 409 like the real 1/day cap so the error path is
            # exercisable in dev.
            god = body.get("god") or "Ymir"
            aspect = bool(body.get("aspect"))
            already = getattr(Handler, "_nominated", None)
            if already:
                self._json({"ok": False,
                            "error": f"You already nominated {already} "
                                     f"today. Try again tomorrow."},
                           status=409)
                return
            Handler._nominated = god + (" (Aspect)" if aspect else "")
            self._json({"ok": True, "god": god, "use_aspect": aspect,
                        "votes": 1, "pool_size": 1})
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/Market":
            self.send_response(301)
            self.send_header("Location", "/market")
            self.end_headers()
            return
        if path in ROUTES:
            body = json.dumps(ROUTES[path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Dynamic routes: god detail page/API, portfolio page/API,
        # god icons (hat.png stands in for every portrait).
        parts = path.lstrip("/").split("/")
        if len(parts) == 4 and parts[:3] == ["api", "me", "holding"]:
            god = parts[3].replace("%20", " ")
            prices = ROUTES["/api/prices"]["prices"]
            shares = next((h["shares"] for h in PORTFOLIO["holdings"]
                           if h["god"].lower() == god.lower()), 0.0)
            self._json({"ok": True, "god": god, "shares": shares,
                        "avg_cost": 0, "price": prices.get(god)})
            return
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "god":
            data = dict(GOD_DETAIL)
            data["name"] = parts[2].replace("%20", " ")
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if (len(parts) == 3 and parts[0] == "api"
                and parts[1] in ("twitch", "yt")):
            body = json.dumps(PORTFOLIO).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        name = None
        if len(parts) == 2 and parts[0] == "god":
            name = "god.html"
        elif len(parts) == 2 and parts[0] in ("twitch", "yt"):
            name = "portfolio.html"
        elif len(parts) == 2 and parts[0] in ("god-icon",
                                              "custom-god-icon"):
            name = "hat.png"
        if name is None:
            name = PAGES.get(path)
        if name is None and "/" not in path.lstrip("/"):
            # static assets straight from public/ (theme.css, auth.js…)
            name = path.lstrip("/")
        f = PUBLIC / name if name else None
        if f and f.is_file():
            body = f.read_bytes()
            if f.suffix == ".html" and b"{{OG_" in body:
                # god.html ships {{OG_*}} placeholders that the real
                # server substitutes per request.
                for ph, val in ((b"{{OG_TITLE}}", b"Ymir: Hatmas Market"),
                                (b"{{OG_DESCRIPTION}}", b"stub preview"),
                                (b"{{OG_URL}}", b"http://localhost:8071/")):
                    body = body.replace(ph, val)
            ctype = ("text/html" if f.suffix == ".html" else
                     "text/css" if f.suffix == ".css" else
                     "application/javascript" if f.suffix == ".js" else
                     "image/png" if f.suffix == ".png" else
                     "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    print(f"stub site on http://127.0.0.1:{PORT}")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT),
                                    Handler).serve_forever()
