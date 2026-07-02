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

import asyncio
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

    def _user_trade_lock(self, username: str) -> asyncio.Lock:
        """Per-user lock serializing the balance-check -> deduct ->
        portfolio-write sequence across every trade entry point (chat
        commands, website /api/trade, tests). Without it, two
        concurrent trades for the same user can both pass the balance
        check and double-spend hats the user only has once. Lazily
        created so the mixin works in stripped-down test harnesses."""
        locks = getattr(self, "_trade_locks_by_user", None)
        if locks is None:
            locks = {}
            self._trade_locks_by_user = locks
        key = (username or "").lower()
        lock = locks.get(key)
        if lock is None:
            lock = locks[key] = asyncio.Lock()
        return lock

    async def execute_buy(self, username: str, god_name: str,
                          hat_amount: int,
                          channel: str = "chat") -> Dict[str, Any]:
        """
        Buy shares of a god with hats.

        Returns dict with: success, shares, price, fee, total_cost, error.
        `fee` is always 0 - trading is fee-free.

        Runs under a per-user lock (see _user_trade_lock). If the
        portfolio/ledger write fails AFTER hats were deducted from
        MixItUp, the hats are refunded so the user never pays for
        shares they didn't receive.
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

        async with self._user_trade_lock(username):
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

            # Hats are gone from MixItUp now. If the portfolio/ledger
            # write fails, refund - otherwise the user paid and got
            # no shares.
            try:
                # Update portfolio
                await self._add_shares(username, god_name, shares, price)

                # Record transaction (fee column kept at 0 for schema compat)
                await self._db.execute("""
                    INSERT INTO transactions (username, god_name, type, shares, price, total, fee, channel)
                    VALUES (?, ?, 'buy', ?, ?, ?, 0, ?)
                """, (username, god_name, shares, price, hat_amount, channel))
                await self._db.commit()
            except Exception as e:
                print(f"[Economy] Buy failed after deduct - refunding "
                      f"{hat_amount} hats to {username}: {e}")
                refunded = await self._adjust_balance(username, hat_amount)
                if not refunded:
                    print(f"[Economy] CRITICAL: refund failed - {username} "
                          f"is owed {hat_amount} hats (buy {god_name})")
                return {"success": False, "error": "Trade failed"}

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
                           hat_amount: int,
                           channel: str = "chat") -> Dict[str, Any]:
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

        async with self._user_trade_lock(username):
            # How many shares does user hold?
            holding = await self._get_holding(username, god_name)
            if holding is None or holding["shares"] <= 0:
                return {"success": False, "error": f"You don't own any {god_name} shares"}

            # Calculate shares to sell
            shares_to_sell = hat_amount / price
            if shares_to_sell > holding["shares"]:
                shares_to_sell = holding["shares"]  # Sell all

            # Fee-free: net == gross. round() rather than int(): the
            # float round-trip (hat_amount / price * price) can land at
            # 4.999999... and a bare int() would short the seller a hat.
            net_received = int(round(shares_to_sell * price))

            if net_received <= 0:
                return {"success": False, "error": "Amount too small"}

            # Remove shares first (local DB - the reliable side), then
            # credit hats via MixItUp HTTP (the flaky side). If the
            # credit fails, restore the shares at their original avg
            # cost so the seller ends up exactly where they started.
            await self._remove_shares(username, god_name, shares_to_sell)

            success = await self._adjust_balance(username, net_received)
            if not success:
                await self._add_shares(username, god_name, shares_to_sell,
                                       holding["avg_cost"])
                return {"success": False, "error": "Transaction failed"}

            # Money has moved correctly on both sides now. A failure
            # writing the history row shouldn't fail (or unwind) the
            # trade - log it and move on.
            try:
                # Record transaction (fee column kept at 0 for schema compat)
                await self._db.execute("""
                    INSERT INTO transactions (username, god_name, type, shares, price, total, fee, channel)
                    VALUES (?, ?, 'sell', ?, ?, ?, 0, ?)
                """, (username, god_name, shares_to_sell, price, net_received, channel))
                await self._db.commit()
            except Exception as e:
                print(f"[Economy] Sell ledger write failed for {username} "
                      f"({god_name}, {net_received} hats) - trade itself "
                      f"completed: {e}")

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
            # New rows inherit the user's leaderboard visibility from
            # their existing holdings — otherwise a hidden user buying
            # a new god would silently reappear on that god's holder
            # list (the opt-out flag is per-row).
            await self._db.execute("""
                INSERT INTO portfolios (username, god_name, shares,
                                        avg_cost, leaderboard_opt_out)
                VALUES (?, ?, ?, ?,
                        COALESCE((SELECT MAX(leaderboard_opt_out)
                                    FROM portfolios
                                   WHERE LOWER(username) = LOWER(?)), 0))
                ON CONFLICT(username, god_name) DO UPDATE SET
                    shares = excluded.shares, avg_cost = excluded.avg_cost
            """, (username, god_name, shares, price, username))
        await self._db.commit()

    # ── leaderboard visibility (website account toggle) ──
    # The leaderboard_opt_out column + query filters have existed
    # since the airtight pass, but nothing ever flipped the flag —
    # the documented !hideme chat command was never implemented.
    # The website toggle (POST /api/me/visibility) is the first
    # real control. Applies to all current rows; _add_shares
    # inheritance keeps future buys consistent.

    async def get_leaderboard_hidden(self, username: str) -> bool:
        async with self._db.execute(
            "SELECT COALESCE(MAX(leaderboard_opt_out), 0) "
            "FROM portfolios WHERE LOWER(username) = LOWER(?)",
            (username,)
        ) as cursor:
            row = await cursor.fetchone()
        return bool(row and row[0])

    async def set_leaderboard_hidden(self, username: str,
                                     hidden: bool) -> None:
        await self._db.execute(
            "UPDATE portfolios SET leaderboard_opt_out = ? "
            "WHERE LOWER(username) = LOWER(?)",
            (1 if hidden else 0, username))
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
