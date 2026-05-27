"""
plugins/economy/trading.py
==========================
Buy / sell + portfolio queries.

Public surface:
  * `execute_buy(username, god_name, hat_amount)` -
        deduct hats from MixItUp, add the equivalent share count to
        the portfolio (weighted average cost basis), record the
        transaction row, emit a trade-feed event.
  * `execute_sell(username, god_name, hat_amount)` -
        the inverse. hat_amount is the desired hat value to receive;
        we compute shares from current price.
  * `_get_holding`, `_get_position_value`, `_get_portfolio_value`,
        `_get_full_portfolio` - read paths used by the !portfolio
        command, the public webserver portfolio page, and the
        leaderboard query.

The actual user-facing chat commands live in commands.py - those
parse args (amount/all/half/quarter), enforce cooldowns, then call
execute_buy / execute_sell here.

Both execute_* paths are idempotent in the sense that they validate
balance + cooldown before mutating MixItUp; if anything fails between
the balance deduction and the portfolio update we'd have a problem,
but in practice MixItUp's API is local + reliable so it's not
something we currently guard against.

Post-airtight-economy pass: transaction fees are removed. The `fee`
column in `transactions` is preserved at 0 for schema compatibility.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class _TradingMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self._db         - for portfolios + transactions writes
      self._prices     - current prices for share/value math
      MixItUp helpers from _MixItUpMixin (_get_balance, _adjust_balance)
      God name resolution from _GodNamesMixin (_resolve_god_name)
      Schema helpers from _DBMixin (_ensure_god_exists)
      Overlay emit from _OverlaysMixin (_emit_trade_event)
    """

    async def execute_buy(self, username: str, god_name: str,
                          hat_amount: int) -> Dict[str, Any]:
        """
        Buy shares of a god with hats.

        Returns dict with: success, shares, price, fee, total_cost, error.
        `fee` is always 0 - trading is fee-free.
        """
        god_name = self._resolve_god_name(god_name)
        if not god_name:
            return {"success": False, "error": "Unknown god"}

        await self._ensure_god_exists(god_name)
        price = self._prices[god_name]

        if hat_amount < 1:
            return {"success": False, "error": "Minimum investment is 1 hat"}

        # Fee-free trading: every hat spent buys shares at the current price.
        shares = hat_amount / price

        if shares <= 0:
            return {"success": False, "error": "Amount too small"}

        # Check balance
        balance = await self._get_balance(username)
        if balance is None:
            return {"success": False, "error": "Could not check balance"}
        if balance < hat_amount:
            return {"success": False, "error": f"Not enough hats (have {balance:,})"}

        # Execute: deduct hats
        success = await self._adjust_balance(username, -hat_amount)
        if not success:
            return {"success": False, "error": "Transaction failed"}

        # Update portfolio
        await self._add_shares(username, god_name, shares, price)

        # Record transaction (fee column kept at 0 for schema compat)
        await self._db.execute("""
            INSERT INTO transactions (username, god_name, type, shares, price, total, fee)
            VALUES (?, ?, 'buy', ?, ?, ?, 0)
        """, (username, god_name, shares, price, hat_amount))
        await self._db.commit()

        # Emit overlay event
        self._emit_trade_event("buy", username, god_name, shares, price, hat_amount, 0)

        return {
            "success": True,
            "shares": shares,
            "price": price,
            "fee": 0,
            "total_cost": hat_amount,
            "god_name": god_name,
        }

    async def execute_sell(self, username: str, god_name: str,
                           hat_amount: int) -> Dict[str, Any]:
        """
        Sell shares of a god for hats.

        hat_amount is the desired hat value to sell. Sells the equivalent shares.
        Returns dict with: success, shares, price, fee, net_received, error.
        `fee` is always 0 - trading is fee-free.
        """
        god_name = self._resolve_god_name(god_name)
        if not god_name:
            return {"success": False, "error": "Unknown god"}

        price = self._prices.get(god_name, 0)
        if price <= 0:
            return {"success": False, "error": "No market data for this god"}

        # How many shares does user hold?
        holding = await self._get_holding(username, god_name)
        if holding is None or holding["shares"] <= 0:
            return {"success": False, "error": f"You don't own any {god_name} shares"}

        # Calculate shares to sell
        shares_to_sell = hat_amount / price
        if shares_to_sell > holding["shares"]:
            shares_to_sell = holding["shares"]  # Sell all

        # Fee-free: net == gross.
        net_received = int(shares_to_sell * price)

        if net_received <= 0:
            return {"success": False, "error": "Amount too small"}

        # Execute: add hats
        success = await self._adjust_balance(username, net_received)
        if not success:
            return {"success": False, "error": "Transaction failed"}

        # Update portfolio
        await self._remove_shares(username, god_name, shares_to_sell)

        # Record transaction (fee column kept at 0 for schema compat)
        await self._db.execute("""
            INSERT INTO transactions (username, god_name, type, shares, price, total, fee)
            VALUES (?, ?, 'sell', ?, ?, ?, 0)
        """, (username, god_name, shares_to_sell, price, net_received))
        await self._db.commit()

        # Emit overlay event
        self._emit_trade_event("sell", username, god_name, shares_to_sell, price, net_received, 0)

        return {
            "success": True,
            "shares": shares_to_sell,
            "price": price,
            "fee": 0,
            "net_received": net_received,
            "god_name": god_name,
        }

    async def _add_shares(self, username: str, god_name: str, shares: float, price: float):
        """Add shares to a user's portfolio, updating average cost basis."""
        existing = await self._get_holding(username, god_name)
        if existing and existing["shares"] > 0:
            # Weighted average cost
            total_shares = existing["shares"] + shares
            avg_cost = ((existing["avg_cost"] * existing["shares"]) + (price * shares)) / total_shares
            await self._db.execute("""
                UPDATE portfolios SET shares = ?, avg_cost = ? WHERE username = ? AND god_name = ?
            """, (total_shares, avg_cost, username, god_name))
        else:
            await self._db.execute("""
                INSERT INTO portfolios (username, god_name, shares, avg_cost)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username, god_name) DO UPDATE SET
                    shares = excluded.shares, avg_cost = excluded.avg_cost
            """, (username, god_name, shares, price))
        await self._db.commit()

    async def _remove_shares(self, username: str, god_name: str, shares: float):
        """Remove shares from a user's portfolio."""
        await self._db.execute("""
            UPDATE portfolios SET shares = MAX(shares - ?, 0)
            WHERE username = ? AND god_name = ?
        """, (shares, username, god_name))
        # Clean up zero holdings
        await self._db.execute("""
            DELETE FROM portfolios WHERE username = ? AND god_name = ? AND shares < 0.001
        """, (username, god_name))
        await self._db.commit()

    async def _get_holding(self, username: str, god_name: str) -> Optional[Dict]:
        """Get a user's holding for a specific god."""
        async with self._db.execute(
            "SELECT shares, avg_cost FROM portfolios WHERE username = ? AND god_name = ?",
            (username, god_name)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"shares": row[0], "avg_cost": row[1]}
        return None

    async def _get_position_value(self, username: str, god_name: str) -> float:
        """Get value of a user's position in a specific god."""
        holding = await self._get_holding(username, god_name)
        if not holding:
            return 0.0
        return holding["shares"] * self._prices.get(god_name, 0)

    async def _get_portfolio_value(self, username: str) -> float:
        """Get total portfolio value for a user."""
        total = 0.0
        async with self._db.execute(
            "SELECT god_name, shares FROM portfolios WHERE username = ?",
            (username,)
        ) as cursor:
            async for row in cursor:
                god_name, shares = row
                price = self._prices.get(god_name, 0)
                total += shares * price
        return total

    async def _get_full_portfolio(self, username: str) -> List[Dict]:
        """Get all holdings for a user with current values."""
        holdings = []
        async with self._db.execute(
            "SELECT god_name, shares, avg_cost FROM portfolios WHERE username = ? AND shares > 0.001 ORDER BY shares * avg_cost DESC",
            (username,)
        ) as cursor:
            async for row in cursor:
                god_name, shares, avg_cost = row
                price = self._prices.get(god_name, 0)
                value = shares * price
                cost_basis = shares * avg_cost
                pnl = value - cost_basis
                pnl_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0
                holdings.append({
                    "god_name": god_name,
                    "shares": shares,
                    "avg_cost": avg_cost,
                    "price": price,
                    "value": value,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                })
        return holdings
