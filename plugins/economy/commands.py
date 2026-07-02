"""
plugins/economy/commands.py
===========================
Chat command handlers for the Hatmas Market.

Surface mirrors the docs:
  !buy [god] [amount|all]      → execute_buy()
  !sell [god] [amount|all]     → execute_sell()
  !portfolio                   → show holdings + emit overlay event
  !price [god]                 → current price + win-rate readout
  !market / !stocks            → top movers (session change)
  !dividend                    → last dividend payout

Each command:
  1. Honors the dashboard 'economy' feature toggle.
  2. Bails early if self._db is None (still loading or aiosqlite missing).
  3. Skips silently for excluded users (bots) so we don't bot-loop.
  4. Enforces a per-user cooldown via _check_cooldown.
  5. Calls into the trading / portfolio / pricing mixins for actual work.
"""

from __future__ import annotations

import time
from typing import Optional

from core.config import ECONOMY_STARTING_PRICE


# Per-command cooldowns (seconds). Defaults tuned to keep chat readable
# without making the commands feel sluggish. Edit here, no other side
# effects — the cooldown helper is the only consumer.
TRADE_COOLDOWN = 3.0
PORTFOLIO_COOLDOWN = 10.0
PRICE_COOLDOWN = 5.0
MARKET_COOLDOWN = 15.0
DIVIDEND_COOLDOWN = 10.0


class _CommandsMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self.bot                (for is_feature_enabled + send_reply)
      self._db, self._prices, self._games_played
      self._session_changes   (top-movers calculation)
      self._last_dividend     (cached !dividend output)
      self._cooldowns         {username: {command: timestamp}}
    Calls into _TradingMixin (execute_buy/sell, _get_holding,
    _get_full_portfolio, _get_balance), _GodNamesMixin (_resolve_god_name),
    _FairValueMixin (_get_volatility), _HelpersMixin (is_excluded_user,
    _get_profile_image), _OverlaysMixin (_emit_overlay_event).
    """

    def _check_cooldown(self, username: str, command: str, cooldown: float) -> Optional[int]:
        """Check if a command is on cooldown. Returns remaining seconds or None."""
        now = time.time()
        user_cds = self._cooldowns.setdefault(username, {})
        last_use = user_cds.get(command, 0)
        if now - last_use < cooldown:
            return int(cooldown - (now - last_use))
        user_cds[command] = now
        return None

    async def cmd_buy(self, message, args, whisper=False):
        """!buy [god] [amount] — Buy shares of a god with hats."""
        if not self.bot.is_feature_enabled("economy"):
            return
        if not self._db:
            await self.bot.send_reply(message, "Economy is still loading. Try again in a moment.", whisper)
            return

        username = message.chatter.name.lower()
        # Bots / excluded accounts can't earn or spend hats. Silently
        # skip — no chat reply (avoids back-and-forth between two bots).
        if self.is_excluded_user(username):
            return
        remaining = self._check_cooldown(username, "trade", TRADE_COOLDOWN)
        if remaining:
            await self.bot.send_reply(message, f"Trade cooldown: {remaining}s", whisper)
            return

        parts = args.strip().split() if args else []
        if len(parts) < 2:
            await self.bot.send_reply(
                message, "Use !buy [god] [amount] to purchase shares.", whisper
            )
            return

        # Last token is amount, everything before is god name
        try:
            amount_str = parts[-1].lower().replace(",", "")
            if amount_str == "all":
                balance = await self._get_balance(username)
                if not balance or balance <= 0:
                    await self.bot.send_reply(message, "You have no hats!", whisper)
                    return
                hat_amount = balance
            else:
                hat_amount = int(amount_str)
        except ValueError:
            await self.bot.send_reply(
                message, "Amount must be a number or 'all'. Use !buy [god] [amount]", whisper
            )
            return

        god_input = " ".join(parts[:-1])
        result = await self.execute_buy(username, god_input, hat_amount)

        if result["success"]:
            await self.bot.send_reply(
                message,
                f"Bought {result['shares']:.1f} shares of {result['god_name']} "
                f"at {result['price']:.0f} hats/share "
                f"(cost: {result['total_cost']:,} hats)" +
                (f" (fee: {result['fee']:,})" if result.get('fee', 0) > 0 else ""),
                whisper
            )
        else:
            await self.bot.send_reply(message, result["error"], whisper)

    async def cmd_sell(self, message, args, whisper=False):
        """!sell [god] [amount] — Sell shares of a god for hats."""
        if not self.bot.is_feature_enabled("economy"):
            return
        if not self._db:
            await self.bot.send_reply(message, "Economy is still loading. Try again in a moment.", whisper)
            return

        username = message.chatter.name.lower()
        if self.is_excluded_user(username):
            return  # silent skip for bot accounts
        remaining = self._check_cooldown(username, "trade", TRADE_COOLDOWN)
        if remaining:
            await self.bot.send_reply(message, f"Trade cooldown: {remaining}s", whisper)
            return

        parts = args.strip().split() if args else []
        if len(parts) < 2:
            await self.bot.send_reply(
                message, "Use !sell [god] [amount] to sell shares. Use 'all' to sell everything.", whisper
            )
            return

        god_input = " ".join(parts[:-1])
        amount_str = parts[-1].lower().replace(",", "")

        god_name = self._resolve_god_name(god_input)
        if not god_name:
            await self.bot.send_reply(message, f"Unknown god: {god_input}", whisper)
            return

        if amount_str == "all":
            holding = await self._get_holding(username, god_name)
            if not holding or holding["shares"] <= 0:
                await self.bot.send_reply(message, f"You don't own any {god_name} shares", whisper)
                return
            # round(), not int(): truncation requested slightly less
            # than the full position value, leaving dust shares behind
            # after every "sell all". Rounding up overshoots by <=0.5
            # hat and execute_sell clamps to the actual holding.
            hat_amount = int(round(holding["shares"]
                                   * self._prices.get(god_name, 0)))
        else:
            try:
                hat_amount = int(amount_str)
            except ValueError:
                await self.bot.send_reply(
                    message, "Amount must be a number or 'all'. Use !sell [god] [amount]", whisper
                )
                return

        result = await self.execute_sell(username, god_input, hat_amount)

        if result["success"]:
            await self.bot.send_reply(
                message,
                f"Sold {result['shares']:.1f} shares of {result['god_name']} "
                f"at {result['price']:.0f} hats/share "
                f"(received: {result['net_received']:,} hats)" +
                (f" (fee: {result['fee']:,})" if result.get('fee', 0) > 0 else ""),
                whisper
            )
        else:
            await self.bot.send_reply(message, result["error"], whisper)

    async def cmd_portfolio(self, message, args, whisper=False):
        """!portfolio — View your holdings with current value and P&L."""
        if not self.bot.is_feature_enabled("economy"):
            return
        if not self._db:
            await self.bot.send_reply(message, "Economy is still loading. Try again in a moment.", whisper)
            return

        username = message.chatter.name.lower()
        remaining = self._check_cooldown(username, "portfolio", PORTFOLIO_COOLDOWN)
        if remaining:
            await self.bot.send_reply(message, f"Cooldown: {remaining}s", whisper)
            return

        holdings = await self._get_full_portfolio(username)
        if not holdings:
            await self.bot.send_reply(
                message, "You don't own any shares yet. Use !buy [god] [amount] to get started.", whisper
            )
            return

        # Sort by current value descending, show top 3
        total_value = sum(h["value"] for h in holdings)
        balance = await self._get_balance(username) or 0
        net_worth = total_value + balance
        by_value = sorted(holdings, key=lambda h: h["value"], reverse=True)

        lines = []
        for h in by_value[:3]:
            lines.append(f"{h['shares']:.1f} {h['god_name']} shares")

        summary = " | ".join(lines)
        await self.bot.send_reply(
            message,
            f"Net Worth: {net_worth:,.0f} hats | {summary}",
            whisper
        )

        # Emit overlay event for portfolio display
        pfp_url = await self._get_profile_image(username)
        self._emit_overlay_event("portfolio_requested", {
            "username": username,
            "display_name": message.chatter.display_name,
            "profile_image_url": pfp_url,
            "holdings": holdings,
            "total_value": round(total_value),
            "total_pnl": round(sum(h["pnl"] for h in holdings)),
            "hat_balance": balance,
        })

    async def cmd_price(self, message, args, whisper=False):
        """!price [god] — Current price, recent trend, volatility tier."""
        if not self.bot.is_feature_enabled("economy"):
            return
        if not self._db:
            await self.bot.send_reply(message, "Economy is still loading. Try again in a moment.", whisper)
            return

        username = message.chatter.name.lower()
        remaining = self._check_cooldown(username, "price", PRICE_COOLDOWN)
        if remaining:
            await self.bot.send_reply(message, f"Cooldown: {remaining}s", whisper)
            return

        if not args or not args.strip():
            await self.bot.send_reply(
                message, "Use !price [god] for the current market price.", whisper
            )
            return

        god_name = self._resolve_god_name(args.strip())
        if not god_name:
            await self.bot.send_reply(message, f"Unknown god: {args.strip()}", whisper)
            return

        price = self._prices.get(god_name, ECONOMY_STARTING_PRICE)
        vol_mult, vol_tier = self._get_volatility(god_name)
        games = self._games_played.get(god_name, 0)
        session_change = self._session_changes.get(god_name, 0)
        sign = "+" if session_change >= 0 else ""

        # Win rate
        async with self._db.execute(
            "SELECT total_wins, total_losses FROM god_prices WHERE god_name = ?",
            (god_name,)
        ) as cursor:
            row = await cursor.fetchone()
            wins, losses = (row[0], row[1]) if row else (0, 0)
            wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        await self.bot.send_reply(
            message,
            f"{god_name}: {price:,.0f} hats | {games} games ({wr:.0f}% WR)",
            whisper
        )

    async def cmd_market(self, message, args, whisper=False):
        """!market / !stocks — Top movers, gainers/losers."""
        if not self.bot.is_feature_enabled("economy"):
            return
        if not self._db:
            await self.bot.send_reply(message, "Economy is still loading. Try again in a moment.", whisper)
            return

        username = message.chatter.name.lower()
        remaining = self._check_cooldown(username, "market", MARKET_COOLDOWN)
        if remaining:
            await self.bot.send_reply(message, f"Cooldown: {remaining}s", whisper)
            return

        if not self._prices:
            await self.bot.send_reply(message, "No market data yet. Play a match to get started.", whisper)
            return

        # Sort by session change
        sorted_gods = sorted(
            self._session_changes.items(),
            key=lambda x: x[1],
            reverse=True
        )

        # Top 3 gainers, top 3 losers
        gainers = [(g, c) for g, c in sorted_gods if c > 0][:3]
        losers = [(g, c) for g, c in sorted_gods if c < 0][-3:]
        losers.reverse()

        parts = []
        if gainers:
            g_str = " ".join(f"{g} +{c:.1f}%" for g, c in gainers)
            parts.append(f"Gainers: {g_str}")
        if losers:
            l_str = " ".join(f"{g} {c:.1f}%" for g, c in losers)
            parts.append(f"Losers: {l_str}")
        if not parts:
            # Show top gods by price
            top = sorted(self._prices.items(), key=lambda x: x[1], reverse=True)[:5]
            price_str = " | ".join(f"{g}: {p:,.0f}" for g, p in top)
            parts.append(f"Top: {price_str}")

        await self.bot.send_reply(
            message,
            " | ".join(parts) + f" | {len(self._prices)} gods tracked",
            whisper
        )

    async def cmd_dividend(self, message, args, whisper=False):
        """!dividend — Show most recent dividend payout."""
        if not self.bot.is_feature_enabled("economy"):
            return
        if not self._db:
            await self.bot.send_reply(message, "Economy is still loading. Try again in a moment.", whisper)
            return

        username = message.chatter.name.lower()
        remaining = self._check_cooldown(username, "dividend", DIVIDEND_COOLDOWN)
        if remaining:
            await self.bot.send_reply(message, f"Cooldown: {remaining}s", whisper)
            return

        if not self._last_dividend:
            # Check database for most recent
            async with self._db.execute(
                "SELECT god_name, rate, price, total_hats, holders, timestamp "
                "FROM dividends ORDER BY timestamp DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    self._last_dividend = {
                        "god_name": row[0], "rate": row[1], "price": row[2],
                        "total_hats": row[3], "holders": row[4], "timestamp": row[5],
                    }

        if not self._last_dividend:
            await self.bot.send_reply(
                message, "No dividends paid yet this session!", whisper
            )
            return

        d = self._last_dividend
        await self.bot.send_reply(
            message,
            f"Last dividend: {d['god_name']} {d['rate']*100:.0f}% "
            f"({d['total_hats']:,.0f} hats to {d['holders']} holders)",
            whisper
        )
