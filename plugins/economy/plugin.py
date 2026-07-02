"""
plugins/economy/plugin.py
=========================
The actual EconomyPlugin class — the only thing main.py imports
from this package.

Responsibility split: this file owns

  * `__init__` (instance state initialization)
  * `setup`    (chat-command registration + schema-callback registration)
  * `on_ready` (one-time async startup: connect MixItUp, load prices,
               build god-name index, kick off backfill loop)
  * `_run_periodic_backfill` (the long-lived backfill background task)
  * `cleanup`  (release per-plugin resources; the shared DB connection
               is closed by main.py)

All other behavior comes from the per-concern mixins in this package.
The mixin order in the inheritance list below determines MRO; in
practice no two mixins define the same method name, so the order is
documentation, not behavior.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

import aiohttp

from core import db as _shared_db

from .api import _APIMixin
from .commands import _CommandsMixin
from .db import _DBMixin
from .dividends import _DividendsMixin
from .fair_value import _FairValueMixin
from .god_names import _GodNamesMixin
from .helpers import _HelpersMixin
from .match import _MatchMixin
from .mixitup import _MixItUpMixin
from .overlays import _OverlaysMixin
from .testing import _TestingMixin
from .ticking import _TickingMixin
from .trading import _TradingMixin


class EconomyPlugin(
    # Foundational mixins first — DB schema/cache + god-name lookup +
    # MixItUp client + fair-value math. Everything else builds on these.
    _DBMixin,
    _GodNamesMixin,
    _FairValueMixin,
    _MixItUpMixin,
    # Trading & portfolio CRUD that the higher-level handlers need.
    _TradingMixin,
    # Overlay + helper functions used by both the live-match path and
    # the periodic backfill.
    _OverlaysMixin,
    _HelpersMixin,
    # Live-match concerns: dividend payouts (on god detection), price
    # ticks (per-kill/death/assist), match settlement (on win/loss).
    _DividendsMixin,
    _TickingMixin,
    _MatchMixin,
    # Operator-facing surface: chat commands, dashboard test triggers,
    # JSON API endpoints. These all sit on top of everything above.
    _CommandsMixin,
    _TestingMixin,
    _APIMixin,
):
    """God stock market economy for HatmasBot."""

    def __init__(self, token_manager=None):
        self.bot = None
        self.token_manager = token_manager
        self.session = None        # aiohttp for MixItUp API
        self._currency_id = None
        self._connected = False    # MixItUp connection status
        self._db = None            # aiosqlite connection (set by _init_schema)

        # In-memory price cache (loaded from DB on startup)
        self._prices: Dict[str, float] = {}          # {god_name: current_price}
        self._games_played: Dict[str, int] = {}      # {god_name: total_games}
        self._price_history: Dict[str, List[float]] = {}  # {god_name: [prices]}
        self._session_changes: Dict[str, float] = {} # {god_name: session_pct_change}

        # Live match state. _match_active flips True only when
        # tracker.gg has CONFIRMED a real match (via on_match_confirmed
        # in match.py) — never on portrait-only signals. Jungle
        # practice / custom games leave _match_active False, so the
        # cosmetic tick handlers in ticking.py skip overlay emission
        # in those modes.
        self._match_active = False
        self._match_god: Optional[str] = None
        self._match_id: Optional[str] = None      # tracker.gg match_id for the current match
        self._match_start_price: float = 0.0
        self._match_kda = [0, 0, 0]               # cosmetic KDA counter for overlays

        # Simulator escape hatch — when True, _is_broadcaster_live()
        # returns True regardless of stream-status state, so the
        # !test sim path fires dividends + free shares end-to-end.
        # Set by simulate_game() before driving a synthetic match.
        self._sim_force_live = False

        # Dividend tracking for this session
        self._last_dividend: Optional[Dict] = None

        # Cooldowns
        self._cooldowns: Dict[str, Dict[str, float]] = {}  # {username: {cmd: timestamp}}

        # God name normalization cache
        self._god_names: Dict[str, str] = {}  # {lowercase: ProperCase}

        # Background task handle for the periodic match backfill.
        # Cancelled in cleanup().
        self._backfill_task: Optional[asyncio.Task] = None

    # ══════════════════════════════════════════════════════════════════════
    #  PLUGIN LIFECYCLE
    # ══════════════════════════════════════════════════════════════════════

    def setup(self, bot):
        self.bot = bot
        bot.register_command("buy", self.cmd_buy,
                             description="Buy shares of a god with Hats", identity=True, plugin="economy")
        bot.register_command("sell", self.cmd_sell,
                             description="Sell shares for Hats", identity=True, plugin="economy")
        bot.register_command("portfolio", self.cmd_portfolio,
                             description="Your holdings, P&L, and net worth", identity=True, plugin="economy")
        bot.register_command("price", self.cmd_price,
                             description="Current price, trend, and volatility tier for a god", platforms=("twitch", "discord"), plugin="economy")
        bot.register_command("market", self.cmd_market,
                             description="Top movers in the Hatmas Market", platforms=("twitch", "discord"), plugin="economy")
        bot.register_command("stocks", self.cmd_market,
                             description="Top movers in the Hatmas Market (alias)", platforms=("twitch", "discord"), plugin="economy")  # alias
        bot.register_command("dividend", self.cmd_dividend,
                             description="Most recent dividend payout info", platforms=("twitch", "discord"), plugin="economy")

        # Register our schema callback with the shared DB. main.py calls
        # core.db.init_db() between plugin setup and bot.start(), so by
        # the time on_ready runs every table the economy needs already
        # exists. We're the schema OWNER — youtube_rewards, public
        # webserver, and the various tools all consume tables we create.
        if _shared_db.is_available():
            _shared_db.register_schema(self._init_schema)

    async def on_ready(self):
        if not _shared_db.is_available():
            print("[Economy] Cannot start — aiosqlite not installed")
            return

        self.session = aiohttp.ClientSession()

        # Schema callback (registered in setup) already ran inside
        # core.db.init_db(). All we need to do here is grab the shared
        # connection — the schema callback also stored it on self._db
        # for us so existing methods that reference self._db keep
        # working unchanged. Defensive get_db() in case init_db was
        # somehow skipped.
        if self._db is None:
            self._db = await _shared_db.get_db()
        if self._db is None:
            print("[Economy] DB unavailable — init_db may not have run")
            return

        # Load price cache from database
        await self._load_prices()

        # Connect to MixItUp
        await self._resolve_currency_id()

        # Build god name lookup from existing data
        await self._build_god_name_index()

        print(f"[Economy] Ready — {len(self._prices)} gods tracked, "
              f"MixItUp {'connected' if self._connected else 'disconnected'}")

        # Schedule the periodic match backfill. Initial wait lets the
        # smite plugin finish its on_ready and connect to tracker.gg,
        # then we cycle every SMITE2_BACKFILL_INTERVAL seconds asking
        # "any new matches?" — picks up matches that finished without
        # the broadcaster manually resolving the Twitch prediction,
        # without requiring a bot restart.
        self._backfill_task = asyncio.create_task(
            self._run_periodic_backfill())

    async def _run_periodic_backfill(self):
        """
        Background loop: bootstrap on first run, then poll the match
        listing every SMITE2_BACKFILL_INTERVAL seconds. Each cycle is
        idempotent thanks to processed_matches dedup, so re-running
        without new matches is a cheap no-op (one HTTP call to
        tracker.gg, no DB writes).
        """
        from core.config import (
            SMITE2_BACKFILL_INTERVAL, SMITE2_BACKFILL_BOOT_DELAY,
        )
        # Initial delay so smite plugin's on_ready finishes first.
        try:
            await asyncio.sleep(SMITE2_BACKFILL_BOOT_DELAY)
        except asyncio.CancelledError:
            raise

        while True:
            try:
                await self.backfill_recent_matches()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # One bad cycle (e.g., transient tracker.gg outage)
                # doesn't kill the loop — log and try again next tick.
                print(f"[Economy] Backfill cycle error: {e}")

            try:
                await asyncio.sleep(SMITE2_BACKFILL_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def cleanup(self):
        """Persist state and release per-plugin resources."""
        # Cancel the backfill task if it's still running. Backfill is
        # idempotent (dedup via processed_matches), so a partial cancel
        # mid-loop is safe — already-settled matches stay settled and
        # anything unsettled is picked up on the next launch.
        #
        # NOTE (2026-07-02): this file was truncated on disk mid-comment
        # right here for several releases — the file-tool desync that
        # HATMASBOT.md warns about. It still compiled because the
        # docstring alone is a valid function body, so cleanup silently
        # did NOTHING (backfill task never cancelled, aiohttp session
        # never closed). Body reconstructed below.
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except (asyncio.CancelledError, Exception):
                pass
            self._backfill_task = None

        # Close our own aiohttp session (the MixItUp API client).
        if self.session is not None:
            try:
                await self.session.close()
            except Exception as e:
                print(f"[Economy] Session close error: {e}")
            self.session = None
        self._connected = False

        # The shared aiosqlite connection is closed by main.py AFTER
        # every plugin's cleanup has run — just drop our reference.
        self._db = None
        print("[Economy] Cleaned up")
