"""
God Stock Market Economy Plugin
================================
Stock-market-style economy where viewers invest hats (MixItUp currency) in
Smite 2 gods.  God "share" prices move based on Hatmaster's actual match
performance: wins/losses are the dominant factor, KDA modifies magnitude.

Architecture:
  - SQLite database (async via aiosqlite) for prices, portfolios, transactions
  - Hooks into SmitePlugin events (god_detected, match_end, match_result)
  - Hooks into KillDetector events (on_kill, on_death, on_assist) for live ticks
  - MixItUp Developer API for hat balance reads/writes
  - Emits overlay events via WebServer overlay manager

Chat commands:
  !buy [god] [amount]   — invest hats (amount in hats, receives shares)
  !sell [god] [amount]   — cash out (amount in hats worth, sells shares)
  !portfolio             — view holdings with current value and P&L
  !price [god]           — current price, recent trend, volatility tier
  !market / !stocks      — top movers, gainers/losers
  !dividend              — recent dividend payouts
"""

import asyncio
import aiohttp
import json
import math
import time
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

try:
    import aiosqlite
except ImportError:
    aiosqlite = None
    print("[Economy] WARNING: aiosqlite not installed. Run: pip install aiosqlite")

from core.config import (
    DATA_DIR, MIXITUP_API_BASE, TWITCH_CHANNEL, TWITCH_OWNER_ID,
    ECONOMY_DB_PATH, ECONOMY_STARTING_PRICE, ECONOMY_PRICE_FLOOR,
    ECONOMY_TRANSACTION_FEE, ECONOMY_POSITION_LIMIT, ECONOMY_DIVIDEND_RATE,
    ECONOMY_KILL_TICK, ECONOMY_DEATH_TICK, ECONOMY_ASSIST_TICK,
    ECONOMY_FREE_SHARE_COUNT, ECONOMY_CURRENCY_NAME,
)

# ── Volatility Multipliers (based on games played) ──────────────────────
VOLATILITY_TIERS = [
    (4,  2.0, "penny stock"),    # 1-4 games
    (10, 1.5, "mid-cap"),        # 5-10 games
    (20, 1.2, "large-cap"),      # 11-20 games
    (999999, 1.0, "blue chip"),  # 20+ games
]

# ── Base price change targets (win/loss + KDA quality) ──────────────────
# The formula produces a base_change_pct, then multiplied by volatility.
# Win base: +3% to +15% depending on KDA ratio.
# Loss base: -5% to -13% depending on KDA ratio.
WIN_BASE_MIN = 3.0    # Carried win (bad KDA)
WIN_BASE_MAX = 15.0   # Domination (great KDA)
LOSS_BASE_MIN = -5.0   # Close loss (decent KDA)
LOSS_BASE_MAX = -13.0  # Feeding (terrible KDA)

# KDA ratio thresholds for scaling (separate for wins and losses,
# because loss KDAs cluster in a tighter range than win KDAs)
WIN_KDA_LOW = 0.3      # Carried win: (1+1)/6 = 0.33
WIN_KDA_HIGH = 5.0     # Domination:  (12+3)/1 = 15 (capped)
LOSS_KDA_LOW = 0.1     # Feeding:     (1+0.5)/10 = 0.15
LOSS_KDA_HIGH = 1.2    # Close loss:  (5+2)/6 = 1.17

# ── Price history sparkline length ──────────────────────────────────────
SPARKLINE_LENGTH = 20   # Keep last N price points for sparklines

# ── Cooldowns ───────────────────────────────────────────────────────────
TRADE_COOLDOWN = 3.0    # Seconds between trades per user
PORTFOLIO_COOLDOWN = 10.0
PRICE_COOLDOWN = 5.0
MARKET_COOLDOWN = 15.0
DIVIDEND_COOLDOWN = 10.0

# ── VGS Voice Line Triggers (economy → voiceline overlay) ─────────────
# Maps economy events to SMITE VGS search patterns.
# God voiceline files use inconsistent naming (Ymir_Emote_R, Bellona_VER,
# Danzaburou_vox_vgs_emote_r), so we search case-insensitively with
# multiple possible suffixes per trigger.
VOICELINE_DIR = DATA_DIR / "smite_voicelines"
VGS_TRIGGERS = {
    "dividend":   ["emote_r",  "ver"],      # "You Rock!"
    "win":        ["emote_a",  "vea"],      # "Awesome!"
    "loss":       ["other_v_t", "vvgt"],    # "That's too bad"
    "big_spike":  ["emote_w",  "vew"],      # "Woohoo!"
    "big_crash":  ["help",     "vhh"],      # "Help!"
}


class EconomyPlugin:
    """God stock market economy for HatmasBot."""

    def __init__(self, token_manager=None):
        self.bot = None
        self.token_manager = token_manager
        self.session = None        # aiohttp for MixItUp API
        self._currency_id = None
        self._connected = False    # MixItUp connection status
        self._db = None            # aiosqlite connection

        # In-memory price cache (loaded from DB on startup)
        self._prices: Dict[str, float] = {}          # {god_name: current_price}
        self._games_played: Dict[str, int] = {}      # {god_name: total_games}
        self._price_history: Dict[str, List[float]] = {}  # {god_name: [prices]}
        self._session_changes: Dict[str, float] = {} # {god_name: session_pct_change}

        # Live match state
        self._match_active = False
        self._match_god: Optional[str] = None
        self._match_start_price: float = 0.0
        self._match_kda = [0, 0, 0]  # [kills, deaths, assists] during match

        # Dividend tracking for this session
        self._last_dividend: Optional[Dict] = None

        # Cooldowns
        self._cooldowns: Dict[str, Dict[str, float]] = {}  # {username: {cmd: timestamp}}

        # God name normalization cache
        self._god_names: Dict[str, str] = {}  # {lowercase: ProperCase}

    # ══════════════════════════════════════════════════════════════════════
    #  PLUGIN LIFECYCLE
    # ══════════════════════════════════════════════════════════════════════

    def setup(self, bot):
        self.bot = bot
        bot.register_command("buy", self.cmd_buy)
        bot.register_command("sell", self.cmd_sell)
        bot.register_command("portfolio", self.cmd_portfolio)
        bot.register_command("price", self.cmd_price)
        bot.register_command("market", self.cmd_market)
        bot.register_command("stocks", self.cmd_market)  # alias
        bot.register_command("dividend", self.cmd_dividend)

    async def on_ready(self):
        if aiosqlite is None:
            print("[Economy] Cannot start — aiosqlite not installed")
            return

        self.session = aiohttp.ClientSession()

        # Initialize database
        await self._init_db()

        # Load price cache from database
        await self._load_prices()

        # Connect to MixItUp
        await self._resolve_currency_id()

        # Build god name lookup from existing data
        await self._build_god_name_index()

        print(f"[Economy] Ready — {len(self._prices)} gods tracked, "
              f"MixItUp {'connected' if self._connected else 'disconnected'}")

    async def cleanup(self):
        """Persist state and close connections."""
        if self._db:
            await self._db.close()
            self._db = None
        if self.session:
            await self.session.close()
            self.session = None
        print("[Economy] Cleaned up")

    # ══════════════════════════════════════════════════════════════════════
    #  DATABASE
    # ══════════════════════════════════════════════════════════════════════

    async def _init_db(self):
        """Create SQLite database and tables."""
        self._db = await aiosqlite.connect(str(ECONOMY_DB_PATH))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")

        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS god_prices (
                god_name     TEXT PRIMARY KEY,
                price        REAL NOT NULL DEFAULT 100.0,
                games_played INTEGER NOT NULL DEFAULT 0,
                total_wins   INTEGER NOT NULL DEFAULT 0,
                total_losses INTEGER NOT NULL DEFAULT 0,
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                god_name  TEXT NOT NULL,
                price     REAL NOT NULL,
                event     TEXT NOT NULL DEFAULT 'update',
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (god_name) REFERENCES god_prices(god_name)
            );

            CREATE INDEX IF NOT EXISTS idx_price_history_god
                ON price_history(god_name, timestamp DESC);

            CREATE TABLE IF NOT EXISTS portfolios (
                username  TEXT NOT NULL,
                god_name  TEXT NOT NULL,
                shares    REAL NOT NULL DEFAULT 0,
                avg_cost  REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (username, god_name),
                FOREIGN KEY (god_name) REFERENCES god_prices(god_name)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                username  TEXT NOT NULL,
                god_name  TEXT NOT NULL,
                type      TEXT NOT NULL,  -- 'buy', 'sell', 'dividend', 'free_share'
                shares    REAL NOT NULL,
                price     REAL NOT NULL,
                total     REAL NOT NULL,
                fee       REAL NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_user
                ON transactions(username, timestamp DESC);

            CREATE TABLE IF NOT EXISTS dividends (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                god_name    TEXT NOT NULL,
                rate        REAL NOT NULL,
                price       REAL NOT NULL,
                total_hats  REAL NOT NULL,
                holders     INTEGER NOT NULL,
                timestamp   TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        await self._db.commit()
        print("[Economy] Database initialized")

    async def _load_prices(self):
        """Load all god prices into memory cache."""
        async with self._db.execute(
            "SELECT god_name, price, games_played FROM god_prices"
        ) as cursor:
            async for row in cursor:
                god_name, price, games = row
                self._prices[god_name] = price
                self._games_played[god_name] = games

        # Load recent price history for sparklines
        for god_name in self._prices:
            self._price_history[god_name] = await self._get_recent_prices(god_name)

    async def _get_recent_prices(self, god_name: str, limit: int = SPARKLINE_LENGTH) -> List[float]:
        """Get recent price points for sparklines."""
        prices = []
        async with self._db.execute(
            "SELECT price FROM price_history WHERE god_name = ? ORDER BY timestamp DESC LIMIT ?",
            (god_name, limit)
        ) as cursor:
            async for row in cursor:
                prices.append(row[0])
        prices.reverse()  # Chronological order
        return prices

    async def _update_price(self, god_name: str, new_price: float, event: str = "update"):
        """Update god price in DB and memory cache."""
        new_price = max(new_price, ECONOMY_PRICE_FLOOR)
        old_price = self._prices.get(god_name, ECONOMY_STARTING_PRICE)

        self._prices[god_name] = new_price

        # Track price history for sparklines
        if god_name not in self._price_history:
            self._price_history[god_name] = []
        self._price_history[god_name].append(new_price)
        if len(self._price_history[god_name]) > SPARKLINE_LENGTH:
            self._price_history[god_name] = self._price_history[god_name][-SPARKLINE_LENGTH:]

        # Track session change
        if god_name not in self._session_changes:
            self._session_changes[god_name] = 0.0
        if old_price > 0:
            pct = ((new_price - old_price) / old_price) * 100
            self._session_changes[god_name] += pct

        # Persist to DB
        await self._db.execute("""
            INSERT INTO god_prices (god_name, price, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(god_name) DO UPDATE SET
                price = excluded.price,
                updated_at = excluded.updated_at
        """, (god_name, new_price))

        await self._db.execute(
            "INSERT INTO price_history (god_name, price, event) VALUES (?, ?, ?)",
            (god_name, new_price, event)
        )
        await self._db.commit()

        return old_price, new_price

    async def _ensure_god_exists(self, god_name: str):
        """Create a god price entry if it doesn't exist."""
        if god_name not in self._prices:
            self._prices[god_name] = ECONOMY_STARTING_PRICE
            self._games_played[god_name] = 0
            self._price_history[god_name] = [ECONOMY_STARTING_PRICE]
            await self._db.execute(
                "INSERT OR IGNORE INTO god_prices (god_name, price) VALUES (?, ?)",
                (god_name, ECONOMY_STARTING_PRICE)
            )
            await self._db.execute(
                "INSERT INTO price_history (god_name, price, event) VALUES (?, ?, 'ipo')",
                (god_name, ECONOMY_STARTING_PRICE)
            )
            await self._db.commit()

    # ══════════════════════════════════════════════════════════════════════
    #  GOD NAME RESOLUTION
    # ══════════════════════════════════════════════════════════════════════

    async def _build_god_name_index(self):
        """Build lookup table from lowercase → ProperCase god names."""
        async with self._db.execute("SELECT god_name FROM god_prices") as cursor:
            async for row in cursor:
                name = row[0]
                self._god_names[name.lower()] = name

        # Also scan the Custom God Icons folder for all known gods
        icons_dir = Path(__file__).parent.parent / "Custom God Icons"
        if icons_dir.exists():
            for f in icons_dir.iterdir():
                if f.suffix == ".png" and not f.stem.startswith("."):
                    # Skip skin variants (contain spaces + numbers like "Ymir 2")
                    # Keep only base god names
                    name = f.stem
                    lower = name.lower()
                    if lower not in self._god_names:
                        self._god_names[lower] = name

    def _resolve_god_name(self, user_input: str) -> Optional[str]:
        """Resolve user input to a proper god name. Supports partial matching."""
        lower = user_input.lower().strip()
        if not lower:
            return None

        # Exact match
        if lower in self._god_names:
            return self._god_names[lower]

        # Partial match (prefix)
        matches = [v for k, v in self._god_names.items() if k.startswith(lower)]
        if len(matches) == 1:
            return matches[0]

        # Partial match (contains)
        if not matches:
            matches = [v for k, v in self._god_names.items() if lower in k]
            if len(matches) == 1:
                return matches[0]

        return None

    # ══════════════════════════════════════════════════════════════════════
    #  VOLATILITY & PRICE CALCULATION
    # ══════════════════════════════════════════════════════════════════════

    def _get_volatility(self, god_name: str) -> Tuple[float, str]:
        """Get volatility multiplier and tier name for a god."""
        games = self._games_played.get(god_name, 0)
        for max_games, multiplier, tier_name in VOLATILITY_TIERS:
            if games <= max_games:
                return multiplier, tier_name
        return 1.0, "blue chip"

    def _calculate_match_end_change(self, outcome: str, kills: int, deaths: int,
                                      assists: int, god_name: str) -> float:
        """
        Calculate the percentage price change at match end.

        Formula:
          1. Compute KDA ratio: (kills + assists*0.5) / max(deaths, 1)
          2. Map ratio to base change range (win or loss)
          3. Multiply by volatility
        """
        # KDA quality ratio
        kda_ratio = (kills + assists * 0.5) / max(deaths, 1)

        if outcome == "win":
            # Wins: higher ratio = bigger gain
            t = (kda_ratio - WIN_KDA_LOW) / (WIN_KDA_HIGH - WIN_KDA_LOW)
            t = max(0.0, min(1.0, t))
            base_change = WIN_BASE_MIN + t * (WIN_BASE_MAX - WIN_BASE_MIN)
        else:
            # Losses: higher ratio = milder loss (close loss vs feeding)
            t = (kda_ratio - LOSS_KDA_LOW) / (LOSS_KDA_HIGH - LOSS_KDA_LOW)
            t = max(0.0, min(1.0, t))
            base_change = LOSS_BASE_MAX + t * (LOSS_BASE_MIN - LOSS_BASE_MAX)

        # Apply volatility multiplier
        vol_mult, _ = self._get_volatility(god_name)
        final_change = base_change * vol_mult

        return final_change

    # ══════════════════════════════════════════════════════════════════════
    #  MIXITUP CURRENCY API
    # ══════════════════════════════════════════════════════════════════════

    async def _miu_get(self, path):
        try:
            async with self.session.get(f"{MIXITUP_API_BASE}{path}") as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except aiohttp.ClientConnectorError:
            return None
        except Exception as e:
            print(f"[Economy] MixItUp GET error: {e}")
            return None

    async def _miu_patch(self, path, data):
        try:
            async with self.session.patch(
                f"{MIXITUP_API_BASE}{path}",
                json=data,
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            print(f"[Economy] MixItUp PATCH error: {e}")
            return None

    async def _resolve_currency_id(self):
        currencies = await self._miu_get("/currency")
        if not currencies:
            print(f"[Economy] Could not connect to MixItUp API")
            return
        for curr in currencies:
            if curr.get("Name", "").lower() == ECONOMY_CURRENCY_NAME.lower():
                self._currency_id = curr["ID"]
                self._connected = True
                print(f"[Economy] MixItUp connected — currency: {self._currency_id}")
                return
        print(f"[Economy] Currency '{ECONOMY_CURRENCY_NAME}' not found!")

    async def _get_user_id(self, twitch_username: str) -> Optional[str]:
        data = await self._miu_get(f"/users/Twitch/{twitch_username}")
        if data and "User" in data:
            return data["User"]["ID"]
        return None

    async def _get_balance(self, twitch_username: str) -> Optional[int]:
        if not self._connected:
            return None
        user_id = await self._get_user_id(twitch_username)
        if not user_id:
            return None
        data = await self._miu_get(f"/currency/{self._currency_id}/{user_id}")
        if data:
            return data.get("Amount", 0)
        return 0

    async def _adjust_balance(self, twitch_username: str, amount: int) -> bool:
        """Add or subtract hats. Positive = add, negative = subtract."""
        if not self._connected:
            return False
        user_id = await self._get_user_id(twitch_username)
        if not user_id:
            return False
        result = await self._miu_patch(
            f"/currency/{self._currency_id}/{user_id}",
            {"Amount": amount}
        )
        return result is not None

    # ══════════════════════════════════════════════════════════════════════
    #  TRADING ENGINE
    # ══════════════════════════════════════════════════════════════════════

    async def execute_buy(self, username: str, god_name: str,
                          hat_amount: int) -> Dict[str, Any]:
        """
        Buy shares of a god with hats.

        Returns dict with: success, shares, price, fee, total_cost, error
        """
        god_name = self._resolve_god_name(god_name)
        if not god_name:
            return {"success": False, "error": "Unknown god"}

        await self._ensure_god_exists(god_name)
        price = self._prices[god_name]

        if hat_amount < 1:
            return {"success": False, "error": "Minimum investment is 1 hat"}

        # Calculate fee and shares
        fee = int(math.ceil(hat_amount * ECONOMY_TRANSACTION_FEE))
        net_amount = hat_amount - fee
        shares = net_amount / price

        if shares <= 0:
            return {"success": False, "error": "Amount too small after fee"}

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

        # Record transaction
        await self._db.execute("""
            INSERT INTO transactions (username, god_name, type, shares, price, total, fee)
            VALUES (?, ?, 'buy', ?, ?, ?, ?)
        """, (username, god_name, shares, price, hat_amount, fee))
        await self._db.commit()

        # Emit overlay event
        self._emit_trade_event("buy", username, god_name, shares, price, hat_amount, fee)

        return {
            "success": True,
            "shares": shares,
            "price": price,
            "fee": fee,
            "total_cost": hat_amount,
            "god_name": god_name,
        }

    async def execute_sell(self, username: str, god_name: str,
                           hat_amount: int) -> Dict[str, Any]:
        """
        Sell shares of a god for hats.

        hat_amount is the desired hat value to sell. Sells the equivalent shares.
        Returns dict with: success, shares, price, fee, net_received, error
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

        gross_value = shares_to_sell * price
        fee = int(math.ceil(gross_value * ECONOMY_TRANSACTION_FEE))
        net_received = int(gross_value - fee)

        if net_received <= 0:
            return {"success": False, "error": "Amount too small after fee"}

        # Execute: add hats
        success = await self._adjust_balance(username, net_received)
        if not success:
            return {"success": False, "error": "Transaction failed"}

        # Update portfolio
        await self._remove_shares(username, god_name, shares_to_sell)

        # Record transaction
        await self._db.execute("""
            INSERT INTO transactions (username, god_name, type, shares, price, total, fee)
            VALUES (?, ?, 'sell', ?, ?, ?, ?)
        """, (username, god_name, shares_to_sell, price, net_received, fee))
        await self._db.commit()

        # Emit overlay event
        self._emit_trade_event("sell", username, god_name, shares_to_sell, price, net_received, fee)

        return {
            "success": True,
            "shares": shares_to_sell,
            "price": price,
            "fee": fee,
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

    # ══════════════════════════════════════════════════════════════════════
    #  MATCH EVENT HANDLERS
    # ══════════════════════════════════════════════════════════════════════

    async def on_god_detected(self, god_info: Dict):
        """
        Fired when god portrait detection identifies the god.
        Pays dividends to all holders on FIRST detection only, starts live price tracking.
        Subsequent detections for the same match (e.g. tracker.gg confirmation) are ignored.
        """
        if not self._db or not god_info or not god_info.get("name"):
            return

        god_name = god_info["name"]

        # Only pay dividend and start tracking on first detection per match
        if self._match_active and self._match_god == god_name:
            print(f"[Economy] Ignoring duplicate god detection for {god_name}")
            return

        await self._ensure_god_exists(god_name)

        self._match_active = True
        self._match_god = god_name
        self._match_start_price = self._prices[god_name]
        self._match_kda = [0, 0, 0]

        print(f"[Economy] Match started with {god_name} (price: {self._match_start_price:.0f})")

        # Pay 5% dividend to all holders (first detection only)
        await self._pay_dividend(god_name)

        # Emit economy event for overlays
        self._emit_overlay_event("economy_god_detected", {
            "god": god_name,
            "price": self._prices[god_name],
            "volatility": self._get_volatility(god_name)[1],
        })

    async def on_kill(self, kill_type: str, count: int = 1):
        """Live price tick on kill during match."""
        if not self._match_active or not self._match_god:
            print(f"[Economy] on_kill ignored (match_active={self._match_active}, god={self._match_god})")
            return

        self._match_kda[0] += count
        god_name = self._match_god
        old_price = self._prices[god_name]
        # Apply tick once per kill
        new_price = old_price * ((1 + ECONOMY_KILL_TICK) ** count)
        await self._update_price(god_name, new_price, event="kill")
        print(f"[Economy] Kill x{count}! {god_name} price {old_price:.0f} → {new_price:.0f} (KDA: {self._match_kda[0]}/{self._match_kda[1]}/{self._match_kda[2]})")

        change_pct = ((new_price - self._match_start_price) / self._match_start_price) * 100

        self._emit_overlay_event("god_stock_update_kd", {
            "god": god_name,
            "price": round(new_price),
            "change_pct": round(change_pct, 1),
            "event": "kill",
            "kda": {"k": self._match_kda[0], "d": self._match_kda[1], "a": self._match_kda[2]},
            "history": self._price_history.get(god_name, [])[-10:],
        })

        # Big spike voiceline trigger (15%+)
        if change_pct >= 15:
            self._emit_overlay_event("economy_big_spike", {
                "god": god_name, "change_pct": round(change_pct, 1)
            })
            self._trigger_voiceline("big_spike", god_name)

    async def on_death(self, count: int = 1):
        """Live price tick on death during match."""
        if not self._match_active or not self._match_god:
            print(f"[Economy] on_death ignored (match_active={self._match_active}, god={self._match_god})")
            return

        self._match_kda[1] += count
        god_name = self._match_god
        old_price = self._prices[god_name]
        # Apply tick once per death
        new_price = old_price * ((1 + ECONOMY_DEATH_TICK) ** count)
        new_price = max(new_price, ECONOMY_PRICE_FLOOR)
        await self._update_price(god_name, new_price, event="death")

        change_pct = ((new_price - self._match_start_price) / self._match_start_price) * 100

        self._emit_overlay_event("god_stock_update_kd", {
            "god": god_name,
            "price": round(new_price),
            "change_pct": round(change_pct, 1),
            "event": "death",
            "kda": {"k": self._match_kda[0], "d": self._match_kda[1], "a": self._match_kda[2]},
            "history": self._price_history.get(god_name, [])[-10:],
        })

        # Big crash voiceline trigger (15%+ drop)
        if change_pct <= -15:
            self._emit_overlay_event("economy_big_crash", {
                "god": god_name, "change_pct": round(change_pct, 1)
            })
            self._trigger_voiceline("big_crash", god_name)

    async def on_assist(self, count: int = 1):
        """Live price tick on assist during match."""
        if not self._match_active or not self._match_god:
            return  # Assists are silent when no match active

        self._match_kda[2] += count
        god_name = self._match_god
        old_price = self._prices[god_name]
        # Apply tick once per assist
        new_price = old_price * ((1 + ECONOMY_ASSIST_TICK) ** count)
        await self._update_price(god_name, new_price, event="assist")

        change_pct = ((new_price - self._match_start_price) / self._match_start_price) * 100

        self._emit_overlay_event("god_stock_update", {
            "god": god_name,
            "price": round(new_price),
            "change_pct": round(change_pct, 1),
            "event": "assist",
            "kda": {"k": self._match_kda[0], "d": self._match_kda[1], "a": self._match_kda[2]},
            "history": self._price_history.get(god_name, [])[-10:],
        })

    async def on_match_end(self, data: Dict):
        """
        Match ended — stop live ticking.
        Actual settlement (W/L price change) happens in on_match_result.
        """
        if not self._match_active:
            return

        print(f"[Economy] Match ended for {self._match_god} "
              f"(KDA: {self._match_kda[0]}/{self._match_kda[1]}/{self._match_kda[2]})")

        # Don't clear match state yet — wait for result
        # Just stop responding to KDA ticks
        self._match_active = False

    async def on_match_result(self, data: Dict):
        """
        Win/loss determined — settle the final price.
        Called from SmitePlugin.record_result() via callback.
        """
        outcome = data.get("outcome")  # 'win' or 'loss'
        if not self._db or not outcome or not self._match_god:
            return

        god_name = self._match_god
        kills, deaths, assists = self._match_kda
        old_price = self._prices.get(god_name, ECONOMY_STARTING_PRICE)

        # Calculate the match-end price change
        change_pct = self._calculate_match_end_change(
            outcome, kills, deaths, assists, god_name
        )

        # The live ticks already moved the price during the match.
        # The settlement price is calculated from the MATCH START price,
        # replacing the live tick movements with the final formula result.
        settlement_price = self._match_start_price * (1 + change_pct / 100)
        settlement_price = max(settlement_price, ECONOMY_PRICE_FLOOR)

        await self._update_price(god_name, settlement_price, event=f"match_{outcome}")

        # Update games played and win/loss record
        self._games_played[god_name] = self._games_played.get(god_name, 0) + 1
        win_col = "total_wins" if outcome == "win" else "total_losses"
        await self._db.execute(f"""
            UPDATE god_prices SET games_played = games_played + 1,
                {win_col} = {win_col} + 1
            WHERE god_name = ?
        """, (god_name,))
        await self._db.commit()

        # Distribute free shares to all viewers
        free_share_info = await self._distribute_free_shares(god_name)

        actual_change_pct = ((settlement_price - self._match_start_price) / self._match_start_price) * 100

        print(f"[Economy] {god_name} {outcome.upper()}: {self._match_start_price:.0f} → "
              f"{settlement_price:.0f} ({actual_change_pct:+.1f}%) "
              f"[KDA {kills}/{deaths}/{assists}]")

        # Emit match end economy overlay event
        vol_mult, vol_tier = self._get_volatility(god_name)
        self._emit_overlay_event("match_end_economy", {
            "god": god_name,
            "outcome": outcome,
            "kda": [kills, deaths, assists],
            "old_price": round(self._match_start_price),
            "new_price": round(settlement_price),
            "change_pct": round(actual_change_pct, 1),
            "volatility_tier": vol_tier,
            "free_shares": free_share_info,
            "games_played": self._games_played[god_name],
        })

        # VGS: "Awesome!" on win, "That's too bad" on loss
        self._trigger_voiceline(outcome, god_name)

        # Emit leaderboard update
        await self._emit_leaderboard()

        # Clear match state
        self._match_god = None
        self._match_start_price = 0.0
        self._match_kda = [0, 0, 0]

    # ══════════════════════════════════════════════════════════════════════
    #  DIVIDENDS
    # ══════════════════════════════════════════════════════════════════════

    async def _pay_dividend(self, god_name: str):
        """Pay 5% dividend to all holders of a god's shares."""
        price = self._prices.get(god_name, 0)
        if price <= 0:
            return

        dividend_per_share = price * ECONOMY_DIVIDEND_RATE
        holders = []
        total_hats = 0

        async with self._db.execute(
            "SELECT username, shares FROM portfolios WHERE god_name = ? AND shares > 0.001",
            (god_name,)
        ) as cursor:
            async for row in cursor:
                username, shares = row
                payout = int(shares * dividend_per_share)
                if payout > 0:
                    holders.append((username, payout))
                    total_hats += payout

        if not holders:
            print(f"[Economy] No holders for {god_name} dividend")
            return

        # Pay out dividends
        for username, payout in holders:
            await self._adjust_balance(username, payout)
            await self._db.execute("""
                INSERT INTO transactions (username, god_name, type, shares, price, total, fee)
                VALUES (?, ?, 'dividend', 0, ?, ?, 0)
            """, (username, god_name, price, payout))

        # Record dividend event
        await self._db.execute("""
            INSERT INTO dividends (god_name, rate, price, total_hats, holders)
            VALUES (?, ?, ?, ?, ?)
        """, (god_name, ECONOMY_DIVIDEND_RATE, price, total_hats, len(holders)))
        await self._db.commit()

        self._last_dividend = {
            "god_name": god_name,
            "rate": ECONOMY_DIVIDEND_RATE,
            "price": price,
            "per_share": dividend_per_share,
            "total_hats": total_hats,
            "holders": len(holders),
            "timestamp": datetime.now().isoformat(),
        }

        print(f"[Economy] Dividend: {god_name} — {len(holders)} holders, "
              f"{total_hats:,} hats distributed ({ECONOMY_DIVIDEND_RATE*100:.0f}%)")

        # Emit overlay event
        self._emit_overlay_event("dividend_paid", self._last_dividend)

        # VGS: "You Rock!" on dividend
        self._trigger_voiceline("dividend", god_name)

    # ══════════════════════════════════════════════════════════════════════
    #  TWITCH USER HELPERS
    # ══════════════════════════════════════════════════════════════════════

    async def _get_profile_image(self, username: str) -> Optional[str]:
        """Fetch a Twitch user's profile image URL via Helix API."""
        if not self.token_manager:
            return None
        try:
            headers = await self.token_manager.get_broadcaster_headers()
            url = f"https://api.twitch.tv/helix/users?login={username}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        users = data.get("data", [])
                        if users:
                            return users[0].get("profile_image_url")
                    elif resp.status == 401 and self.token_manager:
                        refreshed = await self.token_manager.handle_401("broadcaster")
                        if refreshed:
                            headers = await self.token_manager.get_broadcaster_headers()
                            async with session.get(url, headers=headers) as retry:
                                if retry.status == 200:
                                    data = await retry.json()
                                    users = data.get("data", [])
                                    if users:
                                        return users[0].get("profile_image_url")
        except Exception as e:
            print(f"[Economy] Failed to fetch profile image for {username}: {e}")
        return None

    # ══════════════════════════════════════════════════════════════════════
    #  FREE SHARE DISTRIBUTION
    # ══════════════════════════════════════════════════════════════════════

    async def _get_chatters(self) -> List[str]:
        """
        Get list of current chatters via the Twitch Helix API.
        Uses GET /helix/chat/chatters with pagination to get ALL chatters
        (including lurkers connected to chat), not just those who typed.
        Falls back to TwitchIO channel.chatters if the API call fails.
        """
        # Try Helix API first (requires moderator:read:chatters scope)
        if self.token_manager:
            try:
                headers = await self.token_manager.get_broadcaster_headers()
                chatters = []
                cursor = None
                base_url = (
                    f"https://api.twitch.tv/helix/chat/chatters"
                    f"?broadcaster_id={TWITCH_OWNER_ID}"
                    f"&moderator_id={TWITCH_OWNER_ID}"
                    f"&first=1000"
                )

                async with aiohttp.ClientSession() as session:
                    while True:
                        url = base_url
                        if cursor:
                            url += f"&after={cursor}"

                        async with session.get(url, headers=headers) as resp:
                            if resp.status == 401 and self.token_manager:
                                # Token expired — refresh and retry once
                                refreshed = await self.token_manager.handle_401("broadcaster")
                                if refreshed:
                                    headers = await self.token_manager.get_broadcaster_headers()
                                    async with session.get(url, headers=headers) as retry_resp:
                                        if retry_resp.status == 200:
                                            data = await retry_resp.json()
                                        else:
                                            print(f"[Economy] Helix chatters retry failed: {retry_resp.status}")
                                            break
                                else:
                                    break
                            elif resp.status == 200:
                                data = await resp.json()
                            else:
                                body = await resp.text()
                                print(f"[Economy] Helix chatters API error: {resp.status} {body}")
                                break

                            for user in data.get("data", []):
                                name = user.get("user_login", "").lower()
                                if name and name != "hatmasbot":
                                    chatters.append(name)

                            # Check for more pages
                            cursor = data.get("pagination", {}).get("cursor")
                            if not cursor:
                                break

                if chatters:
                    print(f"[Economy] Helix chatters API: {len(chatters)} users in chat")
                    return chatters

            except Exception as e:
                print(f"[Economy] Helix chatters API failed, falling back to TwitchIO: {e}")

        # Fallback: TwitchIO channel.chatters (IRC-based, unreliable for large channels)
        if not self.bot:
            return []
        try:
            channel = self.bot.get_channel(TWITCH_CHANNEL)
            if channel and channel.chatters:
                names = [c.name for c in channel.chatters
                         if c.name.lower() != "hatmasbot"]
                print(f"[Economy] TwitchIO fallback chatters: {len(names)} users")
                return names
        except Exception as e:
            print(f"[Economy] Error getting chatters: {e}")
        return []

    async def _distribute_free_shares(self, god_name: str) -> Dict:
        """
        Give 1 free share to all current viewers (chatters) at match end.
        Only people currently in chat get free shares — not offline portfolio holders.
        """
        # Get live chatters from Twitch (Helix API with TwitchIO fallback)
        viewers = set(await self._get_chatters())

        price = self._prices.get(god_name, ECONOMY_STARTING_PRICE)

        for username in viewers:
            await self._add_shares(username, god_name, ECONOMY_FREE_SHARE_COUNT, 0)  # Cost basis 0 for free shares
            await self._db.execute("""
                INSERT INTO transactions (username, god_name, type, shares, price, total, fee)
                VALUES (?, ?, 'free_share', ?, ?, 0, 0)
            """, (username, god_name, ECONOMY_FREE_SHARE_COUNT, price))

        await self._db.commit()

        info = {
            "god_name": god_name,
            "shares_each": ECONOMY_FREE_SHARE_COUNT,
            "viewer_count": len(viewers),
            "share_value": round(price),
        }
        print(f"[Economy] Free shares: {len(viewers)} chatters "
              f"each got {ECONOMY_FREE_SHARE_COUNT} share(s) of {god_name}")
        return info

    # ══════════════════════════════════════════════════════════════════════
    #  MATCH SIMULATION (for testing the full overlay pipeline)
    # ══════════════════════════════════════════════════════════════════════

    async def simulate_game(self, god_name: str, outcome: str = "win",
                            kills: int = 7, deaths: int = 3, assists: int = 4,
                            speed: float = 1.0, force: bool = False):
        """
        Simulate a full match lifecycle for testing overlays + economy.

        Fires all events in order with realistic delays:
        1. God detected → dividend → ticker/match_live overlays appear
        2. Kill/death/assist ticks → live price movement + flash effects
        3. Match end → stop ticks
        4. Match result → settlement, free shares, voicelines
        5. Leaderboard update

        speed: multiplier for delay durations (0.5 = double speed, 2.0 = slow)
        force: if True, reset any stale match state before simulating
        """
        if not self._db:
            print("[Economy] Cannot simulate — database not initialized")
            return {"error": "Economy not ready (database not initialized)"}

        if self._match_active:
            if force:
                print("[Economy] Force-resetting stale match state for simulation")
                self._match_active = False
                self._match_god = None
                self._match_kda = [0, 0, 0]
            else:
                print("[Economy] Cannot simulate — match already in progress (use force=True to override)")
                return {"error": "Match already in progress"}

        print(f"\n[Economy] ═══ SIMULATING GAME: {god_name} ═══")
        print(f"[Economy] Outcome: {outcome} | KDA: {kills}/{deaths}/{assists} | Speed: {speed}x")

        delay = lambda secs: asyncio.sleep(secs * speed)

        # Ensure god exists
        await self._ensure_god_exists(god_name)
        starting_price = self._prices[god_name]

        # ── Step 1: God Detected (fires dividend, starts match) ──
        print(f"[Economy] Step 1: God detected — {god_name}")
        await self.on_god_detected({"name": god_name})
        await delay(2.0)

        # ── Step 2: Simulate KDA events with realistic timing ──
        # Interleave kills, deaths, assists in a plausible order
        events = (
            [("kill", "player_kill")] * kills +
            [("death", None)] * deaths +
            [("assist", None)] * assists
        )
        random.shuffle(events)

        for i, (event_type, kill_type) in enumerate(events):
            if event_type == "kill":
                await self.on_kill(kill_type or "player_kill")
                print(f"[Economy]   Kill #{self._match_kda[0]} → "
                      f"{self._prices[god_name]:.0f} hats")
            elif event_type == "death":
                await self.on_death()
                print(f"[Economy]   Death #{self._match_kda[1]} → "
                      f"{self._prices[god_name]:.0f} hats")
            elif event_type == "assist":
                await self.on_assist()
                print(f"[Economy]   Assist #{self._match_kda[2]} → "
                      f"{self._prices[god_name]:.0f} hats")

            # Variable delay between events (0.8-2.5s at 1x speed)
            await delay(0.8 + random.random() * 1.7)

        # ── Step 3: Match End ──
        print(f"[Economy] Step 3: Match ended")
        await self.on_match_end({})
        await delay(1.5)

        # ── Step 4: Match Result (settlement) ──
        print(f"[Economy] Step 4: Match result — {outcome}")
        await self.on_match_result({
            "outcome": outcome,
            "god": god_name,
            "stats": {"kills": kills, "deaths": deaths, "assists": assists},
            "record": None,
        })

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
        }

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
        print(f"[Economy] Prices reloaded: {old_count} → {new_count} gods")
        return {"gods_loaded": new_count}

    # ══════════════════════════════════════════════════════════════════════
    #  OVERLAY EVENTS
    # ══════════════════════════════════════════════════════════════════════

    def _emit_overlay_event(self, event_name: str, data: Dict):
        """Emit an event to the overlay manager."""
        if not self.bot or not self.bot.web_server:
            return
        overlay_mgr = getattr(self.bot.web_server, "overlay", None)
        if overlay_mgr:
            asyncio.create_task(overlay_mgr.emit(event_name, data))

    def _trigger_voiceline(self, trigger_key: str, god_name: Optional[str] = None):
        """
        Play a VGS voice line through the voiceline overlay.

        trigger_key: one of 'dividend', 'win', 'loss', 'big_spike', 'big_crash'
        god_name:    god display name (e.g. 'Ymir'). Falls back to _match_god.

        NOTE: Disabled for now — voiceline file naming is too inconsistent
        across gods to reliably match the right files. Re-enable when a
        proper mapping is built.
        """
        return  # Disabled — voiceline naming too inconsistent
        suffixes = VGS_TRIGGERS.get(trigger_key)
        if not suffixes:
            return

        god = god_name or self._match_god
        if not god:
            return

        god_slug = god.lower().replace(" ", "_").replace("'", "")
        vgs_dir = VOICELINE_DIR / god_slug / "vgs"
        if not vgs_dir.exists():
            print(f"[Economy] No VGS dir for {god_slug}")
            return

        # God voicelines have wildly inconsistent naming across gods:
        #   Ymir_Emote_R.ogg, AthenaV2_vox_vgs_emote_r.ogg,
        #   Agni_VGS_Emote_R.ogg, Bellona_VER.ogg
        # Search case-insensitively for any file ending in _{suffix}.ogg
        matches = []
        all_files = list(vgs_dir.iterdir())
        for suffix in suffixes:
            for f in all_files:
                # Case-insensitive match: filename ends with _{suffix}.ogg
                name_lower = f.name.lower()
                if name_lower.endswith(f"_{suffix.lower()}.ogg") or name_lower.endswith(f"_{suffix.lower()}_1.ogg"):
                    matches.append(f)
            if matches:
                break  # Use first suffix that has results

        if not matches:
            print(f"[Economy] No VGS file for {trigger_key} ({suffixes}) for {god_slug}")
            return

        chosen = random.choice(matches)
        audio_url = f"/api/voiceline_audio/{god_slug}/vgs/{chosen.name}"

        god_display = god_slug.replace("_", " ").title()
        event = {
            "type": f"economy_{trigger_key}",
            "god": god_display,
            "user": "HatmasBot",
            "audio_url": audio_url,
            "video_url": None,
            "timestamp": time.time(),
        }

        web_server = getattr(self.bot, "web_server", None)
        if web_server and hasattr(web_server, "trigger_voiceline_event"):
            web_server.trigger_voiceline_event(event)
            print(f"[Economy] VGS triggered: {trigger_key} → {chosen.name}")

    def _emit_trade_event(self, trade_type: str, username: str, god_name: str,
                          shares: float, price: float, total: float, fee: float):
        """Emit a trade event to the trade feed overlay."""
        self._emit_overlay_event("trade_executed", {
            "type": trade_type,
            "username": username,
            "god": god_name,
            "shares": round(shares, 2),
            "price": round(price),
            "total": round(total),
            "fee": round(fee),
            "timestamp": datetime.now().isoformat(),
        })

    async def _emit_leaderboard(self):
        """Emit leaderboard data to the overlay."""
        leaderboard = []
        async with self._db.execute("""
            SELECT p.username, SUM(p.shares * gp.price) as portfolio_value
            FROM portfolios p
            JOIN god_prices gp ON p.god_name = gp.god_name
            WHERE p.shares > 0.001
            GROUP BY p.username
            ORDER BY portfolio_value DESC
            LIMIT 10
        """) as cursor:
            rank = 1
            async for row in cursor:
                username, value = row
                leaderboard.append({
                    "rank": rank,
                    "username": username,
                    "portfolio_value": round(value),
                })
                rank += 1

        self._emit_overlay_event("leaderboard_update", {"leaderboard": leaderboard})

    # ══════════════════════════════════════════════════════════════════════
    #  COOLDOWN HELPER
    # ══════════════════════════════════════════════════════════════════════

    def _check_cooldown(self, username: str, command: str, cooldown: float) -> Optional[int]:
        """Check if a command is on cooldown. Returns remaining seconds or None."""
        now = time.time()
        user_cds = self._cooldowns.setdefault(username, {})
        last_use = user_cds.get(command, 0)
        if now - last_use < cooldown:
            return int(cooldown - (now - last_use))
        user_cds[command] = now
        return None

    # ══════════════════════════════════════════════════════════════════════
    #  CHAT COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    async def cmd_buy(self, message, args, whisper=False):
        """!buy [god] [amount] — Buy shares of a god with hats."""
        if not self.bot.is_feature_enabled("economy"):
            return
        if not self._db:
            await self.bot.send_reply(message, "Economy is still loading. Try again in a moment.", whisper)
            return

        username = message.chatter.name.lower()
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
            hat_amount = int(holding["shares"] * self._prices.get(god_name, 0))
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
            parts.append(f"📈 {g_str}")
        if losers:
            l_str = " ".join(f"{g} {c:.1f}%" for g, c in losers)
            parts.append(f"📉 {l_str}")
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

    # ══════════════════════════════════════════════════════════════════════
    #  WEBSERVER API ENDPOINTS
    # ══════════════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════════════
    #  SEED DATA (for testing / initial setup)
    # ══════════════════════════════════════════════════════════════════════

    async def seed_prices(self, seed_data: Dict[str, Dict]):
        """
        Seed initial god prices from historical data.

        seed_data format: {
            "Ymir": {"price": 300, "games": 22, "wins": 16, "losses": 6},
            "Loki": {"price": 30, "games": 11, "wins": 2, "losses": 9},
            ...
        }
        """
        for god_name, data in seed_data.items():
            price = data.get("price", ECONOMY_STARTING_PRICE)
            games = data.get("games", 0)
            wins = data.get("wins", 0)
            losses = data.get("losses", 0)

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
