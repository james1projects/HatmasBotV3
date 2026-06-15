"""
test_web_trade.py — regression tests for website login + trading
================================================================

Spins up the real PublicWebServer (aiohttp TestServer, no port bound)
with a fake economy plugin and exercises POST /api/trade guard by
guard, plus /api/me and /auth/logout. The session tokens are real
(core/web_session.py); only the economy and Twitch OAuth network
calls are faked.

Run:
    python tools/test_web_trade.py            # whole suite
    python tools/test_web_trade.py cooldown   # name filter

Exit 0 on full pass. Same conventions as test_priority_request.py.
No network, no files, no real config required — module globals are
patched per-test before the server is constructed.
"""

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


# ──────────────────────────────────────────────────────────────────────
#   FAKES + HARNESS
# ──────────────────────────────────────────────────────────────────────

class FakeEconomy:
    """Mirror of the EconomyPlugin surface /api/trade touches."""

    def __init__(self):
        self._db = object()       # non-None = DB ready
        self._connected = True    # MixItUp up
        self._prices = {"Ymir": 200.0, "Loki": 100.0}
        self.balances = {"viewer1": 1000}
        self.holdings = {("viewer1", "Ymir"): {"shares": 5.0}}
        self.trades = []          # (action, login, god, amount, channel)
        self.bot = types.SimpleNamespace(
            is_feature_enabled=lambda f: self.features.get(f, False))
        self.features = {"web_trading": True}
        self._active = 0
        self.overlapped = False   # set True if two trades ever overlap

    def _resolve_god_name(self, raw):
        for god in self._prices:
            if god.lower() == (raw or "").lower():
                return god
        return None

    async def _get_balance(self, login):
        return self.balances.get(login)

    async def _get_holding(self, login, god):
        return self.holdings.get((login, god))

    async def _trade(self, action, login, god, amount, channel):
        self._active += 1
        if self._active > 1:
            self.overlapped = True
        await asyncio.sleep(0.02)
        self._active -= 1
        self.trades.append((action, login, god, amount, channel))
        return {"success": True, "shares": amount / self._prices[god],
                "price": self._prices[god], "god_name": god,
                "total_cost": amount, "net_received": amount, "fee": 0}

    async def execute_buy(self, login, god, amount, channel="chat"):
        return await self._trade("buy", login, god, amount, channel)

    async def execute_sell(self, login, god, amount, channel="chat"):
        return await self._trade("sell", login, god, amount, channel)

    async def get_leaderboard_hidden(self, login):
        return bool(self.hidden_users and login in self.hidden_users)

    async def set_leaderboard_hidden(self, login, hidden):
        self.hidden_users = getattr(self, "hidden_users", set())
        (self.hidden_users.add(login) if hidden
         else self.hidden_users.discard(login))


def patch_config(trading=True, cooldown=0, max_per_min=1000):
    """Patch the module globals the server reads. Must run BEFORE the
    server is constructed (init derives state from them)."""
    pw.WEB_SESSION_SECRET = SECRET
    pw.WEB_TRADING_ENABLED = trading
    pw.WEB_TRADE_COOLDOWN = cooldown
    pw.WEB_TRADE_MAX_PER_MIN = max_per_min
    pw.WEB_OAUTH_REDIRECT_URI = f"{ORIGIN}/auth/twitch/callback"
    pw.TWITCH_CLIENT_ID = "test_client_id"
    pw.TWITCH_CLIENT_SECRET = "test_client_secret"


class Harness:
    def __init__(self, client, economy, server):
        self.client, self.economy, self.server = client, economy, server

    async def trade(self, body, login="viewer1", session=True,
                    origin=ORIGIN, secret=SECRET, headers=None):
        h = dict(headers or {})
        if origin is not None:
            h["Origin"] = origin
        if session:
            token = ws.issue("1", login, login, secret=secret)
            h["Cookie"] = f"{ws.SESSION_COOKIE}={token}"
        return await self.client.post("/api/trade", json=body, headers=h)


async def make_harness(trading=True, cooldown=0, max_per_min=1000):
    patch_config(trading, cooldown, max_per_min)
    economy = FakeEconomy()
    server = pw.PublicWebServer(economy=economy)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    return Harness(client, economy, server)


BUY = {"action": "buy", "god": "ymir", "amount": 100}


# ──────────────────────────────────────────────────────────────────────
#   TESTS
# ──────────────────────────────────────────────────────────────────────

async def test_guard1_master_switch_off():
    h = await make_harness(trading=False)
    try:
        r = await h.trade(BUY)
        assert r.status == 503, r.status
        assert not h.economy.trades
    finally:
        await h.client.close()


async def test_guard1_feature_toggle_off():
    h = await make_harness()
    try:
        h.economy.features["web_trading"] = False
        r = await h.trade(BUY)
        assert r.status == 503, r.status
    finally:
        await h.client.close()


async def test_guard2_no_session():
    h = await make_harness()
    try:
        r = await h.trade(BUY, session=False)
        assert r.status == 401, r.status
    finally:
        await h.client.close()


async def test_guard2_forged_session():
    h = await make_harness()
    try:
        r = await h.trade(BUY, secret="attacker-made-up-secret")
        assert r.status == 401, r.status
        assert not h.economy.trades
    finally:
        await h.client.close()


async def test_guard3_bad_origin():
    h = await make_harness()
    try:
        r = await h.trade(BUY, origin="https://evil.example")
        assert r.status == 403, r.status
        r = await h.trade(BUY, origin=None)  # missing header
        assert r.status == 403, r.status
    finally:
        await h.client.close()


async def test_guard4_excluded_account():
    h = await make_harness()
    try:
        r = await h.trade(BUY, login="nightbot")
        assert r.status == 403, r.status
    finally:
        await h.client.close()


async def test_guard5_cooldown():
    h = await make_harness(cooldown=3)
    try:
        r1 = await h.trade(BUY)
        assert r1.status == 200, (r1.status, await r1.text())
        r2 = await h.trade(BUY)
        assert r2.status == 429, r2.status
        assert r2.headers.get("Retry-After"), "no Retry-After header"
        assert len(h.economy.trades) == 1
    finally:
        await h.client.close()


async def test_guard6_ip_rate_limit():
    h = await make_harness(max_per_min=3)
    try:
        statuses = [(await h.trade(BUY)).status for _ in range(4)]
        assert statuses[:3] == [200, 200, 200], statuses
        assert statuses[3] == 429, statuses
    finally:
        await h.client.close()


async def test_guard7_body_validation():
    h = await make_harness()
    try:
        cases = [
            {"action": "steal", "god": "ymir", "amount": 100},
            {"action": "buy", "god": "", "amount": 100},
            {"action": "buy", "god": "notagod", "amount": 100},
            {"action": "buy", "god": "ymir", "amount": "garbage"},
            {"action": "buy", "god": "ymir", "amount": 0},
            {"action": "buy", "god": "ymir", "amount": -50},
            {"action": "buy", "god": "ymir", "amount": None},
        ]
        for body in cases:
            r = await h.trade(body)
            assert r.status == 400, (body, r.status)
        # raw non-JSON body
        token = ws.issue("1", "viewer1", secret=SECRET)
        r = await h.client.post(
            "/api/trade", data=b"not json",
            headers={"Origin": ORIGIN,
                     "Cookie": f"{ws.SESSION_COOKIE}={token}",
                     "Content-Type": "application/json"})
        assert r.status == 400, r.status
        assert not h.economy.trades
    finally:
        await h.client.close()


async def test_guard9_market_closed():
    h = await make_harness()
    try:
        h.economy._connected = False  # MixItUp down
        r = await h.trade(BUY)
        assert r.status == 503, r.status
        h.economy._connected = True
        h.economy._db = None          # economy DB not ready
        r = await h.trade(BUY)
        assert r.status == 503, r.status
    finally:
        await h.client.close()


async def test_happy_buy_uses_web_channel():
    h = await make_harness()
    try:
        r = await h.trade(BUY)
        assert r.status == 200, await r.text()
        data = await r.json()
        assert data["ok"] and data["god"] == "Ymir"
        assert data["shares"] == 0.5  # 100 hats @ 200
        assert data["balance"] == 1000
        assert h.economy.trades == [("buy", "viewer1", "Ymir", 100, "web")]
        assert r.headers.get("Cache-Control") == "private, no-store"
    finally:
        await h.client.close()


async def test_buy_all_uses_balance():
    h = await make_harness()
    try:
        r = await h.trade({"action": "buy", "god": "ymir",
                           "amount": "all"})
        assert r.status == 200, await r.text()
        assert h.economy.trades[0][3] == 1000  # full balance
    finally:
        await h.client.close()


async def test_sell_all_without_holdings():
    h = await make_harness()
    try:
        r = await h.trade({"action": "sell", "god": "loki",
                           "amount": "all"})  # owns Ymir, not Loki
        assert r.status == 400, r.status
        r = await h.trade({"action": "sell", "god": "ymir",
                           "amount": "all"})
        assert r.status == 200, await r.text()
        assert h.economy.trades[0][3] == 1000  # 5 shares @ 200
    finally:
        await h.client.close()


async def test_per_user_lock_serializes():
    h = await make_harness()
    try:
        rs = await asyncio.gather(h.trade(BUY), h.trade(BUY),
                                  h.trade(BUY))
        assert all(r.status == 200 for r in rs)
        assert not h.economy.overlapped, \
            "two trades ran concurrently for the same user"
    finally:
        await h.client.close()


async def test_visibility_toggle_round_trip():
    h = await make_harness()
    try:
        token = ws.issue("1", "viewer1", secret=SECRET)
        hdrs = {"Origin": ORIGIN,
                "Cookie": f"{ws.SESSION_COOKIE}={token}",
                "Content-Type": "application/json"}
        # default visible
        r = await h.client.get("/api/me/settings",
                               headers={"Cookie": hdrs["Cookie"]})
        assert r.status == 200 and \
            (await r.json())["leaderboard_hidden"] is False
        # hide
        r = await h.client.post("/api/me/visibility",
                                json={"hidden": True}, headers=hdrs)
        assert r.status == 200 and \
            (await r.json())["leaderboard_hidden"] is True
        r = await h.client.get("/api/me/settings",
                               headers={"Cookie": hdrs["Cookie"]})
        assert (await r.json())["leaderboard_hidden"] is True
        # guards: no session / bad origin / bad body
        r = await h.client.post("/api/me/visibility",
                                json={"hidden": False},
                                headers={"Origin": ORIGIN})
        assert r.status == 401, r.status
        bad = dict(hdrs); bad["Origin"] = "https://evil.example"
        r = await h.client.post("/api/me/visibility",
                                json={"hidden": False}, headers=bad)
        assert r.status == 403, r.status
        r = await h.client.post("/api/me/visibility",
                                json={"hidden": "yes"}, headers=hdrs)
        assert r.status == 400, r.status
    finally:
        await h.client.close()


async def test_api_me_logged_in_and_out():
    h = await make_harness()
    try:
        r = await h.client.get("/api/me")
        assert r.status == 401, r.status
        assert (await r.json())["login_available"] is True

        token = ws.issue("99", "Viewer1", "Viewer1", secret=SECRET)
        r = await h.client.get(
            "/api/me",
            headers={"Cookie": f"{ws.SESSION_COOKIE}={token}"})
        assert r.status == 200, r.status
        data = await r.json()
        assert data["login"] == "viewer1"
        assert data["trading_enabled"] is True
        assert data["market_open"] is True
        assert r.headers.get("Cache-Control") == "private, no-store"
    finally:
        await h.client.close()


async def test_logout_clears_cookie():
    h = await make_harness()
    try:
        token = ws.issue("1", "viewer1", secret=SECRET)
        r = await h.client.post(
            "/auth/logout",
            headers={"Cookie": f"{ws.SESSION_COOKIE}={token}"})
        assert r.status == 200
        set_cookie = r.headers.get("Set-Cookie", "")
        assert ws.SESSION_COOKIE in set_cookie
        assert "Max-Age=0" in set_cookie or "expires" in set_cookie.lower()
    finally:
        await h.client.close()


async def test_auth_login_redirects_to_twitch():
    h = await make_harness()
    try:
        r = await h.client.get("/auth/login", allow_redirects=False)
        assert r.status == 302, r.status
        loc = r.headers.get("Location", "")
        assert loc.startswith("https://id.twitch.tv/oauth2/authorize")
        assert "state=" in loc and "client_id=test_client_id" in loc
        assert ws.OAUTH_STATE_COOKIE in r.headers.get("Set-Cookie", "")
    finally:
        await h.client.close()


async def test_callback_state_mismatch_403():
    h = await make_harness()
    try:
        r = await h.client.get(
            "/auth/twitch/callback?code=abc&state=forged",
            headers={"Cookie": f"{ws.OAUTH_STATE_COOKIE}=different"},
            allow_redirects=False)
        assert r.status == 403, r.status
    finally:
        await h.client.close()


async def test_stream_status_has_market_open():
    h = await make_harness()
    try:
        r = await h.client.get("/api/stream-status")
        data = await r.json()
        assert data["market_open"] is True, data
        h.economy._connected = False
        r = await h.client.get("/api/stream-status")
        assert (await r.json())["market_open"] is False
    finally:
        await h.client.close()


# ──────────────────────────────────────────────────────────────────────
#   RUNNER
# ──────────────────────────────────────────────────────────────────────

TESTS = [v for k, v in sorted(globals().items())
         if k.startswith("test_") and callable(v)]


def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    passed = failed = 0
    for fn in TESTS:
        if name_filter and name_filter not in fn.__name__:
            continue
        try:
            asyncio.run(fn())
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
        else:
            print(f"PASS  {fn.__name__}")
            passed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
