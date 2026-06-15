"""
Public Web Server (port 8070)
=============================
Read-only aiohttp app exposing YouTube viewer portfolios. Designed to
sit behind a Cloudflare Tunnel so anyone with the URL (e.g.
https://hatmaster.tv/yt/UCxxxx) can see their portfolio.

This is INTENTIONALLY a separate aiohttp app from `core/webserver.py`:
  * 8069 = control panel + overlays (private — accessible only on LAN /
    OBS browser sources). Has POST endpoints, can mutate state.
  * 8070 = THIS module — read-only public surface. Only GET routes.
    No way to mutate state. Even if Cloudflare's tunnel mis-routed,
    the worst case is leaking publicly-visible portfolio info — the
    dashboard literally cannot be reached because it's on a different
    port the tunnel doesn't know about.

Routes:
    GET  /                           landing / search-by-name
    GET  /yt/{channel_id}            portfolio HTML page
    GET  /api/yt/{channel_id}        JSON portfolio (for the page to fetch)
    GET  /api/search?q=<name>        JSON search by display name
    GET  /api/prices                 JSON: current prices for every god
    GET  /ws/yt/{channel_id}         WebSocket: live price/dividend ticks

Live updates: subscribes to the main bot's OverlayManager via
`add_event_listener` and forwards `god_stock_update` and `dividend_paid`
events to any WebSocket clients viewing the relevant portfolio.
"""

import asyncio
import json
import time
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from typing import Any, Dict, List, Optional, Set

import aiohttp
from aiohttp import web

try:
    import aiosqlite
except ImportError:
    aiosqlite = None

from core import db as _shared_db
from core.config import (
    BASE_DIR, DATA_DIR, ECONOMY_DB_PATH, ECONOMY_STARTING_PRICE, WEB_HOST,
    ECONOMY_EXCLUDED_USERNAMES, TWITCH_BOT_USERNAME,
    TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET,
    WEB_SESSION_SECRET, WEB_TRADING_ENABLED, WEB_TRADE_COOLDOWN,
    WEB_TRADE_MAX_PER_MIN, WEB_OAUTH_REDIRECT_URI,
    YOUTUBE_API_KEY, YOUTUBE_CHANNEL_ID,
    TIKTOK_USERNAME, TIKTOK_LATEST_VIDEO_URL, BLUESKY_HANDLE,
    SOCIAL_FEED_CACHE_TTL,
)
from core import web_session as _ws
from core import config as _config


def _excluded_usernames_lower():
    """Same list the economy plugin uses, computed once at import."""
    out = {u.lower() for u in (ECONOMY_EXCLUDED_USERNAMES or []) if u}
    if TWITCH_BOT_USERNAME and TWITCH_BOT_USERNAME != "YOUR_BOT_USERNAME":
        out.add(TWITCH_BOT_USERNAME.lower())
    return out


EXCLUDED_USERS_LOWER = _excluded_usernames_lower()


PUBLIC_PORT = 8070
PUBLIC_DIR = BASE_DIR / "public"
GOD_ICONS_DIR = BASE_DIR / "data" / "god_icons"
CUSTOM_GOD_ICONS_DIR = BASE_DIR / "Custom God Icons"
SUGGESTIONS_FILE = BASE_DIR / "data" / "suggestions.json"
GODREQ_QUEUE_FILE = BASE_DIR / "data" / "godreq_queue.json"


@web.middleware
async def _custom_404_middleware(request, handler):
    """Catch HTTPNotFound for HTML routes and serve our terminal-
    styled /public/404.html. API and WebSocket paths still get the
    default behavior (raw 404) so AJAX fetches can parse the error
    instead of receiving HTML they can't handle.

    Both code paths are covered: the handler returning a 404 response
    (e.g. `return web.Response(status=404, ...)`) and the handler
    raising `web.HTTPNotFound()` (which aiohttp does for unmatched
    routes). Either way, the middleware sees the 404 and rewrites.
    """
    try:
        response = await handler(request)
        if response.status != 404:
            return response
    except web.HTTPNotFound:
        pass  # fall through to the custom page below

    # API + WebSocket routes keep their JSON / raw 404 behavior.
    path = request.path or ""
    if path.startswith("/api/") or path.startswith("/ws/"):
        raise web.HTTPNotFound()

    file_path = PUBLIC_DIR / "404.html"
    if file_path.exists():
        return web.FileResponse(
            file_path, status=404,
            headers={"Cache-Control": "no-cache"})
    raise web.HTTPNotFound()


class PublicWebServer:
    """Read-only public aiohttp app on port 8070."""

    def __init__(self, overlay_manager=None, stream_status=None,
                 priority_request=None, economy=None, bot=None):
        self.app = web.Application(middlewares=[_custom_404_middleware])
        # HatmasBot or None — the /mod page reads/writes the command
        # registry through bot.get_command_catalog() /
        # set_command_platform(). Same process, no IPC.
        self.bot = bot
        self._helix_mod_cache = (0.0, set())  # (fetched_at, lowercase logins)
        self.runner: Optional[web.AppRunner] = None
        self.overlay_manager = overlay_manager  # used for live event forwarding
        self.stream_status = stream_status      # StreamStatusPlugin or None
        # PriorityRequestPlugin or None — owns Stripe Checkout Session
        # creation + webhook handling. We just expose the HTTP routes
        # and delegate; this server keeps the http-only concerns
        # (request parsing, status codes) while business logic lives
        # in the plugin.
        self.priority_request = priority_request
        # EconomyPlugin or None — website trading delegates to its
        # execute_buy/execute_sell so web + chat share ONE money path
        # (WEBSITE_TRADING_DESIGN.md §2.1). Read paths still hit the
        # DB directly; only trades go through the plugin.
        self.economy = economy
        self._db: Optional["aiosqlite.Connection"] = None

        # ── website login + trading state ──
        # Fail closed: no session secret (or placeholder Twitch app
        # creds) disables login AND trading, loudly.
        self._login_enabled = bool(
            WEB_SESSION_SECRET
            and TWITCH_CLIENT_ID not in ("", "YOUR_CLIENT_ID")
            and TWITCH_CLIENT_SECRET not in ("", "YOUR_CLIENT_SECRET"))
        # Secure cookies only when the site is served over https —
        # keeps the localhost dev flow working over plain http.
        self._cookie_secure = WEB_OAUTH_REDIRECT_URI.startswith("https://")
        _parts = urlsplit(WEB_OAUTH_REDIRECT_URI)
        self._allowed_origin = f"{_parts.scheme}://{_parts.netloc}"
        # Per-user trade locks: serialize each viewer's trades so two
        # browser tabs can't race the balance-check/deduct sequence in
        # execute_buy (chat never races — one message at a time).
        self._trade_locks = defaultdict(asyncio.Lock)
        # Social-feed cache: {key: (fetched_at, payload)}. Stale data
        # is served on upstream failure — a dead YouTube call should
        # never blank the tab for visitors.
        self._social_cache: Dict[str, tuple] = {}
        self._web_trade_cooldowns: Dict[str, float] = {}
        self._ip_window: Dict[tuple, int] = {}
        if not self._login_enabled:
            print("[PublicWebServer] website login disabled — set "
                  "WEB_SESSION_SECRET in config_local.py (and Twitch "
                  "client id/secret) to enable")

        # Per-portfolio WebSocket client sets, keyed on yt_channel_id.
        self._ws_clients: Dict[str, Set[web.WebSocketResponse]] = {}
        # Twitch portfolio WebSocket clients, keyed on lowercase username.
        self._twitch_ws_clients: Dict[str, Set[web.WebSocketResponse]] = {}
        # Per-god WebSocket client sets, keyed on canonical god name.
        self._god_ws_clients: Dict[str, Set[web.WebSocketResponse]] = {}
        self._send_lock = asyncio.Lock()

        self._setup_routes()
        if overlay_manager:
            overlay_manager.add_event_listener(self._on_overlay_event)

    def _setup_routes(self):
        # Static HTML pages — portfolio.html is shared between YT and
        # Twitch platforms; the page itself reads location.pathname to
        # know which platform it's serving.
        self.app.router.add_get("/", self._handle_landing)
        self.app.router.add_get("/yt/{channel_id}", self._handle_portfolio_page)
        self.app.router.add_get("/twitch/{username}", self._handle_portfolio_page)
        self.app.router.add_get("/god/{name}", self._handle_god_page)
        self.app.router.add_get("/community", self._handle_community_page)

        # JSON API.
        self.app.router.add_get("/api/yt/{channel_id}",
                                 self._handle_api_portfolio)
        self.app.router.add_get("/api/twitch/{username}",
                                 self._handle_api_twitch_portfolio)
        self.app.router.add_get("/api/search", self._handle_api_search)
        self.app.router.add_get("/api/prices", self._handle_api_prices)
        self.app.router.add_get("/api/gods", self._handle_api_gods)
        self.app.router.add_get("/api/god/{name}", self._handle_api_god)
        self.app.router.add_get("/api/stream-status",
                                 self._handle_api_stream_status)
        self.app.router.add_get("/api/community",
                                 self._handle_api_community)

        # Activity feed — drives the "Recent activity" strip on the
        # landing page. UNION-style merge across processed_matches,
        # youtube_transactions, dividends, youtube_portfolios, and
        # transactions, sorted by timestamp DESC, trimmed to N.
        self.app.router.add_get("/api/recent-events",
                                 self._handle_api_recent_events)

        # Combined cross-platform leaderboard. Drives the Top Traders
        # strip on /, and is read by the portfolio handlers below to
        # surface "you're #N of M" rank pills.
        self.app.router.add_get("/api/leaderboard",
                                 self._handle_api_leaderboard)

        # Pending YT nominations — admin-gated by COMMUNITY_ADMIN_TOKEN.
        # The GET surfaces the queue (used by the moderation card on
        # /community); the POSTs approve/reject a single row. All three
        # 401 if the X-Hatmas-Admin header doesn't match config.
        self.app.router.add_get("/api/community/pending-nominations",
                                 self._handle_api_pending_nominations)
        self.app.router.add_post(
            "/api/community/nominations/{nid}/approve",
            self._handle_api_nomination_approve)
        self.app.router.add_post(
            "/api/community/nominations/{nid}/reject",
            self._handle_api_nomination_reject)

        # Priority god request — viewer pays $5 on hatmaster.tv/community
        # to push a god request to the head of the queue. Three routes:
        #
        #   POST /api/priority-request/create — creates a Stripe
        #     Checkout Session and returns its URL for the browser
        #     to redirect to. Body: {god, twitch_username, message}.
        #
        #   POST /api/stripe-webhook — Stripe's signed callback after
        #     payment. Verified via webhook secret; on success the
        #     plugin pushes to the godrequest queue head.
        #
        #   GET /priority-success — static thank-you page Stripe
        #     redirects to after payment. Served from public/.
        #
        # All three 503 cleanly if the plugin isn't wired up or its
        # config is missing, so the website's "Pay $5" button just
        # returns a clear error rather than hanging.
        self.app.router.add_post("/api/priority-request/create",
                                 self._handle_priority_create)
        self.app.router.add_post("/api/stripe-webhook",
                                 self._handle_stripe_webhook)
        self.app.router.add_get("/priority-success",
                                 self._handle_priority_success_page)

        # Website login + trading — WEBSITE_TRADING_DESIGN.md. The
        # ONLY state-changing routes besides Stripe + admin-gated
        # nominations: OAuth callback (sets a cookie), logout (clears
        # it), and /api/trade (delegates to the economy plugin behind
        # nine guards). Everything else on this server stays GET.
        # ── hidden mod page (Crossplatform_Commands_Plan.md) ──
        # Every unauthorized state (logged out, non-mod) gets the same
        # 404 as a wrong URL — the page's existence is never revealed.
        self.app.router.add_get("/mod", self._handle_mod_page)
        self.app.router.add_get("/api/mod/commands",
                                self._handle_api_mod_commands_get)
        self.app.router.add_post("/api/mod/commands",
                                 self._handle_api_mod_commands_post)
        self.app.router.add_post("/api/mod/custom-commands",
                                 self._handle_api_mod_custom_post)
        self.app.router.add_delete("/api/mod/custom-commands/{name}",
                                   self._handle_api_mod_custom_delete)

        self.app.router.add_get("/auth/login", self._handle_auth_login)
        self.app.router.add_get("/auth/twitch/callback",
                                self._handle_auth_callback)
        self.app.router.add_post("/auth/logout", self._handle_auth_logout)
        self.app.router.add_get("/api/me", self._handle_api_me)
        self.app.router.add_get("/api/me/balance",
                                self._handle_api_me_balance)
        self.app.router.add_get("/api/me/settings",
                                self._handle_api_me_settings)
        self.app.router.add_post("/api/me/visibility",
                                 self._handle_api_me_visibility)
        self.app.router.add_post("/api/trade", self._handle_api_trade)

        # Social tabs (Social_Tabs_Plan.md) — read-only, cached 15 min.
        self.app.router.add_get("/api/social/youtube",
                                self._handle_social_youtube)
        self.app.router.add_get("/api/social/tiktok",
                                self._handle_social_tiktok)
        self.app.router.add_get("/api/social/bluesky",
                                self._handle_social_bluesky)

        # Serve god portrait icons from data/god_icons/ — we already
        # have a clean kebab-case .png for every god, more reliable
        # than tracker.gg's CDN which 404s on some slugs.
        self.app.router.add_get("/god-icon/{slug}", self._handle_god_icon)

        # Serve from "Custom God Icons/" — the same source the OBS
        # stream overlays use. Used by the live ticker tape so the
        # site visually matches what's on stream. Falls through to
        # /god-icon/{slug} if no custom icon exists.
        self.app.router.add_get("/custom-god-icon/{slug}",
                                 self._handle_custom_god_icon)

        # Shared CSS theme.
        self.app.router.add_get("/theme.css", self._handle_theme_css)
        self.app.router.add_get("/auth.js", self._handle_auth_js)

        # Static brand assets (generated by tools/build_site_meta.py).
        # favicon.ico is requested automatically by every browser; the
        # og-image is what Discord/Bluesky/Twitter render for links.
        for fname in ("og-image.png", "favicon.ico", "favicon-32.png",
                      "apple-touch-icon.png", "hat.png"):
            self.app.router.add_get(
                f"/{fname}", self._make_static_handler(fname))

        # WebSocket for live updates.
        self.app.router.add_get("/ws/yt/{channel_id}", self._handle_ws)
        self.app.router.add_get("/ws/twitch/{username}",
                                 self._handle_twitch_ws)
        self.app.router.add_get("/ws/god/{name}", self._handle_god_ws)

        # Health check.
        self.app.router.add_get("/healthz",
            lambda r: web.Response(text="ok"))

    # ──────────────────────────────────────────────────────────────────
    #   LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    async def start(self):
        if not _shared_db.is_available():
            print("[PublicWebServer] aiosqlite not installed — disabled.")
            return

        # Use the shared connection (managed by core.db / main.py).
        # NOTE: previously this opened its own connection with
        # PRAGMA query_only=ON as defense-in-depth. With the shared
        # connection we lose that PRAGMA — every consumer of this
        # connection can write. The public webserver's handlers are
        # reviewed as read-only (no INSERT/UPDATE/DELETE), so this is
        # a tradeoff: we sacrifice a per-connection safety check for
        # the simpler architecture (single statement cache, single
        # PRAGMA setup, schema migrations propagate to every reader).
        # If a write ever sneaks into a public handler it will succeed
        # rather than getting caught — code review is the new safety
        # net. The query patterns here are simple enough that this is
        # acceptable.
        self._db = await _shared_db.get_db()
        if self._db is None:
            print("[PublicWebServer] DB unavailable — init_db not run yet")
            return

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        # Bind to localhost only — Cloudflare Tunnel reaches us via
        # localhost from the cloudflared process running on this PC.
        # Anyone scanning your IP from outside hits the firewall, not us.
        site = web.TCPSite(self.runner, "127.0.0.1", PUBLIC_PORT)
        await site.start()
        print(f"[PublicWebServer] Read-only public app running at "
              f"http://127.0.0.1:{PUBLIC_PORT}")
        print(f"[PublicWebServer] Preview locally at "
              f"http://127.0.0.1:{PUBLIC_PORT}/")

    async def stop(self):
        # Close all open WebSocket clients across every keyed bucket.
        for bucket in (self._ws_clients, self._twitch_ws_clients,
                        self._god_ws_clients):
            for clients in list(bucket.values()):
                for ws in list(clients):
                    try:
                        await ws.close()
                    except Exception:
                        pass
            bucket.clear()

        if self.overlay_manager:
            self.overlay_manager.remove_event_listener(self._on_overlay_event)

        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        # Shared connection is closed by main.py / core.db at shutdown.
        # Just clear our reference.
        self._db = None

    # ──────────────────────────────────────────────────────────────────
    #   PAGE HANDLERS (HTML)
    # ──────────────────────────────────────────────────────────────────

    async def _handle_landing(self, request: web.Request) -> web.Response:
        path = PUBLIC_DIR / "landing.html"
        if not path.exists():
            return web.Response(
                text="Landing page missing.", status=500)
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    # ──────────────────────────────────────────────────────────────────
    #   HTML meta-tag injection helper
    # ──────────────────────────────────────────────────────────────────
    #
    # Social-media crawlers (Discord, Twitter, Slack, iMessage, etc.)
    # don't execute JS — they read meta tags out of the raw HTML. To
    # make each portfolio / god URL render as its own preview card,
    # we replace {{OG_TITLE}}, {{OG_DESCRIPTION}}, {{OG_URL}} tokens
    # in the file at request time and serve the result as Response
    # instead of a static FileResponse. The fields are HTML-escaped
    # to keep god names with apostrophes from breaking the markup.

    def _render_with_meta(self, path: Path, meta: Dict[str, str]
                          ) -> web.Response:
        """Read `path`, replace meta tokens, return text/html Response."""
        import html as _html
        try:
            body = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return web.Response(text="Page missing.", status=500)
        for key, val in meta.items():
            body = body.replace(
                "{{" + key + "}}",
                _html.escape(val or "", quote=True))
        return web.Response(
            text=body, content_type="text/html",
            headers={"Cache-Control": "no-cache"})

    async def _handle_portfolio_page(self, request: web.Request) -> web.Response:
        path = PUBLIC_DIR / "portfolio.html"
        if not path.exists():
            return web.Response(
                text="Portfolio page missing.", status=500)

        # Per-resource meta for social previews. We look up the
        # display name + rank cheaply (one query each) and stitch
        # together the title/description tokens.
        url_path = request.path or "/"
        display_name = "viewer"
        rank_blurb = ""
        try:
            if url_path.startswith("/yt/"):
                cid = url_path[len("/yt/"):]
                async with self._db.execute(
                        "SELECT yt_display_name FROM youtube_portfolios "
                        " WHERE yt_channel_id = ?", (cid,)) as cur:
                    row = await cur.fetchone()
                if row and row[0]:
                    display_name = row[0]
                rank, total = await self._portfolio_rank("youtube", cid)
                if rank and total:
                    rank_blurb = f" — ranked #{rank} of {total}"
            elif url_path.startswith("/twitch/"):
                display_name = url_path[len("/twitch/"):] or "viewer"
                rank, total = await self._portfolio_rank(
                    "twitch", display_name)
                if rank and total:
                    rank_blurb = f" — ranked #{rank} of {total}"
        except Exception as e:
            print(f"[PublicWebServer] portfolio meta lookup failed: {e}")

        title = f"{display_name} · Hatmas Market portfolio"
        desc = (f"{display_name}'s Smite 2 god portfolio on Hatmas Market"
                f"{rank_blurb}. Built share by share from YouTube "
                "comments and Twitch chat.")
        full_url = f"https://hatmaster.tv{url_path}"
        return self._render_with_meta(path, {
            "OG_TITLE":       title,
            "OG_DESCRIPTION": desc,
            "OG_URL":         full_url,
        })

    # ──────────────────────────────────────────────────────────────────
    #   JSON API
    # ──────────────────────────────────────────────────────────────────

    async def _handle_api_portfolio(self, request: web.Request) -> web.Response:
        """Return holdings + current value for a YouTube channel ID."""
        channel_id = request.match_info["channel_id"]
        if not channel_id or len(channel_id) > 64:
            return web.json_response({"error": "bad channel id"}, status=400)

        # Look up display name + first/last seen.
        async with self._db.execute("""
            SELECT yt_display_name, first_seen_at, last_seen_at
              FROM youtube_portfolios
             WHERE yt_channel_id = ?
        """, (channel_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return web.json_response({"error": "not found"}, status=404)
        display_name, first_seen, last_seen = row

        # Holdings + current price for each.
        holdings: List[Dict[str, Any]] = []
        async with self._db.execute("""
            SELECT h.god_name, h.shares, h.avg_cost, p.price
              FROM youtube_holdings h
              LEFT JOIN god_prices p ON p.god_name = h.god_name
             WHERE h.yt_channel_id = ? AND h.shares > 0.001
             ORDER BY h.god_name
        """, (channel_id,)) as cur:
            async for r in cur:
                god, shares, avg_cost, price = r
                # If god_prices has no row yet (god never played on
                # stream while bot was running), default to the starting
                # price so the portfolio still shows a sensible value
                # instead of 0. The avg_cost is also the starting price
                # in this case, so P&L renders as 0% — neutral.
                price = (float(price) if price is not None
                         else float(ECONOMY_STARTING_PRICE))
                value = shares * price
                cost_basis = shares * avg_cost
                pl = value - cost_basis
                pl_pct = (pl / cost_basis * 100.0) if cost_basis > 0 else 0.0
                holdings.append({
                    "god": god,
                    "shares": round(float(shares), 4),
                    "avg_cost": round(float(avg_cost), 2),
                    "price": round(price, 2),
                    "value": round(value, 2),
                    "pl": round(pl, 2),
                    "pl_pct": round(pl_pct, 2),
                })

        # Recent transactions for the activity feed.
        recent: List[Dict[str, Any]] = []
        async with self._db.execute("""
            SELECT god_name, type, shares, price, yt_video_id, timestamp
              FROM youtube_transactions
             WHERE yt_channel_id = ?
             ORDER BY timestamp DESC LIMIT 20
        """, (channel_id,)) as cur:
            async for r in cur:
                recent.append({
                    "god": r[0], "type": r[1],
                    "shares": round(float(r[2]), 4),
                    "price": round(float(r[3]), 2),
                    "video_id": r[4],
                    "timestamp": r[5],
                })

        total_value = sum(h["value"] for h in holdings)
        total_cost = sum(h["shares"] * h["avg_cost"] for h in holdings)
        total_pl = total_value - total_cost

        # Rank in the cross-platform leaderboard.
        rank, total_traders = await self._portfolio_rank("youtube", channel_id)

        return web.json_response({
            "channel_id":    channel_id,
            "display_name":  display_name,
            "first_seen_at": first_seen,
            "last_seen_at":  last_seen,
            "holdings":      holdings,
            "recent":        recent,
            "total_value":   round(total_value, 2),
            "total_cost":    round(total_cost, 2),
            "total_pl":      round(total_pl, 2),
            "rank":          rank,
            "total_traders": total_traders,
        })

    async def _handle_api_twitch_portfolio(self, request: web.Request
                                            ) -> web.Response:
        """Same shape as _handle_api_portfolio but reads the Twitch
        side: portfolios + transactions tables, filtered against the
        bot/excluded list. Username is the unique key; we lowercase
        for the lookup but preserve whatever case is stored for
        display."""
        username_raw = request.match_info["username"]
        if not username_raw or len(username_raw) > 64:
            return web.json_response({"error": "bad username"}, status=400)
        username = username_raw.lower()

        if username in EXCLUDED_USERS_LOWER:
            return web.json_response({"error": "not found"}, status=404)

        # Confirm user exists (has at least one portfolio row OR
        # transaction history). Otherwise 404 — mirrors the YouTube
        # portfolio's "no shares earned yet" path.
        async with self._db.execute(
                "SELECT 1 FROM portfolios WHERE LOWER(username) = ? "
                "AND shares > 0.001 LIMIT 1", (username,)) as cur:
            row = await cur.fetchone()
        if row is None:
            async with self._db.execute(
                    "SELECT 1 FROM transactions WHERE LOWER(username) = ? "
                    "LIMIT 1", (username,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return web.json_response({"error": "not found"}, status=404)

        # Holdings
        holdings: List[Dict[str, Any]] = []
        async with self._db.execute("""
            SELECT h.god_name, h.shares, h.avg_cost, p.price
              FROM portfolios h
              LEFT JOIN god_prices p ON p.god_name = h.god_name
             WHERE LOWER(h.username) = ? AND h.shares > 0.001
             ORDER BY h.god_name
        """, (username,)) as cur:
            async for r in cur:
                god, shares, avg_cost, price = r
                price = (float(price) if price is not None
                         else float(ECONOMY_STARTING_PRICE))
                value = shares * price
                cost_basis = shares * avg_cost
                pl = value - cost_basis
                pl_pct = (pl / cost_basis * 100.0) if cost_basis > 0 else 0.0
                holdings.append({
                    "god": god,
                    "shares": round(float(shares), 4),
                    "avg_cost": round(float(avg_cost), 2),
                    "price": round(price, 2),
                    "value": round(value, 2),
                    "pl": round(pl, 2),
                    "pl_pct": round(pl_pct, 2),
                })

        # Recent transactions
        recent: List[Dict[str, Any]] = []
        async with self._db.execute("""
            SELECT god_name, type, shares, price, total, fee, timestamp
              FROM transactions
             WHERE LOWER(username) = ?
             ORDER BY timestamp DESC LIMIT 20
        """, (username,)) as cur:
            async for r in cur:
                recent.append({
                    "god": r[0], "type": r[1],
                    "shares": round(float(r[2]), 4),
                    "price": round(float(r[3]), 2),
                    "total": round(float(r[4] or 0), 2),
                    "timestamp": r[6],
                })

        total_value = sum(h["value"] for h in holdings)
        total_cost = sum(h["shares"] * h["avg_cost"] for h in holdings)
        total_pl = total_value - total_cost

        # Rank in the cross-platform leaderboard.
        rank, total_traders = await self._portfolio_rank("twitch", username)

        return web.json_response({
            "platform":      "twitch",
            "channel_id":    username,           # for symmetry with YT
            "display_name":  username_raw,       # preserve case from URL
            "holdings":      holdings,
            "recent":        recent,
            "total_value":   round(total_value, 2),
            "total_cost":    round(total_cost, 2),
            "total_pl":      round(total_pl, 2),
            "rank":          rank,
            "total_traders": total_traders,
        })

    async def _handle_api_search(self, request: web.Request) -> web.Response:
        """Search portfolios by display name (partial, case-insensitive),
        across BOTH platforms. Each result includes a 'platform' field
        so the UI can badge them clearly."""
        q = request.query.get("q", "").strip()
        if len(q) < 2:
            return web.json_response({"results": []})
        if len(q) > 64:
            return web.json_response({"error": "query too long"}, status=400)

        like = f"%{q.lower()}%"
        results: List[Dict[str, Any]] = []

        # YouTube — joined to youtube_portfolios for display name + last seen.
        async with self._db.execute("""
            SELECT yt_channel_id, yt_display_name, last_seen_at
              FROM youtube_portfolios
             WHERE LOWER(yt_display_name) LIKE ?
             ORDER BY last_seen_at DESC LIMIT 20
        """, (like,)) as cur:
            async for r in cur:
                results.append({
                    "platform": "youtube",
                    "channel_id": r[0],
                    "display_name": r[1],
                    "last_seen_at": r[2],
                    "url": f"/yt/{r[0]}",
                })

        # Twitch — DISTINCT usernames from portfolios, filtered against
        # the excluded list so bot accounts never surface in search.
        excluded = list(EXCLUDED_USERS_LOWER)
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            twitch_sql = (f"SELECT DISTINCT username FROM portfolios "
                          f"WHERE LOWER(username) LIKE ? "
                          f"AND shares > 0.001 "
                          f"AND LOWER(username) NOT IN ({placeholders}) "
                          f"ORDER BY username LIMIT 20")
            twitch_params = (like,) + tuple(excluded)
        else:
            twitch_sql = ("SELECT DISTINCT username FROM portfolios "
                          "WHERE LOWER(username) LIKE ? "
                          "AND shares > 0.001 "
                          "ORDER BY username LIMIT 20")
            twitch_params = (like,)
        async with self._db.execute(twitch_sql, twitch_params) as cur:
            async for r in cur:
                username = r[0]
                results.append({
                    "platform": "twitch",
                    "channel_id": username,         # symmetry with YT shape
                    "display_name": username,
                    "last_seen_at": None,
                    "url": f"/twitch/{username}",
                })

        return web.json_response({"results": results})

    async def _handle_api_prices(self, request: web.Request) -> web.Response:
        """Current price snapshot for every god. Used on first paint."""
        prices: Dict[str, float] = {}
        async with self._db.execute(
                "SELECT god_name, price FROM god_prices") as cur:
            async for r in cur:
                prices[r[0]] = round(float(r[1]), 2)
        return web.json_response({"prices": prices})

    async def _handle_api_stream_status(self, request: web.Request) -> web.Response:
        """
        Latest known live status of the broadcaster's Twitch stream.
        Read from StreamStatusPlugin.get_status() if wired in,
        otherwise returns a sensible offline default. The website
        polls this every 30s to show/hide the embedded player.
        """
        if self.stream_status is None:
            return web.json_response({
                "is_live": False,
                "channel": None,
                "market_open": self._market_open(),
                "reason": "stream_status plugin not registered",
            })
        try:
            status = dict(self.stream_status.get_status())
            status["market_open"] = self._market_open()
            return web.json_response(status)
        except Exception as e:
            return web.json_response({
                "is_live": False,
                "market_open": False,
                "error": str(e),
            }, status=200)

    # ──────────────────────────────────────────────────────────────────
    #   God grid / god detail API
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _god_slug(name: str) -> str:
        """Tracker.gg uses kebab-case slugs for god images."""
        return name.lower().replace(" ", "-").replace("'", "")

    @classmethod
    def _god_icon_url(cls, name: str) -> str:
        """
        Local URL to the god's portrait. Served from data/god_icons/
        via /god-icon/{slug}. We have a complete kebab-case .png set
        locally; relying on tracker.gg's CDN would 404 on some gods.
        """
        return f"/god-icon/{cls._god_slug(name)}"

    async def _handle_god_icon(self, request: web.Request) -> web.Response:
        """Serve data/god_icons/{slug}.png. Cached aggressively in
        the browser since icons never change.

        Slug resolution tries (in order):
          1. Exact filename: data/god_icons/{slug}.png
          2. Casefold match against any .png in the directory
          3. With "the-" prefix (handles "Morrigan" -> "the-morrigan.png")
          4. Substring match of similar length (last resort)
        """
        slug = request.match_info["slug"]
        # Reject anything that could escape the icons dir.
        if "/" in slug or "\\" in slug or ".." in slug:
            return web.Response(text="bad slug", status=400)

        path = GOD_ICONS_DIR / f"{slug}.png"
        if not path.exists():
            lower = slug.lower()
            found = None
            # 2. exact case-insensitive match
            for f in GOD_ICONS_DIR.glob("*.png"):
                if f.stem.lower() == lower:
                    found = f
                    break
            # 3. "the-" prefix
            if found is None:
                alt = GOD_ICONS_DIR / f"the-{slug}.png"
                if alt.exists():
                    found = alt
            # 4. close-length substring match as last resort
            if found is None:
                for f in GOD_ICONS_DIR.glob("*.png"):
                    stem = f.stem.lower()
                    if (lower in stem or stem in lower) \
                            and abs(len(stem) - len(lower)) <= 5:
                        found = f
                        break
            if found is None:
                return web.Response(text="god icon not found", status=404)
            path = found

        return web.FileResponse(path, headers={
            "Cache-Control": "public, max-age=86400",
        })

    async def _handle_custom_god_icon(self, request: web.Request
                                       ) -> web.Response:
        """
        Serve a god portrait from Custom God Icons/ — the same folder
        the OBS overlays read from on stream. Files are title-cased
        with spaces ("Hou Yi.png", "Sylvanus.png").

        Skin variants (legacy "Achilles-Battleworn.png" style files
        from the Smite 1 era when tracker.gg told us the skin) are
        intentionally NOT matched — only the base-name file counts.
        Anything that doesn't have a clean Custom God Icons match
        falls back to /god-icon/<slug> so the default data/god_icons/
        portrait still shows.

        Matching strategy:
          1. Exact: <Title>.png
          2. Case-insensitive stem == title
          3. Fall back to /god-icon/<slug> via 302
        """
        slug = request.match_info["slug"]
        if "/" in slug or "\\" in slug or ".." in slug:
            return web.Response(text="bad slug", status=400)

        if not CUSTOM_GOD_ICONS_DIR.exists():
            raise web.HTTPFound(f"/god-icon/{slug}")

        # Slug "hou-yi" -> "Hou Yi" for filename matching.
        title = " ".join(part.capitalize() for part in slug.split("-"))
        title_lower = title.lower()

        # 1. Exact filename match
        path = CUSTOM_GOD_ICONS_DIR / f"{title}.png"
        if path.exists():
            return web.FileResponse(path, headers={
                "Cache-Control": "public, max-age=86400",
            })

        # 2. Case-insensitive exact stem match
        found = None
        for f in CUSTOM_GOD_ICONS_DIR.glob("*.png"):
            if f.stem.lower() == title_lower:
                found = f
                break

        # 3. Fall through to default god-icon route — skin variants
        #    are NOT matched, that was leftover Smite 1 behavior.
        if found is None:
            raise web.HTTPFound(f"/god-icon/{slug}")

        return web.FileResponse(found, headers={
            "Cache-Control": "public, max-age=86400",
        })

    async def _handle_api_gods(self, request: web.Request) -> web.Response:
        """List every god with stats + sparkline. Drives the grid page."""
        gods: List[Dict[str, Any]] = []

        # Pull all gods in price-DESC order.
        async with self._db.execute("""
            SELECT god_name, price, games_played, total_wins, total_losses,
                   total_kills, total_deaths, total_assists
              FROM god_prices
             ORDER BY price DESC, god_name ASC
        """) as cur:
            async for r in cur:
                name, price, games, wins, losses, k, d, a = r
                gods.append({
                    "name": name,
                    "slug": self._god_slug(name),
                    "icon_url": self._god_icon_url(name),
                    "price": round(float(price), 2),
                    "games": int(games or 0),
                    "wins": int(wins or 0),
                    "losses": int(losses or 0),
                    "winrate": round((wins / games), 3) if games else 0.0,
                    "kda_total": [int(k or 0), int(d or 0), int(a or 0)],
                })

        # Pull recent sparklines for each god in one query (last 20 per god).
        # We grab the last 100 history rows per god then keep newest 20 in
        # chronological order.
        sparklines: Dict[str, List[float]] = {g["name"]: [] for g in gods}
        async with self._db.execute("""
            SELECT god_name, price, timestamp
              FROM price_history
             ORDER BY god_name, timestamp DESC
        """) as cur:
            counts: Dict[str, int] = {}
            async for r in cur:
                gn = r[0]
                if gn not in sparklines:
                    continue
                if counts.get(gn, 0) >= 20:
                    continue
                sparklines[gn].append(float(r[1]))
                counts[gn] = counts.get(gn, 0) + 1
        # Reverse each god's list so it's chronological (oldest -> newest).
        for g in gods:
            spark = sparklines.get(g["name"], [])
            spark.reverse()
            g["sparkline"] = [round(p, 2) for p in spark]

        return web.json_response({"gods": gods})

    async def _handle_api_god(self, request: web.Request) -> web.Response:
        """Single god detail: lifetime stats, history, holders, formula."""
        name = request.match_info["name"]
        # Allow URL-encoded names (e.g. "Hou%20Yi" -> "Hou Yi").
        # aiohttp automatically decodes; nothing extra needed.

        # Resolve the canonical case-correct name even if URL casing was off.
        async with self._db.execute("""
            SELECT god_name, price, games_played, total_wins, total_losses,
                   total_kills, total_deaths, total_assists
              FROM god_prices
             WHERE LOWER(god_name) = LOWER(?)
        """, (name,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return web.json_response({"error": "god not found"}, status=404)

        canonical, price, games, wins, losses, k, d, a = row
        winrate = (wins / games) if games else 0.0

        # Lifetime stats card.
        lifetime = {
            "games": int(games or 0),
            "wins": int(wins or 0),
            "losses": int(losses or 0),
            "winrate": round(winrate, 3),
            "kills": int(k or 0),
            "deaths": int(d or 0),
            "assists": int(a or 0),
            "kda_avg": (round(((k or 0) + 0.5 * (a or 0)) / max(d or 1, 1), 2)
                        if games else 0.0),
        }

        # Price history for the chart.
        history: List[Dict[str, Any]] = []
        async with self._db.execute("""
            SELECT price, event, timestamp
              FROM price_history
             WHERE god_name = ?
             ORDER BY timestamp ASC
        """, (canonical,)) as cur:
            async for r in cur:
                history.append({
                    "price": round(float(r[0]), 2),
                    "event": r[1],
                    "timestamp": r[2],
                })

        # Recent matches list.
        recent_matches: List[Dict[str, Any]] = []
        async with self._db.execute("""
            SELECT match_id, outcome, kills, deaths, assists,
                   price_change, source, processed_at
              FROM processed_matches
             WHERE god_name = ?
             ORDER BY processed_at DESC LIMIT 20
        """, (canonical,)) as cur:
            async for r in cur:
                recent_matches.append({
                    "match_id": r[0],
                    "outcome": r[1],
                    "kills": int(r[2] or 0),
                    "deaths": int(r[3] or 0),
                    "assists": int(r[4] or 0),
                    "price_change": round(float(r[5] or 0.0), 2),
                    "source": r[6],
                    "timestamp": r[7],
                })

        # Top holders — three views (all / twitch / youtube).
        # Filters: opt-out flag respected, bot accounts excluded
        # (StreamElements, the bot itself, etc.). Each per-platform
        # list is its own top 10. The 'all' list is the combined top
        # 10 sorted by current value (so the most-valuable holdings
        # float regardless of which platform they came from).
        # Adding TikTok or any other platform later is just another
        # query + another key in the response.
        twitch_holders: List[Dict[str, Any]] = []
        youtube_holders: List[Dict[str, Any]] = []
        excluded_list = list(EXCLUDED_USERS_LOWER)
        if excluded_list:
            placeholders = ",".join("?" for _ in excluded_list)
            twitch_sql = (f"SELECT username, shares, avg_cost "
                          f"FROM portfolios "
                          f"WHERE god_name = ? AND shares > 0.001 "
                          f"AND COALESCE(leaderboard_opt_out, 0) = 0 "
                          f"AND LOWER(username) NOT IN ({placeholders}) "
                          f"ORDER BY shares DESC LIMIT 25")
            twitch_params = (canonical,) + tuple(excluded_list)
        else:
            twitch_sql = ("SELECT username, shares, avg_cost FROM portfolios "
                          "WHERE god_name = ? AND shares > 0.001 "
                          "AND COALESCE(leaderboard_opt_out, 0) = 0 "
                          "ORDER BY shares DESC LIMIT 25")
            twitch_params = (canonical,)
        async with self._db.execute(twitch_sql, twitch_params) as cur:
            async for r in cur:
                twitch_holders.append({
                    "platform": "twitch",
                    "name": r[0],
                    "shares": round(float(r[1]), 3),
                    "value": round(float(r[1]) * float(price), 2),
                    "avg_cost": round(float(r[2]), 2),
                })
        async with self._db.execute("""
            SELECT yp.yt_display_name, yh.shares, yh.avg_cost,
                   yp.yt_channel_id
              FROM youtube_holdings yh
              JOIN youtube_portfolios yp
                ON yp.yt_channel_id = yh.yt_channel_id
             WHERE yh.god_name = ? AND yh.shares > 0.001
                   AND COALESCE(yp.leaderboard_opt_out, 0) = 0
             ORDER BY yh.shares DESC LIMIT 25
        """, (canonical,)) as cur:
            async for r in cur:
                youtube_holders.append({
                    "platform": "youtube",
                    "name": r[0],
                    "shares": round(float(r[1]), 3),
                    "value": round(float(r[1]) * float(price), 2),
                    "avg_cost": round(float(r[2]), 2),
                    "channel_id": r[3],
                })
        all_holders = (twitch_holders + youtube_holders)
        all_holders.sort(key=lambda h: h["value"], reverse=True)
        top_holders = {
            "all": all_holders[:10],
            "twitch": twitch_holders[:10],
            "youtube": youtube_holders[:10],
        }

        # Formula breakdown — show the components contributing to price.
        try:
            from plugins.economy import (
                calculate_fair_value, FAIR_VALUE_BASE,
                FAIR_VALUE_CONFIDENCE_K, FAIR_VALUE_GAMES_LOG_BONUS,
                FAIR_VALUE_VOLUME_LOG_BONUS, FAIR_VALUE_KDA_TARGET,
                FAIR_VALUE_KDA_PER_UNIT, FAIR_VALUE_KDA_CAP,
                FAIR_VALUE_DOWNSIDE_CONFIDENCE_FLOOR,
            )
            import math as _m
            confidence = (games / (games + FAIR_VALUE_CONFIDENCE_K)
                          if games else 0.0)
            volume_premium = 0.0
            winrate_pct = 0.0
            if games > 0:
                if winrate >= 0.5:
                    games_factor = (1.0 + _m.log10(games + 1)
                                    * FAIR_VALUE_GAMES_LOG_BONUS)
                    winrate_pct = (winrate - 0.5) * games_factor
                    volume_premium = (_m.log10(games + 1)
                                       * FAIR_VALUE_VOLUME_LOG_BONUS)
                    eff_conf = confidence
                else:
                    winrate_pct = max((winrate - 0.5), -0.5)
                    eff_conf = max(confidence,
                                   FAIR_VALUE_DOWNSIDE_CONFIDENCE_FLOOR)
            deaths_basis = max((d or 0), 0.5 * games) if games else 1
            kda_ratio = (((k or 0) + 0.5 * (a or 0)) / deaths_basis
                         if deaths_basis > 0 else FAIR_VALUE_KDA_TARGET)
            kda_pct = (kda_ratio - FAIR_VALUE_KDA_TARGET) * FAIR_VALUE_KDA_PER_UNIT
            kda_pct = max(min(kda_pct, FAIR_VALUE_KDA_CAP), -FAIR_VALUE_KDA_CAP)

            breakdown = {
                "base": FAIR_VALUE_BASE,
                "winrate_pct": round(winrate_pct * 100, 1),
                "winrate_contribution": round(
                    FAIR_VALUE_BASE * winrate_pct
                    * (eff_conf if games else 0), 2),
                "volume_pct": round(volume_premium * 100, 1),
                "volume_contribution": round(
                    FAIR_VALUE_BASE * volume_premium, 2),
                "kda_pct": round(kda_pct * 100, 1),
                "kda_contribution": round(
                    FAIR_VALUE_BASE * kda_pct
                    * (confidence if games else 0), 2),
                "confidence": round(confidence, 2),
            }
        except Exception:
            breakdown = None

        return web.json_response({
            "name": canonical,
            "slug": self._god_slug(canonical),
            "icon_url": self._god_icon_url(canonical),
            "price": round(float(price), 2),
            "lifetime": lifetime,
            "history": history,
            "recent_matches": recent_matches,
            "top_holders": top_holders,
            "breakdown": breakdown,
        })

    async def _handle_god_page(self, request: web.Request) -> web.Response:
        path = PUBLIC_DIR / "god.html"
        if not path.exists():
            return web.Response(text="God page missing.", status=500)

        # Per-god social preview meta. Look up the current price for
        # a richer description; tolerate a missing/unknown god (the
        # client page already handles the "GOD NOT FOUND" empty state).
        god_name = request.match_info.get("name", "")
        price_blurb = ""
        try:
            if god_name:
                async with self._db.execute(
                        "SELECT price FROM god_prices WHERE god_name = ?",
                        (god_name,)) as cur:
                    row = await cur.fetchone()
                if row and row[0]:
                    price_blurb = f" · {round(float(row[0]))} hats"
        except Exception as e:
            print(f"[PublicWebServer] god meta lookup failed: {e}")

        title = f"{god_name or 'God'}{price_blurb} · Hatmas Market"
        desc = (f"Live price, sparkline, and top holders for "
                f"{god_name or 'this god'} on Hatmas Market — Smite 2's "
                "fan-run stock exchange.")
        full_url = f"https://hatmaster.tv{request.path}"
        return self._render_with_meta(path, {
            "OG_TITLE":       title,
            "OG_DESCRIPTION": desc,
            "OG_URL":         full_url,
        })

    async def _handle_community_page(self, request: web.Request) -> web.Response:
        path = PUBLIC_DIR / "community.html"
        if not path.exists():
            return web.Response(text="Community page missing.", status=500)
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def _handle_api_community(self, request: web.Request) -> web.Response:
        """
        Surface community-driven data:
          - god_queue: pending !godrequest entries (next 10)
          - god_pool: viewer-nominated gods awaiting !spin (sorted
            by vote_count DESC, then name)

        The suggestions panel was intentionally removed — viewers can
        still submit via !suggest in chat for the broadcaster's eyes,
        but they're not displayed publicly to avoid surfacing trolls.
        """
        # ── god queue (paid !godrequest) ───────────────────────────
        god_queue = []
        try:
            if GODREQ_QUEUE_FILE.exists():
                import json as _json
                raw = _json.loads(GODREQ_QUEUE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for idx, item in enumerate(raw[:10]):
                        if not isinstance(item, dict):
                            continue
                        god_queue.append({
                            "position": idx + 1,
                            "god": item.get("god"),
                            "requester": item.get("requester"),
                            "requested_at": item.get("requested_at"),
                            "token_spent": bool(item.get("token_spent")),
                            # source: "paid"|"manual"|"spin"|"paid_priority"
                            # — used by the community.html renderer to
                            # show the PRIORITY pill on $5 entries.
                            "source": item.get("source"),
                            "message": item.get("message"),
                        })
        except Exception as e:
            print(f"[PublicWebServer] god queue read failed: {e}")

        # ── god pool (viewer-nominated, waiting for spin) ──────────
        god_pool = []
        try:
            async with self._db.execute("""
                SELECT god_name, added_by, vote_count, added_at
                  FROM god_pool
                 ORDER BY vote_count DESC, god_name ASC
            """) as cur:
                async for r in cur:
                    god_pool.append({
                        "god": r[0],
                        "added_by": r[1],
                        "vote_count": int(r[2] or 0),
                        "added_at": r[3],
                    })
        except Exception as e:
            # Table may not exist yet on older DBs (god_pool plugin
            # creates it on first launch). Treat as empty pool.
            print(f"[PublicWebServer] god pool read failed: {e}")

        return web.json_response({
            "god_queue": god_queue,
            "queue_total": len(god_queue),
            "god_pool": god_pool,
            "pool_total": len(god_pool),
        })

    # ──────────────────────────────────────────────────────────────────
    #   LEADERBOARD (cross-platform, computed on demand)
    # ──────────────────────────────────────────────────────────────────
    #
    # Single ranked list of every portfolio with positive value, both
    # Twitch and YouTube sides merged. Value math mirrors the
    # per-portfolio handlers above (shares × current price, default to
    # ECONOMY_STARTING_PRICE when god_prices has no row yet). Excluded
    # bot accounts get filtered out. Ties broken by id ASC for
    # determinism so refreshes don't shuffle the bottom of the list.
    #
    # The leaderboard isn't pre-computed or cached — it's cheap enough
    # on the scales we're at (a few hundred portfolios at most) and
    # always-fresh feels better than introducing a "last updated 5
    # minutes ago" lag on a stock-market site.

    async def _compute_leaderboard(self) -> List[Dict[str, Any]]:
        """Full ranked portfolio list, sorted by total_value DESC.
        Each entry gets a 1-based `rank` field. Empty portfolios
        (total_value <= 0) and excluded bot accounts are filtered."""
        starting_price = float(ECONOMY_STARTING_PRICE)
        results: List[Dict[str, Any]] = []

        # Twitch side
        try:
            async with self._db.execute("""
                SELECT p.username,
                       COUNT(DISTINCT p.god_name) AS holdings_count,
                       SUM(p.shares * COALESCE(gp.price, ?)) AS total_value
                  FROM portfolios p
                  LEFT JOIN god_prices gp ON gp.god_name = p.god_name
                 WHERE p.shares > 0.001
                 GROUP BY p.username
            """, (starting_price,)) as cur:
                async for r in cur:
                    username, count, total = r
                    if not username:
                        continue
                    if username.lower() in EXCLUDED_USERS_LOWER:
                        continue
                    total_val = float(total or 0)
                    if total_val <= 0:
                        continue
                    results.append({
                        "platform":       "twitch",
                        "id":             username,
                        "display_name":   username,
                        "holdings_count": int(count or 0),
                        "total_value":    round(total_val, 2),
                        "url":            "/twitch/" + username,
                    })
        except Exception as e:
            print(f"[PublicWebServer] twitch leaderboard scan failed: {e}")

        # YouTube side
        try:
            async with self._db.execute("""
                SELECT yp.yt_channel_id, yp.yt_display_name,
                       COUNT(DISTINCT h.god_name) AS holdings_count,
                       SUM(h.shares * COALESCE(gp.price, ?)) AS total_value
                  FROM youtube_portfolios yp
                  JOIN youtube_holdings h
                    ON h.yt_channel_id = yp.yt_channel_id
                  LEFT JOIN god_prices gp ON gp.god_name = h.god_name
                 WHERE h.shares > 0.001
                 GROUP BY yp.yt_channel_id, yp.yt_display_name
            """, (starting_price,)) as cur:
                async for r in cur:
                    cid, name, count, total = r
                    if not cid:
                        continue
                    total_val = float(total or 0)
                    if total_val <= 0:
                        continue
                    results.append({
                        "platform":       "youtube",
                        "id":             cid,
                        "display_name":   name or cid,
                        "holdings_count": int(count or 0),
                        "total_value":    round(total_val, 2),
                        "url":            "/yt/" + cid,
                    })
        except Exception as e:
            print(f"[PublicWebServer] yt leaderboard scan failed: {e}")

        # Sort DESC by value; id ASC as deterministic tiebreaker.
        results.sort(key=lambda r: (-r["total_value"], (r["id"] or "").lower()))
        for i, r in enumerate(results):
            r["rank"] = i + 1
        return results

    async def _portfolio_rank(self, platform: str, identifier: str):
        """Return (rank, total_traders) for a portfolio. rank is None
        if the portfolio has no positive-value holdings (and therefore
        doesn't appear in the leaderboard)."""
        full = await self._compute_leaderboard()
        total = len(full)
        if not identifier:
            return None, total
        target = identifier.lower()
        for r in full:
            if r["platform"] == platform and (r["id"] or "").lower() == target:
                return r["rank"], total
        return None, total

    async def _handle_api_leaderboard(
            self, request: web.Request) -> web.Response:
        try:
            limit = max(1, min(100, int(request.query.get("limit", "25"))))
        except ValueError:
            limit = 25
        full = await self._compute_leaderboard()
        return web.json_response({
            "leaderboard":   full[:limit],
            "total_traders": len(full),
            "limit":         limit,
        })

    # ──────────────────────────────────────────────────────────────────
    #   RECENT ACTIVITY FEED
    # ──────────────────────────────────────────────────────────────────
    #
    # Aggregates "things that just happened" from multiple tables and
    # returns one normalized event stream. The landing page polls this
    # every ~20s and renders each event with an icon + pre-rendered
    # label. New event types are easy to add: just append another
    # query that maps to the common shape.
    #
    # Event shape (all fields optional except `ts` and `kind`):
    #   {
    #     "ts":     ISO timestamp (UTC),
    #     "kind":   "match" | "share_grant" | "dividend" |
    #               "new_portfolio" | "trade",
    #     "god":    god name (most events have one),
    #     "actor":  username / display name (events involving a viewer),
    #     "outcome":"win" | "loss" (match events),
    #     "kda":    [k, d, a] (match events),
    #     "delta":  price delta percent (match events),
    #     "shares": share quantity (trade / grant events),
    #     "price":  per-share price (trade events),
    #     "trade":  "buy" | "sell" (trade events),
    #     "total":  total hats paid out (dividend events),
    #     "holders":number of payouts (dividend events),
    #   }

    async def _handle_api_recent_events(
            self, request: web.Request) -> web.Response:
        events: list = []

        # Hatmaster's match settlements — these are the headliner
        # events: "Hatmaster won on Apollo · K/D/A · +X%".
        try:
            async with self._db.execute("""
                SELECT god_name, outcome, kills, deaths, assists,
                       price_change, processed_at
                  FROM processed_matches
                 ORDER BY processed_at DESC
                 LIMIT 30
            """) as cur:
                async for r in cur:
                    events.append({
                        "ts":      r[6],
                        "kind":    "match",
                        "god":     r[0],
                        "outcome": r[1],
                        "kda":     [int(r[2] or 0), int(r[3] or 0),
                                    int(r[4] or 0)],
                        "delta":   float(r[5] or 0),
                    })
        except Exception as e:
            print(f"[PublicWebServer] match feed failed: {e}")

        # YouTube share grants — new commenters earning their first
        # shares of a god. Join to portfolios for the display name.
        try:
            async with self._db.execute("""
                SELECT yp.yt_display_name, t.god_name, t.shares,
                       t.timestamp
                  FROM youtube_transactions t
                  JOIN youtube_portfolios  yp
                    ON yp.yt_channel_id = t.yt_channel_id
                 WHERE t.type = 'comment_share'
                 ORDER BY t.timestamp DESC
                 LIMIT 30
            """) as cur:
                async for r in cur:
                    events.append({
                        "ts":     r[3],
                        "kind":   "share_grant",
                        "actor":  r[0],
                        "god":    r[1],
                        "shares": float(r[2] or 0),
                    })
        except Exception as e:
            print(f"[PublicWebServer] share-grant feed failed: {e}")

        # Dividend payouts — god paid X total hats to Y holders.
        try:
            async with self._db.execute("""
                SELECT god_name, rate, total_hats, holders, timestamp
                  FROM dividends
                 ORDER BY timestamp DESC
                 LIMIT 20
            """) as cur:
                async for r in cur:
                    events.append({
                        "ts":      r[4],
                        "kind":    "dividend",
                        "god":     r[0],
                        "total":   float(r[2] or 0),
                        "holders": int(r[3] or 0),
                    })
        except Exception as e:
            print(f"[PublicWebServer] dividend feed failed: {e}")

        # New portfolios — viewer showed up for the first time.
        try:
            async with self._db.execute("""
                SELECT yt_display_name, first_seen_at
                  FROM youtube_portfolios
                 ORDER BY first_seen_at DESC
                 LIMIT 20
            """) as cur:
                async for r in cur:
                    events.append({
                        "ts":    r[1],
                        "kind":  "new_portfolio",
                        "actor": r[0],
                    })
        except Exception as e:
            print(f"[PublicWebServer] new-portfolio feed failed: {e}")

        # Twitch-side trades (buys / sells). Filter the known bot
        # accounts out so the feed isn't full of system actors.
        excluded = {u.lower() for u in (ECONOMY_EXCLUDED_USERNAMES or [])}
        if TWITCH_BOT_USERNAME:
            excluded.add(TWITCH_BOT_USERNAME.lower())
        try:
            async with self._db.execute("""
                SELECT username, god_name, type, shares, price, timestamp
                  FROM transactions
                 WHERE type IN ('buy', 'sell')
                 ORDER BY timestamp DESC
                 LIMIT 30
            """) as cur:
                async for r in cur:
                    if (r[0] or "").lower() in excluded:
                        continue
                    events.append({
                        "ts":     r[5],
                        "kind":   "trade",
                        "actor":  r[0],
                        "god":    r[1],
                        "trade":  r[2],
                        "shares": float(r[3] or 0),
                        "price":  float(r[4] or 0),
                    })
        except Exception as e:
            print(f"[PublicWebServer] trade feed failed: {e}")

        # Merge + trim. Sort DESC by ts; SQLite stores timestamps
        # as ISO strings so lexical sort == chronological.
        events.sort(key=lambda e: e.get("ts") or "", reverse=True)
        return web.json_response({
            "events": events[:25],
            "total":  len(events[:25]),
        })

    # ──────────────────────────────────────────────────────────────────
    #   PENDING YT NOMINATIONS (admin-only, loopback gate)
    # ──────────────────────────────────────────────────────────────────
    #
    # The YouTube comment scanner (plugins/youtube_rewards.py) writes a
    # row to pending_yt_nominations whenever a comment mentions a known
    # god. These three endpoints are how Hatmaster reviews the queue
    # from /community. Access is gated to direct loopback connections
    # — opening http://localhost:8070/community on the host machine.
    # Tunneled requests (via Cloudflare → cloudflared → localhost) also
    # appear from 127.0.0.1 but carry CF-Connecting-IP / X-Forwarded-For
    # headers; we reject those.
    #
    # Approval semantics: insert into god_pool (or increment vote_count
    # if the god is already there), and record a god_pool_votes row
    # attributed to the YT user. The voter_username is prefixed with
    # "yt:" so it can't collide with Twitch usernames in the daily-cap
    # PK. Rejecting just flips status to 'rejected' for audit.

    def _is_local_admin(self, request: web.Request) -> bool:
        """True only for direct loopback browser requests.

        cloudflared opens the upstream connection from the same host
        as the webserver, so every tunneled request also looks like
        it's coming from 127.0.0.1 at the socket level. The proxy
        adds CF-Connecting-IP (Cloudflare) and standard XFF — the
        presence of either is the signal that this is a tunneled
        request and must be rejected.
        """
        if request.remote not in ("127.0.0.1", "::1"):
            return False
        if request.headers.get("CF-Connecting-IP"):
            return False
        if request.headers.get("X-Forwarded-For"):
            return False
        return True

    async def _handle_api_pending_nominations(
            self, request: web.Request) -> web.Response:
        if not self._is_local_admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            async with self._db.execute("""
                SELECT id, yt_video_id, yt_comment_id, yt_channel_id,
                       yt_display_name, god_name, comment_snippet,
                       status, created_at
                  FROM pending_yt_nominations
                 WHERE status = 'pending'
                 ORDER BY created_at DESC
                 LIMIT 100
            """) as cur:
                rows = await cur.fetchall()
        except Exception as e:
            # Table may not exist yet if youtube_rewards hasn't booted
            # (or is disabled). Treat as empty.
            print(f"[PublicWebServer] pending-nominations read failed: {e}")
            rows = []

        items = [{
            "id":            r[0],
            "video_id":      r[1],
            "comment_id":    r[2],
            "channel_id":    r[3],
            "display_name":  r[4],
            "god":           r[5],
            "snippet":       r[6],
            "status":        r[7],
            "created_at":    r[8],
        } for r in rows]
        return web.json_response({
            "pending": items,
            "total":   len(items),
        })

    async def _handle_api_nomination_approve(
            self, request: web.Request) -> web.Response:
        if not self._is_local_admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            nid = int(request.match_info["nid"])
        except (TypeError, ValueError):
            return web.json_response({"error": "bad id"}, status=400)

        # Read the row (must be pending). Doing this under the same
        # connection means we serialize against concurrent approves —
        # if the user clicks Approve twice, the second one finds
        # status='approved' and bails.
        async with self._db.execute("""
            SELECT yt_display_name, god_name, status
              FROM pending_yt_nominations
             WHERE id = ?
        """, (nid,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return web.json_response({"error": "not found"}, status=404)

        display_name, god_name, status = row[0], row[1], row[2]
        if status != "pending":
            return web.json_response(
                {"error": f"already {status}"}, status=409)

        # Route through GodPool's data model. We replicate the SQL
        # directly (rather than reaching into the plugin instance)
        # because cmd_nominate is wired to a chat-message context we
        # don't have here. The semantics match:
        #   - god_pool: insert or +1 vote_count
        #   - god_pool_votes: record the daily vote, namespaced with
        #     "yt:" so a YT viewer and a Twitch viewer of the same
        #     name don't collide on the (voter_username, vote_date) PK
        from datetime import date as _date
        today_iso = _date.today().isoformat()
        voter_key = f"yt:{display_name.lower()}"

        try:
            await self._db.execute("""
                INSERT INTO god_pool (god_name, added_by, vote_count)
                VALUES (?, ?, 1)
                ON CONFLICT(god_name) DO UPDATE SET
                    vote_count = vote_count + 1
            """, (god_name, display_name))
            await self._db.execute("""
                INSERT INTO god_pool_votes
                    (voter_username, vote_date, god_name)
                VALUES (?, ?, ?)
                ON CONFLICT(voter_username, vote_date) DO NOTHING
            """, (voter_key, today_iso, god_name))
            await self._db.execute("""
                UPDATE pending_yt_nominations
                   SET status = 'approved',
                       decided_at = datetime('now')
                 WHERE id = ?
            """, (nid,))
            await self._db.commit()
        except Exception as e:
            print(f"[PublicWebServer] nomination approve failed: {e}")
            return web.json_response({"error": "approve failed"}, status=500)

        return web.json_response({
            "ok": True,
            "id": nid,
            "god": god_name,
            "display_name": display_name,
        })

    async def _handle_api_nomination_reject(
            self, request: web.Request) -> web.Response:
        if not self._is_local_admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            nid = int(request.match_info["nid"])
        except (TypeError, ValueError):
            return web.json_response({"error": "bad id"}, status=400)

        # Rejection is a one-row status flip. No god_pool side effects.
        cur = await self._db.execute("""
            UPDATE pending_yt_nominations
               SET status = 'rejected',
                   decided_at = datetime('now')
             WHERE id = ? AND status = 'pending'
        """, (nid,))
        await self._db.commit()
        if cur.rowcount == 0:
            return web.json_response(
                {"error": "not found or already decided"}, status=404)

        return web.json_response({"ok": True, "id": nid})

    # ──────────────────────────────────────────────────────────────────
    #   WEBSITE LOGIN + TRADING (WEBSITE_TRADING_DESIGN.md)
    # ──────────────────────────────────────────────────────────────────
    # Twitch OAuth proves identity (zero scopes, token used once for
    # /helix/users then discarded). Sessions are stateless HMAC-signed
    # cookies (core/web_session.py). /api/trade delegates to the
    # economy plugin's execute_buy/execute_sell — the same money path
    # chat commands use.

    _NO_STORE = {"Cache-Control": "private, no-store"}

    # ──────────────────────────────────────────────────────────────
    #   MOD PAGE (hidden command matrix, Crossplatform_Commands_Plan.md)
    # ──────────────────────────────────────────────────────────────

    async def _effective_mods(self) -> Set[str]:
        """Lowercase logins allowed on /mod: MODERATORS config plus
        broadcaster plus Helix Get Moderators (cached 5 min). Helix
        failures (commonly a missing moderation:read scope) degrade to
        the config list with a console warning, never an exception."""
        mods = {m.lower() for m in getattr(_config, "MODERATORS", []) if m}
        channel = getattr(_config, "TWITCH_CHANNEL", "") or ""
        if channel and channel != "YOUR_CHANNEL":
            mods.add(channel.lower())

        now = time.time()
        fetched_at, helix_mods = self._helix_mod_cache
        if now - fetched_at >= 300:
            helix_mods = set()
            tm = getattr(self.bot, "token_manager", None) if self.bot else None
            owner_id = getattr(_config, "TWITCH_OWNER_ID", "") or ""
            if tm and owner_id:
                try:
                    headers = await tm.get_broadcaster_headers()
                    url = ("https://api.twitch.tv/helix/moderation/moderators"
                           f"?broadcaster_id={owner_id}&first=100")
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                helix_mods = {
                                    (d.get("user_login") or "").lower()
                                    for d in data.get("data", [])
                                } - {""}
                            else:
                                print(f"[ModPage] Helix moderators lookup "
                                      f"HTTP {resp.status}, falling back to "
                                      f"MODERATORS config (check the "
                                      f"moderation:read scope)")
                except Exception as e:
                    print(f"[ModPage] Helix moderators lookup failed: {e}")
            self._helix_mod_cache = (now, helix_mods)
        return mods | helix_mods

    async def _mod_identity(self, request: web.Request) -> Optional[dict]:
        """Session identity IF the logged-in user is a mod, else None."""
        ident = self._session_identity(request)
        if ident is None:
            return None
        login = (ident.get("login") or "").lower()
        if not login:
            return None
        return ident if login in await self._effective_mods() else None

    def _mod_audit(self, login: str, name: str, platform: str,
                   old: bool, new: bool):
        line = (f"{datetime.now().isoformat(timespec='seconds')} | {login} | "
                f"{name} | {platform} | {old} -> {new}\n")
        print(f"[ModPage] {line.strip()}")
        try:
            with open(DATA_DIR / "mod_audit.log", "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            print(f"[ModPage] audit write failed: {e}")

    async def _handle_mod_page(self, request: web.Request):
        if await self._mod_identity(request) is None:
            raise web.HTTPNotFound()  # styled by the 404 middleware
        path = PUBLIC_DIR / "mod.html"
        if not path.exists():
            return web.Response(text="Mod page missing.", status=500)
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def _handle_api_mod_commands_get(self, request: web.Request):
        ident = await self._mod_identity(request)
        if ident is None:
            raise web.HTTPNotFound()
        if self.bot is None:
            return web.json_response({"ok": False, "error": "Bot unavailable."},
                                     status=503, headers=self._NO_STORE)
        catalog = self.bot.get_command_catalog()
        customs = self.bot.plugins.get("custom_commands") if self.bot.plugins else None
        if customs is not None:
            for entry in catalog:
                if entry["custom"]:
                    entry["response"] = customs.get_response(entry["name"])
        discord_plugin = (self.bot.plugins or {}).get("discord")
        status = {
            "uptime": self.bot.get_uptime() if hasattr(self.bot, "get_uptime") else "?",
            "discord_connected": bool(getattr(discord_plugin, "is_ready", False)),
            "command_count": getattr(self.bot, "command_count", 0),
        }
        return web.json_response(
            {"ok": True, "user": ident.get("login"),
             "status": status, "commands": catalog},
            headers=self._NO_STORE)

    async def _handle_api_mod_commands_post(self, request: web.Request):
        """POST /api/mod/commands. Two body shapes:
        {name, platform, enabled} flips availability;
        {name, cooldown} sets per-user cooldown seconds (0 = off).
        Guard order mirrors /api/trade: session+mod -> origin -> rate
        limit -> body -> write -> audit."""
        ident = await self._mod_identity(request)
        if ident is None:
            raise web.HTTPNotFound()
        if not self._origin_ok(request):
            return web.json_response({"ok": False, "error": "Bad origin."},
                                     status=403, headers=self._NO_STORE)
        if not self._ip_rate_ok(request):
            return web.json_response({"ok": False, "error": "Too many requests."},
                                     status=429, headers=self._NO_STORE)
        if self.bot is None:
            return web.json_response({"ok": False, "error": "Bot unavailable."},
                                     status=503, headers=self._NO_STORE)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "Bad JSON."},
                                     status=400, headers=self._NO_STORE)
        name = str(body.get("name") or "").lower().strip()
        login = ident.get("login") or "?"

        if "cooldown" in body:
            cooldown = body.get("cooldown")
            if (not isinstance(cooldown, (int, float)) or isinstance(cooldown, bool)
                    or not (0 <= cooldown <= 3600)):
                return web.json_response(
                    {"ok": False, "error": "Cooldown must be 0-3600 seconds."},
                    status=400, headers=self._NO_STORE)
            old_cd = self.bot.get_command_cooldown(name)
            entry = await self.bot.set_command_cooldown(name, cooldown)
            if entry is None:
                return web.json_response({"ok": False, "error": "Unknown command."},
                                         status=400, headers=self._NO_STORE)
            self._mod_audit(login, name, "cooldown", old_cd, float(cooldown))
            return web.json_response({"ok": True, "command": entry},
                                     headers=self._NO_STORE)

        platform = str(body.get("platform") or "").lower().strip()
        enabled = body.get("enabled")
        if platform not in ("twitch", "discord") or not isinstance(enabled, bool):
            return web.json_response({"ok": False, "error": "Bad request body."},
                                     status=400, headers=self._NO_STORE)
        old_state = self.bot.is_command_enabled(name, platform)
        entry = await self.bot.set_command_platform(name, platform, enabled)
        if entry is None:
            return web.json_response({"ok": False, "error": "Unknown command."},
                                     status=400, headers=self._NO_STORE)
        self._mod_audit(login, name, platform, old_state, enabled)
        return web.json_response({"ok": True, "command": entry},
                                 headers=self._NO_STORE)

    def _custom_plugin(self):
        return (self.bot.plugins or {}).get("custom_commands") if self.bot else None

    async def _mod_guards(self, request):
        """Shared guards for custom-command writes. Returns (ident, err)."""
        ident = await self._mod_identity(request)
        if ident is None:
            raise web.HTTPNotFound()
        if not self._origin_ok(request):
            return None, web.json_response(
                {"ok": False, "error": "Bad origin."},
                status=403, headers=self._NO_STORE)
        if not self._ip_rate_ok(request):
            return None, web.json_response(
                {"ok": False, "error": "Too many requests."},
                status=429, headers=self._NO_STORE)
        if self._custom_plugin() is None:
            return None, web.json_response(
                {"ok": False, "error": "Custom commands unavailable."},
                status=503, headers=self._NO_STORE)
        return ident, None

    async def _handle_api_mod_custom_post(self, request: web.Request):
        """POST /api/mod/custom-commands, body {name, response}. Upserts
        a mod-created text command."""
        ident, err = await self._mod_guards(request)
        if err is not None:
            return err
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "Bad JSON."},
                                     status=400, headers=self._NO_STORE)
        name = str(body.get("name") or "").lower().strip()
        response = str(body.get("response") or "")
        login = ident.get("login") or "?"
        ok, result = await self._custom_plugin().add_or_update(
            name, response, created_by=login)
        if not ok:
            return web.json_response({"ok": False, "error": result},
                                     status=400, headers=self._NO_STORE)
        self._mod_audit(login, name, f"custom:{result}", "", response[:80])
        for entry in self.bot.get_command_catalog():
            if entry["name"] == name:
                entry["response"] = response
                return web.json_response(
                    {"ok": True, "action": result, "command": entry},
                    headers=self._NO_STORE)
        return web.json_response({"ok": True, "action": result},
                                 headers=self._NO_STORE)

    async def _handle_api_mod_custom_delete(self, request: web.Request):
        """DELETE /api/mod/custom-commands/{name}."""
        ident, err = await self._mod_guards(request)
        if err is not None:
            return err
        name = str(request.match_info.get("name") or "").lower().strip()
        login = ident.get("login") or "?"
        ok, result = await self._custom_plugin().delete(name)
        if not ok:
            return web.json_response({"ok": False, "error": result},
                                     status=400, headers=self._NO_STORE)
        self._mod_audit(login, name, "custom:deleted", "", "")
        return web.json_response({"ok": True, "action": "deleted"},
                                 headers=self._NO_STORE)

    def _session_identity(self, request: web.Request) -> Optional[dict]:
        """Verified session payload, or None (= logged out)."""
        if not self._login_enabled:
            return None
        return _ws.verify(request.cookies.get(_ws.SESSION_COOKIE),
                          WEB_SESSION_SECRET)

    @staticmethod
    def _client_ip(request: web.Request) -> str:
        """Real client IP. Behind the Cloudflare tunnel every socket
        is localhost, so trust CF-Connecting-IP first."""
        return (request.headers.get("CF-Connecting-IP")
                or request.headers.get(
                    "X-Forwarded-For", "").split(",")[0].strip()
                or request.remote or "unknown")

    def _ip_rate_ok(self, request: web.Request) -> bool:
        """Fixed-window per-IP limiter for /api/trade + /auth/*.
        Defense in depth behind the Cloudflare WAF edge rule."""
        window = int(time.time() // 60)
        if len(self._ip_window) > 4096:
            self._ip_window = {k: v for k, v in self._ip_window.items()
                               if k[1] == window}
        key = (self._client_ip(request), window)
        self._ip_window[key] = self._ip_window.get(key, 0) + 1
        return self._ip_window[key] <= WEB_TRADE_MAX_PER_MIN

    def _origin_ok(self, request: web.Request) -> bool:
        """CSRF backstop: state-changing requests must carry an Origin
        header matching the site. SameSite=Strict on the session
        cookie already blocks cross-site sends in modern browsers."""
        return request.headers.get("Origin", "") == self._allowed_origin

    def _set_session_cookie(self, response, token: str):
        response.set_cookie(
            _ws.SESSION_COOKIE, token, max_age=_ws.DEFAULT_MAX_AGE,
            httponly=True, secure=self._cookie_secure,
            samesite="Strict", path="/")

    def _trading_allowed(self) -> bool:
        """Config master switch AND dashboard feature toggle."""
        if not (WEB_TRADING_ENABLED and self._login_enabled
                and self.economy is not None):
            return False
        bot = getattr(self.economy, "bot", None)
        if bot is not None and hasattr(bot, "is_feature_enabled"):
            return bool(bot.is_feature_enabled("web_trading"))
        return True

    def _market_open(self) -> bool:
        """Trades need the economy DB AND MixItUp (hats live there).
        Surfaced via /api/stream-status as market_open."""
        eco = self.economy
        return bool(self._trading_allowed()
                    and getattr(eco, "_db", None) is not None
                    and getattr(eco, "_connected", False))

    async def _handle_auth_login(self, request: web.Request):
        """GET /auth/login — redirect to Twitch authorize with a
        state nonce bound to a short-lived cookie."""
        if not self._login_enabled:
            return web.Response(
                status=503, text="Login is not configured on this site.")
        if not self._ip_rate_ok(request):
            return web.Response(status=429, text="Too many requests.")
        state = _ws.make_state()
        params = urlencode({
            "response_type": "code",
            "client_id": TWITCH_CLIENT_ID,
            "redirect_uri": WEB_OAUTH_REDIRECT_URI,
            "scope": "",  # identity only — deliberately zero scopes
            "state": state,
        })
        resp = web.HTTPFound(
            f"https://id.twitch.tv/oauth2/authorize?{params}")
        resp.set_cookie(
            _ws.OAUTH_STATE_COOKIE, state, max_age=600, httponly=True,
            secure=self._cookie_secure, samesite="Lax", path="/")
        return resp

    async def _handle_auth_callback(self, request: web.Request):
        """GET /auth/twitch/callback — verify state, exchange the code
        server-side, fetch identity, drop the token, issue session."""
        if not self._login_enabled:
            return web.Response(
                status=503, text="Login is not configured on this site.")
        if not self._ip_rate_ok(request):
            return web.Response(status=429, text="Too many requests.")

        state = request.query.get("state", "")
        cookie_state = request.cookies.get(_ws.OAUTH_STATE_COOKIE, "")
        code = request.query.get("code", "")
        if not state or not cookie_state or state != cookie_state \
                or not code:
            # Stale bookmark or replayed login URL. No retry logic on
            # purpose — the user just clicks Log in again.
            return web.Response(
                status=403,
                text="Login state mismatch. Return to hatmaster.tv "
                     "and try logging in again.")

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(
                    "https://id.twitch.tv/oauth2/token",
                    data={
                        "client_id": TWITCH_CLIENT_ID,
                        "client_secret": TWITCH_CLIENT_SECRET,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": WEB_OAUTH_REDIRECT_URI,
                    },
                ) as r:
                    tok = await r.json()
                access_token = tok.get("access_token")
                if not access_token:
                    print(f"[PublicWebServer] OAuth exchange failed: "
                          f"{tok.get('message', 'no access_token')}")
                    return web.HTTPFound("/?login=failed")
                async with http.get(
                    "https://api.twitch.tv/helix/users",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Client-Id": TWITCH_CLIENT_ID,
                    },
                ) as r:
                    users = await r.json()
        except Exception as e:
            print(f"[PublicWebServer] OAuth callback error: {e}")
            return web.HTTPFound("/?login=failed")
        # access_token goes out of scope here — never persisted.

        data = (users or {}).get("data") or []
        if not data:
            return web.HTTPFound("/?login=failed")
        u = data[0]
        token = _ws.issue(
            u.get("id", ""), u.get("login", ""),
            u.get("display_name", ""), u.get("profile_image_url", ""),
            secret=WEB_SESSION_SECRET)
        resp = web.HTTPFound(f"/twitch/{u.get('login', '')}")
        self._set_session_cookie(resp, token)
        resp.del_cookie(_ws.OAUTH_STATE_COOKIE, path="/")
        print(f"[PublicWebServer] website login: {u.get('login')}")
        return resp

    async def _handle_auth_logout(self, request: web.Request):
        """POST /auth/logout — POST (not GET) so a hostile <img> tag
        cannot log viewers out."""
        resp = web.json_response({"ok": True}, headers=self._NO_STORE)
        resp.del_cookie(_ws.SESSION_COOKIE, path="/")
        return resp

    async def _handle_api_me(self, request: web.Request):
        """GET /api/me — session identity for JS hydration. 401 when
        logged out (body still says whether login is even possible)."""
        ident = self._session_identity(request)
        if ident is None:
            return web.json_response(
                {"logged_in": False,
                 "login_available": self._login_enabled,
                 "trading_enabled": self._trading_allowed(),
                 "market_open": self._market_open()},
                status=401, headers=self._NO_STORE)
        return web.json_response({
            "logged_in": True,
            "uid": ident.get("uid"),
            "login": ident.get("login"),
            "name": ident.get("name"),
            "img": ident.get("img"),
            "trading_enabled": self._trading_allowed(),
            "market_open": self._market_open(),
        }, headers=self._NO_STORE)

    async def _handle_api_me_balance(self, request: web.Request):
        """GET /api/me/balance — current hat balance from MixItUp."""
        ident = self._session_identity(request)
        if ident is None:
            return web.json_response(
                {"error": "not_logged_in"}, status=401,
                headers=self._NO_STORE)
        balance = None
        eco = self.economy
        if eco is not None and getattr(eco, "_connected", False):
            try:
                balance = await eco._get_balance(ident.get("login", ""))
            except Exception as e:
                print(f"[PublicWebServer] balance read error: {e}")
        return web.json_response({
            "login": ident.get("login"),
            "balance": balance,
            "market_open": self._market_open(),
        }, headers=self._NO_STORE)

    async def _handle_api_me_settings(self, request: web.Request):
        """GET /api/me/settings — logged-in viewer's account flags."""
        ident = self._session_identity(request)
        if ident is None:
            return web.json_response(
                {"error": "not_logged_in"}, status=401,
                headers=self._NO_STORE)
        hidden = False
        eco = self.economy
        if eco is not None and getattr(eco, "_db", None) is not None:
            try:
                hidden = await eco.get_leaderboard_hidden(
                    ident.get("login", ""))
            except Exception as e:
                print(f"[PublicWebServer] settings read error: {e}")
        return web.json_response(
            {"login": ident.get("login"),
             "leaderboard_hidden": hidden},
            headers=self._NO_STORE)

    async def _handle_api_me_visibility(self, request: web.Request):
        """POST /api/me/visibility — body {"hidden": true|false}.

        The self-serve replacement for the never-implemented !hideme
        chat command. Same guard structure as /api/trade minus the
        trading switches (privacy control should work even when the
        market is closed): session -> origin -> rate limit -> write.
        """
        ident = self._session_identity(request)
        if ident is None:
            return web.json_response(
                {"ok": False, "error": "Log in with Twitch first."},
                status=401, headers=self._NO_STORE)
        if not self._origin_ok(request):
            return web.json_response(
                {"ok": False, "error": "Bad origin."},
                status=403, headers=self._NO_STORE)
        if not self._ip_rate_ok(request):
            return web.json_response(
                {"ok": False, "error": "Too many requests."},
                status=429, headers=self._NO_STORE)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response(
                {"ok": False, "error": "Bad JSON."},
                status=400, headers=self._NO_STORE)
        hidden = body.get("hidden")
        if not isinstance(hidden, bool):
            return web.json_response(
                {"ok": False, "error": "hidden must be true or false."},
                status=400, headers=self._NO_STORE)
        eco = self.economy
        if eco is None or getattr(eco, "_db", None) is None:
            return web.json_response(
                {"ok": False, "error": "Economy offline - try later."},
                status=503, headers=self._NO_STORE)
        try:
            await eco.set_leaderboard_hidden(ident.get("login", ""),
                                             hidden)
        except Exception as e:
            print(f"[PublicWebServer] visibility write error: {e}")
            return web.json_response(
                {"ok": False, "error": "Write failed."},
                status=500, headers=self._NO_STORE)
        print(f"[PublicWebServer] leaderboard visibility: "
              f"{ident.get('login')} -> "
              f"{'hidden' if hidden else 'visible'}")
        return web.json_response(
            {"ok": True, "leaderboard_hidden": hidden},
            headers=self._NO_STORE)

    async def _handle_api_trade(self, request: web.Request):
        """POST /api/trade — body {action, god, amount|"all"}.

        Guards run in the order documented in WEBSITE_TRADING_DESIGN.md
        §4.2. The trade itself is the SAME execute_buy / execute_sell
        path chat commands use — this handler never touches balances,
        shares, or prices directly.
        """
        def err(status, message, **extra_headers):
            return web.json_response(
                {"ok": False, "error": message}, status=status,
                headers={**self._NO_STORE, **extra_headers})

        # 1. master switches (config + dashboard toggle)
        if not self._trading_allowed():
            return err(503, "Trading is disabled.")
        # 2. session
        ident = self._session_identity(request)
        if ident is None:
            return err(401, "Log in with Twitch first.")
        login = (ident.get("login") or "").lower()
        # 3. origin allowlist (CSRF backstop)
        if not self._origin_ok(request):
            return err(403, "Bad origin.")
        # 4. excluded bot accounts
        if not login or login in EXCLUDED_USERS_LOWER:
            return err(403, "This account cannot trade.")
        # 5. per-user cooldown (mirrors chat)
        now = time.time()
        elapsed = now - self._web_trade_cooldowns.get(login, 0)
        if elapsed < WEB_TRADE_COOLDOWN:
            retry = max(1, int(WEB_TRADE_COOLDOWN - elapsed) + 1)
            return err(429, f"Trade cooldown: {retry}s",
                       **{"Retry-After": str(retry)})
        # 6. per-IP bucket
        if not self._ip_rate_ok(request):
            return err(429, "Too many requests.")
        # 7. body shape + god resolution
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return err(400, "Bad JSON.")
        action = (str(body.get("action") or "")).strip().lower()
        god_input = (str(body.get("god") or "")).strip()
        amount_raw = body.get("amount")
        if action not in ("buy", "sell") or not god_input:
            return err(400, "action must be buy or sell, "
                            "and god is required.")
        eco = self.economy
        god_name = eco._resolve_god_name(god_input)
        if not god_name:
            return err(400, f"Unknown god: {god_input}")
        # 9 (checked before amount math so "all" can read balances).
        # economy DB + MixItUp must both be up — hats live in MixItUp.
        if getattr(eco, "_db", None) is None \
                or not getattr(eco, "_connected", False):
            return err(503, "Market closed — the bot or MixItUp "
                            "is offline.")

        # 8. per-user lock serializes balance-check → deduct
        async with self._trade_locks[login]:
            try:
                if isinstance(amount_raw, str) \
                        and amount_raw.strip().lower() == "all":
                    if action == "buy":
                        balance = await eco._get_balance(login)
                        if not balance or balance <= 0:
                            return err(400, "You have no hats.")
                        hat_amount = int(balance)
                    else:
                        holding = await eco._get_holding(login, god_name)
                        if not holding or holding["shares"] <= 0:
                            return err(400, f"You do not own any "
                                            f"{god_name} shares.")
                        hat_amount = int(
                            holding["shares"]
                            * eco._prices.get(god_name, 0))
                else:
                    hat_amount = int(str(amount_raw).replace(",", ""))
            except (TypeError, ValueError):
                return err(400, "Amount must be a whole number of "
                                "hats or 'all'.")
            if hat_amount < 1:
                return err(400, "Minimum trade is 1 hat.")

            if action == "buy":
                result = await eco.execute_buy(
                    login, god_name, hat_amount, channel="web")
            else:
                result = await eco.execute_sell(
                    login, god_name, hat_amount, channel="web")

        if not result.get("success"):
            return err(400, result.get("error", "Trade failed."))

        self._web_trade_cooldowns[login] = time.time()
        balance = None
        try:
            balance = await eco._get_balance(login)
        except Exception:
            pass
        print(f"[PublicWebServer] WEB TRADE: {login} {action} "
              f"{result.get('god_name', god_name)} for {hat_amount} hats")
        return web.json_response({
            "ok": True,
            "action": action,
            "god": result.get("god_name", god_name),
            "shares": round(float(result.get("shares", 0)), 4),
            "price": result.get("price"),
            "total": result.get("total_cost",
                                result.get("net_received")),
            "balance": balance,
        }, headers=self._NO_STORE)

    # ──────────────────────────────────────────────────────────────────
    #   SOCIAL TABS (Social_Tabs_Plan.md)
    # ──────────────────────────────────────────────────────────────────
    # Three cached read-only feeds for the landing-page tab strip.
    # Cache serves stale data on upstream failure so a dead API call
    # never blanks a tab.

    def _social_cache_get(self, key):
        hit = self._social_cache.get(key)
        if hit and time.time() - hit[0] < SOCIAL_FEED_CACHE_TTL:
            return hit[1]
        return None

    def _social_cache_stale(self, key):
        hit = self._social_cache.get(key)
        return hit[1] if hit else None

    async def _handle_social_youtube(self, request: web.Request):
        """Latest uploads via playlistItems (1 quota unit/call — NOT
        the 100-unit search.list). Uploads playlist id = channel id
        with the UC prefix swapped to UU."""
        cached = self._social_cache_get("youtube")
        if cached is not None:
            return web.json_response(cached)
        if not YOUTUBE_API_KEY or not YOUTUBE_CHANNEL_ID:
            return web.json_response(
                {"videos": [], "error": "not_configured"})
        uploads = "UU" + YOUTUBE_CHANNEL_ID[2:] \
            if YOUTUBE_CHANNEL_ID.startswith("UC") else YOUTUBE_CHANNEL_ID
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(
                    "https://www.googleapis.com/youtube/v3/playlistItems",
                    params={"part": "snippet", "playlistId": uploads,
                            "maxResults": "12", "key": YOUTUBE_API_KEY},
                ) as r:
                    data = await r.json()
            videos = []
            for item in data.get("items", []):
                sn = item.get("snippet") or {}
                rid = (sn.get("resourceId") or {}).get("videoId")
                if not rid:
                    continue
                thumbs = sn.get("thumbnails") or {}
                thumb = (thumbs.get("medium") or thumbs.get("default")
                         or {}).get("url", "")
                videos.append({
                    "video_id": rid,
                    "title": sn.get("title", ""),
                    "thumbnail_url": thumb,
                    "published_at": sn.get("publishedAt", ""),
                })
            payload = {"videos": videos}
            self._social_cache["youtube"] = (time.time(), payload)
            return web.json_response(payload)
        except Exception as e:
            print(f"[PublicWebServer] youtube feed error: {e}")
            stale = self._social_cache_stale("youtube")
            return web.json_response(
                stale or {"videos": [], "error": "fetch_failed"})

    async def _handle_social_bluesky(self, request: web.Request):
        """Author feed via Bluesky's public AT Protocol API (no auth).

        Skips reposts + replies. Supports cursor pagination
        (?cursor=...) for the landing page's load-more / infinite
        scroll. Response includes the author profile (avatar, display
        name) captured from the feed so the tab can render a real
        Bluesky-style header without a second API call.
        """
        cursor = (request.query.get("cursor") or "").strip()
        cache_key = f"bluesky:{cursor}" if cursor else "bluesky"
        cached = self._social_cache_get(cache_key)
        if cached is not None:
            return web.json_response(cached)
        if not BLUESKY_HANDLE:
            return web.json_response(
                {"posts": [], "error": "not_configured"})
        try:
            params = {"actor": BLUESKY_HANDLE, "limit": "30"}
            if cursor:
                params["cursor"] = cursor
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(
                    "https://public.api.bsky.app/xrpc/"
                    "app.bsky.feed.getAuthorFeed",
                    params=params,
                ) as r:
                    data = await r.json()
            profile = None
            posts = []
            for item in data.get("feed", []):
                if item.get("reason"):
                    continue  # repost
                post = item.get("post") or {}
                record = post.get("record") or {}
                if record.get("reply"):
                    continue
                author = post.get("author") or {}
                if profile is None and author:
                    profile = {
                        "handle": author.get("handle", BLUESKY_HANDLE),
                        "display_name": (author.get("displayName")
                                         or author.get("handle", "")),
                        "avatar": author.get("avatar", ""),
                        "url": (f"https://bsky.app/profile/"
                                f"{author.get('handle', BLUESKY_HANDLE)}"),
                    }
                uri = post.get("uri", "")
                rkey = uri.rsplit("/", 1)[-1] if uri else ""
                image = ""
                embed = post.get("embed") or {}
                images = embed.get("images") or []
                if images:
                    image = images[0].get("thumb", "")
                posts.append({
                    "text": record.get("text", ""),
                    "created_at": record.get("createdAt", ""),
                    "like_count": post.get("likeCount", 0),
                    "reply_count": post.get("replyCount", 0),
                    "repost_count": post.get("repostCount", 0),
                    "image": image,
                    "url": (f"https://bsky.app/profile/"
                            f"{BLUESKY_HANDLE}/post/{rkey}"),
                })
            payload = {
                "posts": posts,
                "handle": BLUESKY_HANDLE,
                "profile": profile,
                "cursor": data.get("cursor", ""),
            }
            self._social_cache[cache_key] = (time.time(), payload)
            return web.json_response(payload)
        except Exception as e:
            print(f"[PublicWebServer] bluesky feed error: {e}")
            stale = self._social_cache_stale(cache_key)
            return web.json_response(
                stale or {"posts": [], "error": "fetch_failed"})

    async def _handle_social_tiktok(self, request: web.Request):
        """Manual config passthrough — TikTok has no usable public
        API. Paste the latest video URL into config_local.py."""
        return web.json_response({
            "profile_url": f"https://www.tiktok.com/@{TIKTOK_USERNAME}",
            "latest_video_url": TIKTOK_LATEST_VIDEO_URL or "",
        })

    # ──────────────────────────────────────────────────────────────────
    #   PRIORITY GOD REQUEST (Stripe)
    # ──────────────────────────────────────────────────────────────────
    #
    # Thin HTTP layer over PriorityRequestPlugin. We translate body
    # parsing + Stripe signature header → plugin call → HTTP status.
    # Business logic (idempotency, queueing, message persistence) all
    # lives in the plugin so it stays testable without an aiohttp
    # request object on hand.

    async def _handle_priority_create(
            self, request: web.Request) -> web.Response:
        """POST /api/priority-request/create
        Body: {god: str, twitch_username: str, message: str}
        Returns: {checkout_url: str, session_id: str}
        """
        plugin = self.priority_request
        if plugin is None or not plugin.is_enabled():
            return web.json_response(
                {"error": "priority_requests_disabled"}, status=503)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": "bad_json"}, status=400)

        god = (body.get("god") or "").strip()
        uname = (body.get("twitch_username") or "").strip()
        msg = body.get("message") or ""

        try:
            result = await plugin.create_session(god, uname, msg)
        except ValueError as e:
            # bad_twitch_username | unknown_god:<raw>
            return web.json_response(
                {"error": str(e)}, status=400)
        except RuntimeError as e:
            # Stripe API failure — upstream issue, not client's fault.
            print(f"[PublicWebServer] priority create upstream "
                  f"error: {e}")
            return web.json_response(
                {"error": "upstream_failed"}, status=502)
        except Exception as e:
            print(f"[PublicWebServer] priority create error: {e}")
            return web.json_response(
                {"error": "internal_error"}, status=500)

        return web.json_response(result)

    async def _handle_stripe_webhook(
            self, request: web.Request) -> web.Response:
        """POST /api/stripe-webhook
        Body: raw Stripe event JSON.
        Header: Stripe-Signature: t=...,v1=...  (signature verify
                requires the RAW bytes, NOT request.json()).
        """
        plugin = self.priority_request
        if plugin is None or not plugin.is_enabled():
            # 200 anyway so Stripe doesn't keep retrying when the
            # feature is intentionally off. The plugin would just
            # reject every retry as disabled.
            return web.json_response(
                {"ok": False, "reason": "disabled"}, status=200)

        # Signature verification requires the unparsed request body —
        # any whitespace or key-order normalization breaks the HMAC.
        raw = await request.read()
        sig = request.headers.get("Stripe-Signature", "")

        result = await plugin.handle_webhook(raw, sig)

        if not result.get("ok"):
            reason = result.get("reason", "")
            # Bad signatures are client errors (someone POSTing
            # without the right secret); everything else is a 200 so
            # Stripe stops retrying once we've made a decision.
            status = 400 if reason in ("bad_signature", "bad_payload",
                                       "no_webhook_secret") else 200
            return web.json_response(result, status=status)

        return web.json_response(result)

    async def _handle_priority_success_page(
            self, request: web.Request) -> web.Response:
        """GET /priority-success — Stripe redirects here after a
        successful payment. The page itself doesn't grant anything
        (the webhook is the source of truth); it just confirms the
        purchase to the user."""
        path = PUBLIC_DIR / "priority-success.html"
        if not path.exists():
            return web.Response(text="Success page missing.", status=500)
        return web.FileResponse(
            path, headers={"Cache-Control": "no-cache"})

    @staticmethod
    def _make_static_handler(fname: str):
        async def handler(request: web.Request) -> web.Response:
            path = PUBLIC_DIR / fname
            if path.exists():
                return web.FileResponse(
                    path,
                    headers={"Cache-Control": "public, max-age=86400"})
            raise web.HTTPNotFound()
        return handler

    async def _handle_auth_js(self, request: web.Request) -> web.Response:
        path = PUBLIC_DIR / "auth.js"
        if path.exists():
            return web.FileResponse(
                path, headers={"Cache-Control": "no-cache"})
        raise web.HTTPNotFound()

    async def _handle_theme_css(self, request: web.Request) -> web.Response:
        path = PUBLIC_DIR / "theme.css"
        if not path.exists():
            return web.Response(text="theme.css missing", status=500)
        return web.FileResponse(path, headers={
            "Content-Type": "text/css",
            "Cache-Control": "public, max-age=300",
        })

    # ──────────────────────────────────────────────────────────────────
    #   WEBSOCKET (live ticks)
    # ──────────────────────────────────────────────────────────────────

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        channel_id = request.match_info["channel_id"]
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        clients = self._ws_clients.setdefault(channel_id, set())
        clients.add(ws)

        try:
            # Just keep the connection open. The page receives pushes.
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    break
                # Allow simple ping messages from the client.
                if msg.type == aiohttp.WSMsgType.TEXT and msg.data == "ping":
                    await ws.send_str("pong")
        finally:
            clients.discard(ws)
            if not clients:
                self._ws_clients.pop(channel_id, None)

        return ws

    async def _handle_twitch_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket per Twitch portfolio for live price ticks. Same
        pattern as _handle_ws, but keyed on lowercase username and
        listed in self._twitch_ws_clients."""
        username = request.match_info["username"].lower()
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        clients = self._twitch_ws_clients.setdefault(username, set())
        clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT and msg.data == "ping":
                    await ws.send_str("pong")
        finally:
            clients.discard(ws)
            if not clients:
                self._twitch_ws_clients.pop(username, None)
        return ws

    async def _handle_god_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket per-god for live price ticks on the god detail page."""
        # Lookup canonical name (case-correct) so a typo'd URL still works
        # AND so the listener-side bucket key is consistent.
        raw_name = request.match_info["name"]
        canonical = raw_name
        if self._db is not None:
            async with self._db.execute(
                    "SELECT god_name FROM god_prices "
                    "WHERE LOWER(god_name) = LOWER(?)",
                    (raw_name,)) as cur:
                row = await cur.fetchone()
            if row:
                canonical = row[0]

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        clients = self._god_ws_clients.setdefault(canonical, set())
        clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT and msg.data == "ping":
                    await ws.send_str("pong")
        finally:
            clients.discard(ws)
            if not clients:
                self._god_ws_clients.pop(canonical, None)
        return ws

    async def _on_overlay_event(self, event_name: str, data: Any) -> None:
        """
        Fired by OverlayManager.add_event_listener for EVERY event.
        We forward god_stock_update and dividend_paid to any portfolio
        page that holds the affected god.
        """
        # economy.py emits 'god_stock_update_kd' for kill/death ticks and
        # 'god_stock_update' for assist ticks — listen to both. Plus the
        # dividend event for the bonus-share compounding.
        if event_name not in ("god_stock_update", "god_stock_update_kd",
                              "dividend_paid"):
            return
        if not isinstance(data, dict):
            return
        # Listener is registered in __init__, but self._db is opened in
        # start(). In the small window between, ignore events.
        if self._db is None:
            return

        god_name = data.get("god_name") or data.get("god")
        if not god_name:
            return

        # If nothing is listening, skip. All three buckets contribute.
        if (not self._ws_clients
                and not self._twitch_ws_clients
                and not self._god_ws_clients):
            return

        # YouTube portfolio clients: filter to only those channels that
        # actually hold this god.
        affected_yt_channels: Set[str] = set()
        if self._ws_clients:
            try:
                async with self._db.execute("""
                    SELECT DISTINCT yt_channel_id
                      FROM youtube_holdings
                     WHERE god_name = ? AND shares > 0.001
                """, (god_name,)) as cur:
                    async for r in cur:
                        affected_yt_channels.add(r[0])
            except Exception as e:
                print(f"[PublicWebServer] holder lookup failed (yt): {e}")

        # Twitch portfolio clients: same filter against the Twitch table.
        affected_twitch_users: Set[str] = set()
        if self._twitch_ws_clients:
            try:
                async with self._db.execute("""
                    SELECT DISTINCT LOWER(username)
                      FROM portfolios
                     WHERE god_name = ? AND shares > 0.001
                """, (god_name,)) as cur:
                    async for r in cur:
                        affected_twitch_users.add(r[0])
            except Exception as e:
                print(f"[PublicWebServer] holder lookup failed (twitch): {e}")

        msg = json.dumps({"event": event_name, "data": data})
        async with self._send_lock:
            # Fan out to per-YT-portfolio clients.
            for channel_id in affected_yt_channels:
                clients = self._ws_clients.get(channel_id)
                if not clients:
                    continue
                for ws in list(clients):
                    try:
                        await ws.send_str(msg)
                    except Exception:
                        clients.discard(ws)

            # Fan out to per-Twitch-portfolio clients.
            for username in affected_twitch_users:
                clients = self._twitch_ws_clients.get(username)
                if not clients:
                    continue
                for ws in list(clients):
                    try:
                        await ws.send_str(msg)
                    except Exception:
                        clients.discard(ws)

            # Fan out to per-god clients (filtered by exact god match).
            god_clients = self._god_ws_clients.get(god_name)
            if god_clients:
                for ws in list(god_clients):
                    try:
                        await ws.send_str(msg)
                    except Exception:
                        god_clients.discard(ws)
