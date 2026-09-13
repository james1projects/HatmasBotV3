"""
test_web_profile_live.py — regression tests for /me + /live
===========================================================

Spins up the real PublicWebServer (aiohttp TestServer) with a fake
economy plugin, a seeded in-memory SQLite DB, and temp godreq fixture
files, then exercises the viewer profile aggregate (/api/me/profile),
the live snapshot (/api/live), the /ws/live broadcast bucket, and the
web_profile / web_live feature-toggle 404 contracts.

Run:
    python tests/test_web_profile_live.py            # whole suite
    python tests/test_web_profile_live.py toggle     # name filter

Exit 0 on full pass. Same conventions as test_web_trade.py — module
globals are patched per-test before the server is constructed.
"""

import asyncio
import json
import sys
import tempfile
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from aiohttp.test_utils import TestServer, TestClient
    import aiosqlite
except ImportError:
    print("Missing aiohttp/aiosqlite. Install with: pip install aiohttp aiosqlite")
    sys.exit(1)

import core.public_webserver as pw
from core import web_session as ws_mod
from core import users as _users

SECRET = "profile-live-test-secret-0123456789abcdef01234567"


# ──────────────────────────────────────────────────────────────────────
#   FAKES + HARNESS
# ──────────────────────────────────────────────────────────────────────

class FakeEconomy:
    def __init__(self):
        self._db = object()
        self._connected = True
        self._prices = {"Ymir": 231.0, "Loki": 95.0}
        self._match_god = "Ymir"
        self._match_kda = [7, 2, 5]
        self._match_start_price = 200.0
        self._match_price_series = [200, 213, 231]
        self.bot = types.SimpleNamespace(
            is_feature_enabled=lambda f: True)

    async def _get_balance(self, user_uuid):
        # mirrors the MixItUp bridge: no Twitch login -> no balance yet
        return None if user_uuid == "u-yt" else 12345


class FakeStreamStatus:
    def get_status(self):
        return {"is_live": True, "channel": "hatmaster",
                "title": "test stream", "viewer_count": 42,
                "current_god": "Ymir"}


# Viewer-keyed tables use user_uuid (docs/USER_IDENTITY_PLAN.md); the
# users / user_identities tables come from core.users.ensure_schema.
SCHEMA = """
CREATE TABLE god_prices (god_name TEXT PRIMARY KEY, price REAL);
CREATE TABLE portfolios (user_uuid TEXT, god_name TEXT, shares REAL,
  avg_cost REAL, PRIMARY KEY (user_uuid, god_name));
CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT, user_uuid TEXT, god_name TEXT, type TEXT, shares REAL,
  price REAL, total REAL, fee REAL, channel TEXT DEFAULT 'chat',
  timestamp TEXT, ref TEXT);
CREATE TABLE god_pool_votes (user_uuid TEXT, vote_date TEXT,
  god_name TEXT, voted_at TEXT, use_aspect INT DEFAULT 0,
  voter_username TEXT, PRIMARY KEY (user_uuid, vote_date));
CREATE TABLE priority_payments (stripe_session_id TEXT PRIMARY KEY,
  twitch_username TEXT, god TEXT, amount_cents INT, currency TEXT,
  created_at TEXT, status TEXT, played_at TEXT, user_uuid TEXT);
CREATE TABLE processed_matches (match_id TEXT PRIMARY KEY,
  god_name TEXT, outcome TEXT, kills INT, deaths INT, assists INT,
  price_change REAL, source TEXT, was_live_at_settle INT,
  processed_at TEXT, played_at TEXT);
"""

SEED = [
    "INSERT INTO users (uuid, display_name) VALUES ('u-1', 'Viewer One')",
    "INSERT INTO users (uuid, display_name) VALUES ('u-2', 'Rival')",
    "INSERT INTO users (uuid, display_name) VALUES ('u-yt', 'YT Viewer')",
    "INSERT INTO user_identities (provider, provider_id, user_uuid, login)"
    " VALUES ('twitch', '100', 'u-1', 'viewer1')",
    "INSERT INTO user_identities (provider, provider_id, user_uuid, login)"
    " VALUES ('twitch', '200', 'u-2', 'rival')",
    "INSERT INTO user_identities (provider, provider_id, user_uuid)"
    " VALUES ('youtube', 'UCabc', 'u-yt')",
    "INSERT INTO god_prices VALUES ('Ymir', 231.0)",
    "INSERT INTO god_prices VALUES ('Loki', 95.0)",
    "INSERT INTO portfolios VALUES ('u-1', 'Ymir', 12.0, 180.0)",
    "INSERT INTO portfolios VALUES ('u-2', 'Loki', 40.0, 90.0)",
    "INSERT INTO portfolios VALUES ('u-yt', 'Ymir', 2.0, 0.0)",
    "INSERT INTO transactions (user_uuid, god_name, type, shares, price,"
    " total, fee, channel, timestamp) VALUES"
    " ('u-1','Ymir','buy',12.0,180.0,2160.0,21.6,'web','2026-08-05T20:11:00')",
    "INSERT INTO transactions (user_uuid, god_name, type, shares, price,"
    " total, fee, channel, timestamp) VALUES"
    " ('u-1','Loki','sell',5.0,101.0,505.0,5.05,'chat','2026-08-02T21:40:00')",
    "INSERT INTO transactions (user_uuid, god_name, type, shares, price,"
    " total, fee, channel, timestamp) VALUES"
    " ('u-1','Ymir','dividend',0,220.0,86.0,0,'chat','2026-08-06T22:00:00')",
    "INSERT INTO transactions (user_uuid, god_name, type, shares, price,"
    " total, fee, channel, timestamp, ref) VALUES"
    " ('u-yt','Ymir','comment_share',1.0,200.0,200.0,0,'youtube','2026-08-01','vid1')",
    "INSERT INTO god_pool_votes (user_uuid, vote_date, god_name, voted_at)"
    " VALUES ('u-1','2026-08-07','Baron Samedi','2026-08-07T20:00:00')",
    "INSERT INTO priority_payments VALUES"
    " ('cs_1','viewer1','Achilles',500,'usd','2026-08-03','fulfilled','2026-08-03','u-1')",
    "INSERT INTO processed_matches VALUES"
    " ('m1','Ymir','win',9,3,7,12.0,'live',1,'2026-08-07T22:00:00',NULL)",
    "INSERT INTO processed_matches VALUES"
    " ('m2','Loki','loss',1,9,2,-11.0,'live',1,'2026-08-06T22:00:00',NULL)",
]

_TMP = Path(tempfile.mkdtemp(prefix="hatmas_profile_test_"))


def patch_config(features=None):
    pw.WEB_SESSION_SECRET = SECRET
    pw.TWITCH_CLIENT_ID = "test_client_id"
    pw.TWITCH_CLIENT_SECRET = "test_client_secret"
    pw.WEB_OAUTH_REDIRECT_URI = "https://hatmaster.tv/auth/twitch/callback"
    hist = _TMP / "hist.json"
    queue = _TMP / "queue.json"
    hist.write_text(json.dumps([
        {"god": "Ymir", "requester": "viewer1", "status": "played",
         "requested_at": "2026-08-01", "completed_at": "2026-08-01"},
        {"god": "Anubis", "requester": "other", "status": "played"},
    ]), encoding="utf-8")
    queue.write_text(json.dumps([
        {"god": "Loki", "requester": "other", "source": "paid"},
        {"god": "Achilles", "requester": "viewer1", "source": "paid",
         "use_aspect": True},
    ]), encoding="utf-8")
    pw.GODREQ_HISTORY_FILE = hist
    pw.GODREQ_QUEUE_FILE = queue


async def make_env(features=None):
    """Returns (client, server). Caller must close client + db."""
    patch_config()
    feats = {"web_profile": True, "web_live": True}
    if features is not None:
        feats.update(features)
    smite = types.SimpleNamespace(
        match_start_time=time.time() - 754,
        _session_wins=3, _session_losses=1, is_in_match=True)
    bot = types.SimpleNamespace(
        plugins={"smite": smite},
        is_feature_enabled=lambda f: feats.get(f, True))
    server = pw.PublicWebServer(
        economy=FakeEconomy(), stream_status=FakeStreamStatus(), bot=bot)
    db = await aiosqlite.connect(":memory:")
    await _users.ensure_schema(db)
    for stmt in SCHEMA.strip().split(";"):
        if stmt.strip():
            await db.execute(stmt)
    for stmt in SEED:
        await db.execute(stmt)
    await db.commit()
    server._db = db
    client = TestClient(TestServer(server.app))
    await client.start_server()
    return client, server, db


def tw_cookie(login="viewer1", user_uuid="u-1"):
    return {ws_mod.SESSION_COOKIE:
            ws_mod.issue("100", login, "Viewer One", "", SECRET,
                         user_uuid=user_uuid)}


def yt_cookie(channel_id="UCabc", user_uuid="u-yt"):
    return {ws_mod.SESSION_COOKIE:
            ws_mod.issue_youtube(channel_id, "YT Viewer", "", SECRET,
                                 user_uuid=user_uuid)}


# ──────────────────────────────────────────────────────────────────────
#   TESTS
# ──────────────────────────────────────────────────────────────────────

async def test_profile_requires_login():
    client, server, db = await make_env()
    try:
        res = await client.get("/api/me/profile")
        assert res.status == 401, f"expected 401, got {res.status}"
        body = await res.json()
        assert body["error"] == "not_logged_in"
    finally:
        await client.close(); await db.close()


async def test_profile_logged_in():
    client, server, db = await make_env()
    try:
        res = await client.get("/api/me/profile", cookies=tw_cookie())
        assert res.status == 200, f"expected 200, got {res.status}"
        p = await res.json()
        assert p["platform"] == "twitch"
        assert p["login"] == "viewer1"
        assert p["balance"] == 12345
        assert len(p["holdings"]) == 1
        h = p["holdings"][0]
        assert h["god"] == "Ymir" and h["shares"] == 12.0
        assert h["pl"] == round(12 * (231 - 180), 2)
        assert len(p["transactions"]) == 3
        assert all("channel" in t for t in p["transactions"])
        assert p["stats"]["trades"] == 2
        assert p["stats"]["buys"] == 1 and p["stats"]["sells"] == 1
        assert p["stats"]["dividends_earned"] == 86.0
        # godreq history filtered to this login only
        assert [r["god"] for r in p["god_requests"]] == ["Ymir"]
        pend = p["god_requests_pending"]
        assert len(pend) == 1 and pend[0]["position"] == 2
        assert pend[0]["use_aspect"] is True
        assert len(p["pool_votes"]) == 1
        assert p["priority_payments"][0]["god"] == "Achilles"
        assert p["priority_payments"][0]["amount_cents"] == 500
        assert res.headers.get("Cache-Control") == "private, no-store"
    finally:
        await client.close(); await db.close()


async def test_profile_yt_session():
    client, server, db = await make_env()
    try:
        res = await client.get("/api/me/profile", cookies=yt_cookie())
        assert res.status == 200, f"expected 200, got {res.status}"
        p = await res.json()
        assert p["platform"] == "youtube"
        assert p["login"] is None
        assert p["user_uuid"] == "u-yt"
        assert p["balance"] is None
        assert len(p["holdings"]) == 1
        assert p["holdings"][0]["god"] == "Ymir"
        assert len(p["transactions"]) == 1
        assert p["transactions"][0]["channel"] == "youtube"
        # a YouTube-only viewer ranks like everyone else
        assert p["rank"] is not None and p["total_traders"] == 3
    finally:
        await client.close(); await db.close()


async def test_profile_toggle_off():
    client, server, db = await make_env(features={"web_profile": False})
    try:
        res = await client.get("/me")
        assert res.status == 404, f"/me expected 404, got {res.status}"
        res = await client.get("/api/me/profile", cookies=tw_cookie())
        assert res.status == 404, f"api expected 404, got {res.status}"
    finally:
        await client.close(); await db.close()


async def test_live_snapshot():
    client, server, db = await make_env()
    try:
        res = await client.get("/api/live")
        assert res.status == 200, f"expected 200, got {res.status}"
        s = await res.json()
        assert s["stream"]["is_live"] is True
        assert s["match"]["active"] is True
        assert s["match"]["god"] == "Ymir"
        assert s["match"]["kda"] == {"k": 7, "d": 2, "a": 5}
        assert s["match"]["start_price"] == 200.0
        assert s["match"]["price"] == 231
        assert 750 <= s["match"]["duration_s"] <= 760
        assert s["record"] == {"wins": 3, "losses": 1}
        assert len(s["recent_matches"]) == 2
        assert s["recent_matches"][0]["outcome"] == "win"
    finally:
        await client.close(); await db.close()


async def test_live_toggle_off():
    client, server, db = await make_env(features={"web_live": False})
    try:
        for path in ("/live", "/api/live", "/ws/live"):
            res = await client.get(path)
            assert res.status == 404, f"{path} expected 404, got {res.status}"
    finally:
        await client.close(); await db.close()


async def test_live_ws_broadcast():
    client, server, db = await make_env()
    try:
        ws = await client.ws_connect("/ws/live")
        # in _LIVE_EVENTS → delivered
        await server._on_overlay_event(
            "god_stock_update_kd",
            {"god": "Ymir", "price": 235, "kda": {"k": 8, "d": 2, "a": 5}})
        msg = json.loads((await asyncio.wait_for(ws.receive(), 2)).data)
        assert msg["event"] == "god_stock_update_kd"
        assert msg["data"]["price"] == 235
        # stream lifecycle event → delivered
        await server._on_overlay_event("stream_live", {"title": "x"})
        msg = json.loads((await asyncio.wait_for(ws.receive(), 2)).data)
        assert msg["event"] == "stream_live"
        # NOT in _LIVE_EVENTS → not delivered (next receive must time out)
        await server._on_overlay_event("leaderboard_update", {"leaderboard": []})
        try:
            await asyncio.wait_for(ws.receive(), 0.3)
            assert False, "leaderboard_update should not reach /ws/live"
        except asyncio.TimeoutError:
            pass
        await ws.close()
    finally:
        await client.close(); await db.close()


async def test_hats_leaderboard():
    from core import wallet as W
    client, server, db = await make_env()
    try:
        await W.ensure_schema(db)
        await W.credit(db, "u-1", "hats", 300, "watch")
        await W.credit(db, "u-2", "hats", 1200, "watch")
        await W.credit(db, "u-yt", "hats", 50, "watch")
        await db.execute("UPDATE users SET watch_minutes = 125 WHERE uuid = 'u-2'")
        await db.commit()
        res = await client.get("/api/hats-leaderboard?limit=2")
        assert res.status == 200
        data = await res.json()
        rows = data["leaderboard"]
        assert [(r["rank"], r["display_name"], r["hats"]) for r in rows] == \
            [(1, "Rival", 1200), (2, "Viewer One", 300)], rows
        assert rows[0]["watch"] == "2h 5m" and rows[0]["watch_minutes"] == 125
        assert rows[1]["platform"] == "twitch" and rows[1]["url"] == "/twitch/viewer1"
        assert data["total_holders"] == 3 and data["currency"]
        # opt-out drops a viewer from the board and the count
        await _users.set_leaderboard_opt_out(db, "u-2", True)
        data = await (await client.get("/api/hats-leaderboard")).json()
        assert [r["display_name"] for r in data["leaderboard"]] == ["Viewer One", "YT Viewer"]
        assert data["total_holders"] == 2
        assert data["leaderboard"][1]["platform"] == "youtube"
    finally:
        await client.close(); await db.close()


async def test_pages_serve():
    client, server, db = await make_env()
    try:
        for path, marker in (("/me", "Your profile"),
                             ("/live", "Live trade feed")):
            res = await client.get(path)
            assert res.status == 200, f"{path} expected 200, got {res.status}"
            text = await res.text()
            assert marker in text, f"{path} missing marker {marker!r}"
    finally:
        await client.close(); await db.close()


# ──────────────────────────────────────────────────────────────────────
#   RUNNER
# ──────────────────────────────────────────────────────────────────────

TESTS = [
    test_profile_requires_login,
    test_profile_logged_in,
    test_profile_yt_session,
    test_profile_toggle_off,
    test_live_snapshot,
    test_live_toggle_off,
    test_live_ws_broadcast,
    test_pages_serve,
    test_hats_leaderboard,
]


async def main():
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    passed = failed = 0
    for test in TESTS:
        if name_filter and name_filter not in test.__name__:
            continue
        try:
            await test()
            print(f"ok  {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {test.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed or not passed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
