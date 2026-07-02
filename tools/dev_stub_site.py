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
    "/api/me": {"logged_in": False},
}

PAGES = {
    "/": "landing.html",
    "/market": "market.html",
    "/community": "community.html",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[stub]", fmt % args)

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
        name = PAGES.get(path)
        if name is None and "/" not in path.lstrip("/"):
            # static assets straight from public/ (theme.css, auth.js…)
            name = path.lstrip("/")
        f = PUBLIC / name if name else None
        if f and f.is_file():
            body = f.read_bytes()
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
