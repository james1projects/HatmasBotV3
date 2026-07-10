import asyncio
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from aiohttp.test_utils import TestServer, TestClient
except ImportError:
    print("Missing aiohttp. Install with: pip install aiohttp")
    sys.exit(1)

import core.public_webserver as pw
from core import web_session as ws

SECRET = "web-trade-test-secret-0123456789abcdef0123456789"
ORIGIN = "https://hatmaster.tv"

def patch_config(trading=True, cooldown=0, max_per_min=1000):
    pw.WEB_SESSION_SECRET = SECRET
    pw.WEB_TRADING_ENABLED = trading
    pw.WEB_TRADE_COOLDOWN = cooldown
    pw.WEB_TRADE_MAX_PER_MIN = max_per_min
    pw.WEB_OAUTH_REDIRECT_URI = f"{ORIGIN}/auth/twitch/callback"
    pw.TWITCH_CLIENT_ID = "test_client_id"
    pw.TWITCH_CLIENT_SECRET = "test_client_secret"

class FakeEconomy:
    def __init__(self):
        self.calls = []

    async def get_user_balance(self, user):
        return 100

    async def transfer(self, from_user, to_user, amount):
        self.calls.append((from_user, to_user, amount))
        return True

class FakeGodPool:
    def __init__(self):
        self._db = object()
        self.calls = []
        self.result = {"ok": True, "god": "Atlas", "votes": 1, "pool_size": 3}

    async def do_nominate(self, username, raw_god, is_broadcaster=False):
        self.calls.append((username, raw_god, is_broadcaster))
        return self.result

class Harness:
    def __init__(self, client, economy, server):
        self.client, self.economy, self.server = client, economy, server

    async def nominate(self, body, login="viewer1", session=True,
                       origin=ORIGIN, secret=SECRET, headers=None):
        h = dict(headers or {})
        if origin is not None:
            h["Origin"] = origin
        if session:
            token = ws.issue("1", login, login, secret=secret)
            h["Cookie"] = f"{ws.SESSION_COOKIE}={token}"
        return await self.client.post("/api/nominate", json=body, headers=h)

    async def nominate_raw(self, data, login="viewer1", session=True,
                           origin=ORIGIN, secret=SECRET, headers=None):
        h = dict(headers or {})
        if origin is not None:
            h["Origin"] = origin
        if session:
            token = ws.issue("1", login, login, secret=secret)
            h["Cookie"] = f"{ws.SESSION_COOKIE}={token}"
        h["Content-Type"] = "application/json"
        return await self.client.post("/api/nominate", data=data, headers=h)

async def make_harness(trading=True, cooldown=0, max_per_min=1000):
    patch_config(trading, cooldown, max_per_min)
    economy = FakeEconomy()
    fake_pool = FakeGodPool()
    bot = types.SimpleNamespace(plugins={"god_pool": fake_pool})
    server = pw.PublicWebServer(economy=None, bot=bot)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    return Harness(client, economy, server)

async def test_no_session():
    harness = await make_harness()
    try:
        resp = await harness.nominate({"god": "Atlas"}, session=False)
        assert resp.status == 401
        data = await resp.json()
        assert data["ok"] is False
        assert data["error"] == "Log in with Twitch first."
    finally:
        await harness.client.close()

async def test_forged_session():
    harness = await make_harness()
    try:
        h = {"Cookie": f"{ws.SESSION_COOKIE}=fake_token"}
        resp = await harness.nominate({"god": "Atlas"}, session=False, headers=h)
        assert resp.status == 401
        data = await resp.json()
        assert data["ok"] is False
        assert data["error"] == "Log in with Twitch first."
    finally:
        await harness.client.close()

async def test_bad_origin():
    harness = await make_harness()
    try:
        resp = await harness.nominate({"god": "Atlas"}, origin="evil.com")
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "Bad origin."

        resp = await harness.nominate({"god": "Atlas"}, origin=None)
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "Bad origin."
    finally:
        await harness.client.close()

async def test_excluded_account():
    harness = await make_harness()
    try:
        # Patch the excluded users set
        original = pw.EXCLUDED_USERS_LOWER
        pw.EXCLUDED_USERS_LOWER = {"nightbot"}
        try:
            resp = await harness.nominate({"god": "Atlas"}, login="nightbot")
            assert resp.status == 403
            data = await resp.json()
            assert data["error"] == "This account cannot nominate."
        finally:
            pw.EXCLUDED_USERS_LOWER = original
    finally:
        await harness.client.close()

async def test_ip_rate_limit():
    harness = await make_harness(max_per_min=3)
    try:
        # Make 4 requests, first 3 should succeed, 4th should fail
        for i in range(3):
            resp = await harness.nominate({"god": f"Atlas{i}"})
            assert resp.status == 200

        resp = await harness.nominate({"god": "Atlas3"})
        assert resp.status == 429
        data = await resp.json()
        assert data["error"] == "Too many requests."
    finally:
        await harness.client.close()

async def test_plugin_missing():
    harness = await make_harness()
    try:
        # Remove god_pool plugin
        del harness.server.bot.plugins["god_pool"]
        resp = await harness.nominate({"god": "Atlas"})
        assert resp.status == 503
        data = await resp.json()
        assert data["error"] == "Nominations are offline right now."
    finally:
        await harness.client.close()

async def test_plugin_db_none():
    harness = await make_harness()
    try:
        # Set _db to None
        harness.server.bot.plugins["god_pool"]._db = None
        resp = await harness.nominate({"god": "Atlas"})
        assert resp.status == 503
        data = await resp.json()
        assert data["error"] == "Nominations are offline right now."
    finally:
        await harness.client.close()

async def test_bad_json():
    harness = await make_harness()
    try:
        resp = await harness.nominate_raw(b"not json")
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "Bad JSON."
    finally:
        await harness.client.close()

async def test_missing_god():
    harness = await make_harness()
    try:
        resp = await harness.nominate({})
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "god is required."
    finally:
        await harness.client.close()

async def test_unknown_god():
    harness = await make_harness()
    try:
        harness.server.bot.plugins["god_pool"].result = {"ok": False, "reason": "unknown_god"}
        resp = await harness.nominate({"god": "Atlas"})
        assert resp.status == 400
        data = await resp.json()
        assert data["error"].startswith("Unknown god:")
    finally:
        await harness.client.close()

async def test_already_voted():
    harness = await make_harness()
    try:
        harness.server.bot.plugins["god_pool"].result = {"ok": False, "reason": "already_voted", "god": "Ymir"}
        resp = await harness.nominate({"god": "Atlas"})
        assert resp.status == 409
        data = await resp.json()
        assert "already nominated" in data["error"]
        assert "Ymir" in data["error"]
    finally:
        await harness.client.close()

async def test_happy_path():
    harness = await make_harness()
    try:
        fake_pool = harness.server.bot.plugins["god_pool"]
        resp = await harness.nominate({"god": "Atlas"})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["god"] == "Atlas"
        assert data["votes"] == 1
        assert data["pool_size"] == 3

        # Check that do_nominate was called correctly
        assert len(fake_pool.calls) == 1
        username, raw_god, is_broadcaster = fake_pool.calls[0]
        assert username == "viewer1"
        assert raw_god == "Atlas"
        assert is_broadcaster is False
    finally:
        await harness.client.close()

async def test_broadcaster_flag():
    harness = await make_harness()
    try:
        # Patch the channel config
        original_channel = pw._config.TWITCH_CHANNEL
        pw._config.TWITCH_CHANNEL = "hatmaster"
        try:
            fake_pool = harness.server.bot.plugins["god_pool"]
            resp = await harness.nominate({"god": "Atlas"}, login="hatmaster")
            assert resp.status == 200

            # Check that do_nominate was called with is_broadcaster=True
            assert len(fake_pool.calls) == 1
            username, raw_god, is_broadcaster = fake_pool.calls[0]
            assert username == "hatmaster"
            assert raw_god == "Atlas"
            assert is_broadcaster is True
        finally:
            pw._config.TWITCH_CHANNEL = original_channel
    finally:
        await harness.client.close()

async def main():
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)
             and name_filter in n]
    failed = 0
    for name, fn in tests:
        try:
            await fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    asyncio.run(main())
