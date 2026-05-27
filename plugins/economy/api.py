"""
plugins/economy/api.py
======================
Aiohttp routes the economy plugin adds to the dashboard webserver
(port 8069). main.py calls `economy.register_api_routes(web.app)`
right after registering the plugin.

Routes:
  GET /api/economy/market               — every god's price + sparkline
  GET /api/economy/portfolio?user=...   — one viewer's holdings
  GET /api/economy/leaderboard          — top 20 portfolios by value
  GET /api/economy/price/{god}          — single-god price data

These power the dashboard's economy panel and any external tools
poking at the bot during development. The PUBLIC webserver
(port 8070) has its own, separately-defined routes in
core/public_webserver.py — they hit the DB directly rather than
calling these.
"""

from __future__ import annotations


class _APIMixin:
    """
    Mixed into EconomyPlugin. Reads:
      self._prices, self._games_played, self._price_history,
      self._session_changes, self._match_active, self._db
    Calls into _FairValueMixin (_get_volatility), _GodNamesMixin
    (_resolve_god_name), _TradingMixin (_get_full_portfolio),
    _MixItUpMixin (_get_balance).
    """

    def register_api_routes(self, app):
        """Register economy API endpoints on the webserver's aiohttp app."""
        from aiohttp import web

        async def handle_market(request):
            """GET /api/economy/market — All god prices and metadata."""
            gods = []
            for god_name, price in sorted(self._prices.items()):
                vol_mult, vol_tier = self._get_volatility(god_name)
                gods.append({
                    "name": god_name,
                    "price": round(price),
                    "games_played": self._games_played.get(god_name, 0),
                    "volatility_tier": vol_tier,
                    "volatility_mult": vol_mult,
                    "session_change": round(self._session_changes.get(god_name, 0), 1),
                    "history": self._price_history.get(god_name, []),
                })
            return web.json_response({"gods": gods, "match_active": self._match_active})

        async def handle_portfolio(request):
            """GET /api/economy/portfolio?user=username — User portfolio."""
            username = request.query.get("user", "").lower()
            if not username:
                return web.json_response({"error": "user parameter required"}, status=400)
            holdings = await self._get_full_portfolio(username)
            total_value = sum(h["value"] for h in holdings)
            balance = await self._get_balance(username)
            return web.json_response({
                "username": username,
                "holdings": holdings,
                "total_value": round(total_value),
                "hat_balance": balance or 0,
            })

        async def handle_leaderboard(request):
            """GET /api/economy/leaderboard — Top investors."""
            leaderboard = []
            async with self._db.execute("""
                SELECT p.username, SUM(p.shares * gp.price) as portfolio_value
                FROM portfolios p
                JOIN god_prices gp ON p.god_name = gp.god_name
                WHERE p.shares > 0.001
                GROUP BY p.username
                ORDER BY portfolio_value DESC
                LIMIT 20
            """) as cursor:
                rank = 1
                async for row in cursor:
                    leaderboard.append({
                        "rank": rank,
                        "username": row[0],
                        "portfolio_value": round(row[1]),
                    })
                    rank += 1
            return web.json_response({"leaderboard": leaderboard})

        async def handle_god_price(request):
            """GET /api/economy/price/{god} — Single god price data."""
            god_input = request.match_info.get("god", "")
            god_name = self._resolve_god_name(god_input)
            if not god_name:
                return web.json_response({"error": "Unknown god"}, status=404)
            price = self._prices.get(god_name, 0)
            vol_mult, vol_tier = self._get_volatility(god_name)
            return web.json_response({
                "name": god_name,
                "price": round(price),
                "games_played": self._games_played.get(god_name, 0),
                "volatility_tier": vol_tier,
                "session_change": round(self._session_changes.get(god_name, 0), 1),
                "history": self._price_history.get(god_name, []),
            })

        app.router.add_get("/api/economy/market", handle_market)
        app.router.add_get("/api/economy/portfolio", handle_portfolio)
        app.router.add_get("/api/economy/leaderboard", handle_leaderboard)
        app.router.add_get("/api/economy/price/{god}", handle_god_price)
