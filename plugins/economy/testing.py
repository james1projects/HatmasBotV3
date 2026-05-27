"""
plugins/economy/testing.py
==========================
Sim + test-overlay endpoints, plus the price seeder + reloader.

`simulate_game()` runs a full match lifecycle end-to-end (god_detected
→ live ticks → match_end → match_result) so the operator can preview
the entire overlay pipeline from the dashboard's "Run Sim" button
without needing a real Smite match.

`emit_test_*()` methods drive each overlay individually with sample
data — handy for tweaking overlay layouts and animations without
having to wait for a real event to fire.

`reload_prices()` reloads the in-memory price cache from the DB.
Used after running `tools/replay_economy.py` so the live bot picks
up the recomputed prices without a restart.

`seed_prices()` is called by `tools/seed_economy.py` to populate
god_prices from historical tracker.gg data.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Dict

from core.config import ECONOMY_STARTING_PRICE


class _TestingMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self._db, self._prices, self._games_played, self._price_history,
      self._god_names, self._match_active, self._match_god, self._match_kda
      self.bot              — for accessing web_server's overlay manager
    Calls into _DBMixin, _MatchMixin, _TickingMixin, _OverlaysMixin,
    _HelpersMixin via mixin composition.
    """

    async def simulate_game(self, god_name: str, outcome: str = "win",
                            kills: int = 7, deaths: int = 3, assists: int = 4,
                            speed: float = 1.0, force: bool = False):
        """
        Simulate a full match lifecycle for testing overlays + economy.

        Drives the same event sequence the live bot does:
          1. on_match_confirmed (synthetic match_id) → dividend + ticker
             overlay arms + cosmetic ticks enabled
          2. on_kill / on_death / on_assist → cosmetic ticks + flash effects
          3. on_match_end → cosmetic ticks stop
          4. settle_match called directly with the simulated outcome —
             same code path as the live backfill, but with a fake
             match_id so dedup tracks it and reruns don't double-fire.

        speed: multiplier for delay durations (0.5 = double speed, 2.0 = slow)
        force: if True, reset any stale match state before simulating

        Side effects (dividends, free shares, match-end overlay) are
        gated on `_is_broadcaster_live()`. To make the simulator fire
        them end-to-end without needing a real Twitch stream, we flip
        the `_sim_force_live` escape hatch on for the duration of the
        sim and clear it in `finally`.
        """
        if not self._db:
            print("[Economy] Cannot simulate — database not initialized")
            return {"error": "Economy not ready (database not initialized)"}

        if self._match_active:
            if force:
                print("[Economy] Force-resetting stale match state for simulation")
                self._match_active = False
                self._match_god = None
                self._match_id = None
                self._match_start_price = 0.0
                self._match_kda = [0, 0, 0]
            else:
                print("[Economy] Cannot simulate — match already in progress (use force=True to override)")
                return {"error": "Match already in progress"}

        print(f"\n[Economy] ═══ SIMULATING GAME: {god_name} ═══")
        print(f"[Economy] Outcome: {outcome} | KDA: {kills}/{deaths}/{assists} | Speed: {speed}x")

        delay = lambda secs: asyncio.sleep(secs * speed)

        # Synthetic match_id so dedup tracks the sim through the same
        # path as a real tracker.gg match. Prefix lets us identify and
        # clean up sim rows later.
        sim_match_id = f"sim-{uuid.uuid4().hex[:12]}"

        # Force broadcaster-live for the duration of the sim so
        # dividends + free shares fire even when the bot isn't
        # actually streaming.
        self._sim_force_live = True
        try:
            # Ensure god exists
            await self._ensure_god_exists(god_name)
            starting_price = self._prices[god_name]

            # ── Step 1: Match Confirmed (fires dividend, starts cosmetic ticks) ──
            print(f"[Economy] Step 1: Match confirmed — {god_name} ({sim_match_id})")
            await self.on_match_confirmed({
                "match_id": sim_match_id,
                "god": god_name,
                "team": "Order",
            })
            await delay(2.0)

            # ── Step 2: Simulate KDA events with realistic timing ──
            # Interleave kills, deaths, assists in a plausible order.
            # These now produce COSMETIC overlay events only; the
            # printed cosmetic price comes from the local computation
            # in ticking.py, not from self._prices (which never moves
            # during a match under the new design).
            events = (
                [("kill", "player_kill")] * kills +
                [("death", None)] * deaths +
                [("assist", None)] * assists
            )
            random.shuffle(events)

            for i, (event_type, kill_type) in enumerate(events):
                if event_type == "kill":
                    await self.on_kill(kill_type or "player_kill")
                    print(f"[Economy]   Sim kill #{self._match_kda[0]}")
                elif event_type == "death":
                    await self.on_death()
                    print(f"[Economy]   Sim death #{self._match_kda[1]}")
                elif event_type == "assist":
                    await self.on_assist()
                    print(f"[Economy]   Sim assist #{self._match_kda[2]}")

                # Variable delay between events (0.8-2.5s at 1x speed)
                await delay(0.8 + random.random() * 1.7)

            # ── Step 3: Match End ──
            print(f"[Economy] Step 3: Match ended")
            await self.on_match_end({})
            await delay(1.5)

            # ── Step 4: Settlement directly via settle_match() ──
            # (We skip on_match_result because in production that
            # triggers a real tracker.gg backfill. The sim drives
            # settle_match directly with the simulated outcome.)
            print(f"[Economy] Step 4: Match result — {outcome}")
            await self.settle_match(
                match_id=sim_match_id,
                god_name=god_name,
                outcome=outcome,
                kills=kills,
                deaths=deaths,
                assists=assists,
                source="sim",
                match_start_price=starting_price,
            )

            final_price = self._prices[god_name]
            total_change = ((final_price - starting_price) / starting_price) * 100

            print(f"\n[Economy] ═══ SIMULATION COMPLETE ═══")
            print(f"[Economy] {god_name}: {starting_price:.0f} → {final_price:.0f} hats "
                  f"({total_change:+.1f}%)")

            return {
                "god": god_name,
                "outcome": outcome,
                "kda": [kills, deaths, assists],
                "start_price": round(starting_price),
                "end_price": round(final_price),
                "change_pct": round(total_change, 1),
                "match_id": sim_match_id,
            }
        finally:
            self._sim_force_live = False

    # ══════════════════════════════════════════════════════════════════════
    #  TEST OVERLAY TRIGGERS (Control Panel)
    # ══════════════════════════════════════════════════════════════════════

    async def emit_test_dividend(self):
        """Emit a test dividend_paid event with sample data."""
        god = next(iter(self._prices), "Ymir")
        price = self._prices.get(god, 100)
        self._emit_overlay_event("dividend_paid", {
            "god_name": god,
            "rate": 0.05,
            "price": round(price),
            "total_hats": round(price * 0.05 * 3),
            "holders": 3,
        })
        print(f"[Economy] Test: dividend_paid for {god}")

    async def emit_test_leaderboard(self):
        """Emit a test leaderboard_update event with sample data."""
        gods = list(self._prices.keys())[:3] or ["Ymir", "Geb", "Sylvanus"]
        leaderboard = []
        for i, name in enumerate(["hatmaster", "viewer1", "viewer2", "viewer3", "viewer4"][:5]):
            value = 5000 - i * 800
            leaderboard.append({
                "username": name,
                "portfolio_value": value,
                "change_pct": round(random.uniform(-5, 15), 1),
                "rank_change": random.choice([-1, 0, 0, 1, 2]),
                "top_gods": gods[:2],
            })
        self._emit_overlay_event("leaderboard_update", {"leaderboard": leaderboard})
        print(f"[Economy] Test: leaderboard_update ({len(leaderboard)} entries)")

    async def emit_test_portfolio(self):
        """Emit a test portfolio_requested event with sample data."""
        gods = list(self._prices.keys())[:3] or ["Ymir"]
        holdings = []
        for god in gods:
            price = self._prices.get(god, 100)
            avg_cost = price * random.uniform(0.7, 1.1)
            shares = round(random.uniform(1, 10), 1)
            value = shares * price
            pnl = value - (shares * avg_cost)
            holdings.append({
                "god_name": god,
                "shares": shares,
                "avg_cost": round(avg_cost),
                "price": round(price),
                "value": round(value),
                "pnl": round(pnl),
                "pnl_pct": round((pnl / (shares * avg_cost)) * 100, 1) if avg_cost > 0 else 0,
            })
        total_value = sum(h["value"] for h in holdings)
        total_pnl = sum(h["pnl"] for h in holdings)
        pfp_url = await self._get_profile_image("hatmaster")
        self._emit_overlay_event("portfolio_requested", {
            "username": "hatmaster",
            "display_name": "Hatmaster",
            "profile_image_url": pfp_url,
            "holdings": holdings,
            "total_value": round(total_value),
            "total_pnl": round(total_pnl),
            "hat_balance": 10000,
        })
        print(f"[Economy] Test: portfolio_requested ({len(holdings)} holdings)")

    async def emit_test_tradefeed(self):
        """Emit a test trade_executed event with sample data."""
        god = next(iter(self._prices), "Ymir")
        price = self._prices.get(god, 100)
        trade_type = random.choice(["buy", "sell"])
        self._emit_overlay_event("trade_executed", {
            "type": trade_type,
            "username": "viewer1",
            "god": god,
            "god_name": god,
            "shares": round(random.uniform(1, 5), 1),
            "price": round(price),
            "total": round(price * random.uniform(1, 5)),
            "amount": round(price * random.uniform(1, 5)),
        })
        print(f"[Economy] Test: trade_executed ({trade_type} {god})")

    async def emit_test_match_end(self):
        """Emit a test match_end_economy event with sample data."""
        god = next(iter(self._prices), "Ymir")
        price = self._prices.get(god, 100)
        outcome = random.choice(["win", "loss"])
        change = random.uniform(-10, 15) if outcome == "win" else random.uniform(-15, -3)
        old_price = round(price / (1 + change / 100))
        self._emit_overlay_event("match_end_economy", {
            "god": god,
            "outcome": outcome,
            "kda": [random.randint(2, 10), random.randint(1, 8), random.randint(2, 12)],
            "old_price": old_price,
            "new_price": round(price),
            "change_pct": round(change, 1),
            "volatility_tier": "MEDIUM",
            "free_shares": {
                "god_name": god,
                "shares_each": 1,
                "viewer_count": 5,
                "share_value": round(price),
            },
            "games_played": self._games_played.get(god, 10),
        })
        print(f"[Economy] Test: match_end_economy ({god} {outcome})")

    async def emit_test_ticker(self):
        """Directly show the ticker overlay by sending it market data."""
        if not self.bot or not self.bot.web_server:
            return
        overlay_mgr = getattr(self.bot.web_server, "overlay", None)
        if overlay_mgr:
            await overlay_mgr._send("economy_ticker", "show", {})
            overlay_mgr._visible["economy_ticker"] = True
        print(f"[Economy] Test: ticker (direct show, {len(self._prices)} gods)")

    async def reload_prices(self):
        """Reload prices from the database (e.g. after running the seeder)."""
        if not self._db:
            return {"error": "Database not connected"}
        old_count = len(self._prices)
        await self._load_prices()
        new_count = len(self._prices)
        print(f"[Economy] Prices reloaded: {old_count} -> {new_count} gods")
        return {"gods_loaded": new_count}

    async def seed_prices(self, seed_data):
        """
        Seed initial god prices from historical data.

        seed_data format: {
            "Ymir": {"price": 300, "games": 22, "wins": 16, "losses": 6},
            "Loki": {"price": 30, "games": 11, "wins": 2, "losses": 9},
            ...
        }
        Used by tools/seed_economy.py. Idempotent via ON CONFLICT DO UPDATE.
        """
        from core.config import ECONOMY_STARTING_PRICE
        for god_name, info in seed_data.items():
            price = info.get("price", ECONOMY_STARTING_PRICE)
            games = info.get("games", 0)
            wins = info.get("wins", 0)
            losses = info.get("losses", 0)

            await self._db.execute("""
                INSERT INTO god_prices (god_name, price, games_played, total_wins, total_losses)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(god_name) DO UPDATE SET
                    price = excluded.price,
                    games_played = excluded.games_played,
                    total_wins = excluded.total_wins,
                    total_losses = excluded.total_losses,
                    updated_at = datetime('now')
            """, (god_name, price, games, wins, losses))

            await self._db.execute(
                "INSERT INTO price_history (god_name, price, event) VALUES (?, ?, 'seed')",
                (god_name, price)
            )

            self._prices[god_name] = price
            self._games_played[god_name] = games
            self._price_history[god_name] = [price]
            self._god_names[god_name.lower()] = god_name

        await self._db.commit()
        print(f"[Economy] Seeded {len(seed_data)} god prices")

