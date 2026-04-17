"""
Economy Plugin Test Harness
============================
Simulates matches with fake KDA events to verify price movement,
dividends, trading, and portfolio tracking without running the full bot.

Usage: python tests/test_economy.py
"""

import asyncio
import sys
import os
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Override DB path to use a test database
import core.config as config
config.ECONOMY_DB_PATH = PROJECT_ROOT / "data" / "economy_test.db"

from plugins.economy import EconomyPlugin


class MockMessage:
    """Mock Twitch message for testing chat commands."""
    def __init__(self, username="testuser", display_name="TestUser"):
        self.chatter = MockChatter(username, display_name)


class MockChatter:
    def __init__(self, name, display_name):
        self.name = name
        self.display_name = display_name


class MockBot:
    """Minimal mock bot for testing."""
    def __init__(self):
        self.web_server = None
        self.plugins = {}
        self._features = {"economy": True}
        self._commands = {}
        self._replies = []

    def register_command(self, name, handler, **kwargs):
        self._commands[name] = handler

    def is_feature_enabled(self, feature):
        return self._features.get(feature, True)

    def is_mod(self, chatter):
        return False

    async def send_reply(self, message, text, whisper=False):
        print(f"  💬 [{message.chatter.display_name}] {text}")
        self._replies.append(text)

    async def send_chat(self, text):
        print(f"  💬 [CHAT] {text}")


class MockMixItUp:
    """Mock MixItUp balance tracker (in-memory)."""
    def __init__(self):
        self.balances = {}

    def set_balance(self, username, amount):
        self.balances[username] = amount

    def get_balance(self, username):
        return self.balances.get(username, 0)

    def adjust(self, username, amount):
        self.balances[username] = self.balances.get(username, 0) + amount
        return True


async def run_tests():
    print("=" * 60)
    print("  Economy Plugin Test Harness")
    print("=" * 60)
    print()

    # Clean up test DB if it exists
    test_db = config.ECONOMY_DB_PATH
    if test_db.exists():
        os.remove(test_db)

    # Create plugin with mock bot
    bot = MockBot()
    economy = EconomyPlugin()
    economy.setup(bot)

    # Override MixItUp methods with mock
    mock_miu = MockMixItUp()
    mock_miu.set_balance("viewer1", 10000)
    mock_miu.set_balance("viewer2", 5000)
    mock_miu.set_balance("viewer3", 8000)

    # Initialize database (skip MixItUp connection)
    await economy._init_db()
    await economy._load_prices()
    await economy._build_god_name_index()
    economy._connected = True  # Pretend MixItUp is connected

    # Monkey-patch currency methods to use mock
    async def mock_get_balance(username):
        return mock_miu.get_balance(username)

    async def mock_adjust_balance(username, amount):
        mock_miu.adjust(username, amount)
        return True

    economy._get_balance = mock_get_balance
    economy._adjust_balance = mock_adjust_balance

    print("✅ Database initialized")
    print()

    # ── Test 1: Seed Initial Prices ──
    print("─" * 40)
    print("TEST 1: Seed Initial Prices")
    print("─" * 40)

    await economy.seed_prices({
        "Ymir": {"price": 300, "games": 22, "wins": 16, "losses": 6},
        "Loki": {"price": 30, "games": 11, "wins": 2, "losses": 9},
        "Athena": {"price": 220, "games": 15, "wins": 10, "losses": 5},
        "Thanatos": {"price": 180, "games": 8, "wins": 5, "losses": 3},
        "Danzaburou": {"price": 150, "games": 3, "wins": 2, "losses": 1},
    })

    for god, price in sorted(economy._prices.items()):
        vol_mult, vol_tier = economy._get_volatility(god)
        games = economy._games_played.get(god, 0)
        print(f"  {god:12s}: {price:>7.0f} hats | {games:>2} games | {vol_tier} ({vol_mult}x)")

    print()

    # ── Test 2: Buy Shares ──
    print("─" * 40)
    print("TEST 2: Buy Shares")
    print("─" * 40)

    msg = MockMessage("viewer1", "Viewer1")

    # Buy Ymir shares
    result = await economy.execute_buy("viewer1", "ymir", 3000)
    print(f"  Buy Ymir (3000 hats): {result}")
    print(f"  Balance: {mock_miu.get_balance('viewer1'):,}")

    # Buy Loki shares
    result = await economy.execute_buy("viewer1", "loki", 500)
    print(f"  Buy Loki (500 hats): {result}")

    # Viewer 2 buys Ymir
    result = await economy.execute_buy("viewer2", "ymir", 2000)
    print(f"  Viewer2 Buy Ymir (2000 hats): {result}")

    # Viewer 3 buys Danzaburou
    result = await economy.execute_buy("viewer3", "danzaburou", 1500)
    print(f"  Viewer3 Buy Danz (1500 hats): {result}")

    print()

    # ── Test 3: View Portfolio ──
    print("─" * 40)
    print("TEST 3: View Portfolio")
    print("─" * 40)

    holdings = await economy._get_full_portfolio("viewer1")
    for h in holdings:
        print(f"  {h['god_name']:12s}: {h['shares']:.2f} shares @ {h['avg_cost']:.0f} avg "
              f"= {h['value']:.0f} hats ({h['pnl_pct']:+.1f}% P&L)")

    total = await economy._get_portfolio_value("viewer1")
    print(f"  Total value: {total:,.0f} hats")
    print()

    # ── Test 4: Simulate a Match (Ymir — Strong Win) ──
    print("─" * 40)
    print("TEST 4: Simulate Match — Ymir Strong Win (8/2/6)")
    print("─" * 40)

    # God detected → dividend
    ymir_price_before = economy._prices["Ymir"]
    print(f"  Price before match: {ymir_price_before:.0f}")

    await economy.on_god_detected({"name": "Ymir"})
    print(f"  Dividend paid ✓")

    # Simulate live KDA events
    for i in range(8):
        await economy.on_kill("player_kill")
    for i in range(2):
        await economy.on_death()
    for i in range(6):
        await economy.on_assist()

    live_price = economy._prices["Ymir"]
    print(f"  Price after live ticks (8K/2D/6A): {live_price:.1f}")

    # Match ends
    await economy.on_match_end({"match_id": "test_001", "god": {"name": "Ymir"}})

    # Result: WIN
    await economy.on_match_result({
        "outcome": "win",
        "god": "Ymir",
        "stats": {"kills": 8, "deaths": 2, "assists": 6},
        "record": "1-0",
    })

    final_price = economy._prices["Ymir"]
    change_pct = ((final_price - ymir_price_before) / ymir_price_before) * 100
    print(f"  Final settlement price: {final_price:.0f} ({change_pct:+.1f}%)")
    print(f"  Games played: {economy._games_played['Ymir']}")
    print()

    # ── Test 5: Simulate a Match (Loki — Feeding Loss) ──
    print("─" * 40)
    print("TEST 5: Simulate Match — Loki Feeding Loss (1/10/1)")
    print("─" * 40)

    loki_price_before = economy._prices["Loki"]
    print(f"  Price before match: {loki_price_before:.0f}")

    await economy.on_god_detected({"name": "Loki"})

    # Simulate live KDA
    await economy.on_kill("player_kill")  # 1 kill
    for i in range(10):
        await economy.on_death()
    await economy.on_assist()  # 1 assist

    live_price = economy._prices["Loki"]
    print(f"  Price after live ticks (1K/10D/1A): {live_price:.1f}")

    await economy.on_match_end({"match_id": "test_002", "god": {"name": "Loki"}})
    await economy.on_match_result({
        "outcome": "loss",
        "god": "Loki",
        "stats": {"kills": 1, "deaths": 10, "assists": 1},
        "record": "1-1",
    })

    final_price = economy._prices["Loki"]
    change_pct = ((final_price - loki_price_before) / loki_price_before) * 100
    print(f"  Final settlement price: {final_price:.1f} ({change_pct:+.1f}%)")
    print(f"  Games played: {economy._games_played['Loki']}")
    print()

    # ── Test 6: Sell Shares ──
    print("─" * 40)
    print("TEST 6: Sell Shares")
    print("─" * 40)

    holding = await economy._get_holding("viewer1", "Ymir")
    if holding:
        print(f"  Viewer1 holds {holding['shares']:.2f} Ymir shares")

    balance_before = mock_miu.get_balance("viewer1")
    result = await economy.execute_sell("viewer1", "ymir", 2000)
    print(f"  Sell Ymir (2000 hats worth): {result}")
    print(f"  Balance: {balance_before:,} → {mock_miu.get_balance('viewer1'):,}")

    print()

    # ── Test 7: Chat Commands ──
    print("─" * 40)
    print("TEST 7: Chat Commands")
    print("─" * 40)

    msg = MockMessage("viewer1", "Viewer1")

    # !price ymir
    await economy.cmd_price(msg, "ymir")

    # !portfolio
    await economy.cmd_portfolio(msg, "")

    # !market
    await economy.cmd_market(msg, "")

    # !dividend
    await economy.cmd_dividend(msg, "")

    print()

    # ── Test 8: Price Formula Verification ──
    print("─" * 40)
    print("TEST 8: Price Formula (All Scenarios)")
    print("─" * 40)

    scenarios = [
        ("Domination Win", "win", 12, 1, 6, "Ymir"),
        ("Strong Win", "win", 7, 3, 4, "Ymir"),
        ("Average Win", "win", 4, 4, 3, "Ymir"),
        ("Carried Win", "win", 1, 6, 2, "Ymir"),
        ("Close Loss", "loss", 5, 6, 4, "Ymir"),
        ("Clear Loss", "loss", 3, 7, 2, "Ymir"),
        ("Feeding Loss", "loss", 1, 10, 1, "Ymir"),
        ("Penny Stock Win", "win", 7, 3, 4, "Danzaburou"),
        ("Penny Stock Loss", "loss", 3, 7, 2, "Danzaburou"),
    ]

    print(f"  {'Scenario':<20s} {'KDA':>10s} {'Base%':>8s} {'Vol':>5s} {'Final%':>8s}")
    print(f"  {'─'*20} {'─'*10} {'─'*8} {'─'*5} {'─'*8}")

    for name, outcome, k, d, a, god in scenarios:
        change = economy._calculate_match_end_change(outcome, k, d, a, god)
        vol, tier = economy._get_volatility(god)
        base = change / vol
        kda_str = f"{k}/{d}/{a} {'W' if outcome == 'win' else 'L'}"
        print(f"  {name:<20s} {kda_str:>10s} {base:>+7.1f}% {vol:>4.1f}x {change:>+7.1f}%")

    print()

    # ── Test 9: Check Final State ──
    print("─" * 40)
    print("TEST 9: Final Market State")
    print("─" * 40)

    for god, price in sorted(economy._prices.items(), key=lambda x: -x[1]):
        vol_mult, vol_tier = economy._get_volatility(god)
        games = economy._games_played.get(god, 0)
        session_change = economy._session_changes.get(god, 0)
        print(f"  {god:12s}: {price:>7.1f} hats | {games:>2} games | "
              f"{session_change:>+6.1f}% session | {vol_tier}")

    print()

    # ── Test 10: API endpoint data ──
    print("─" * 40)
    print("TEST 10: API Data Check")
    print("─" * 40)

    # Check leaderboard query
    leaders = []
    async with economy._db.execute("""
        SELECT p.username, SUM(p.shares * gp.price) as portfolio_value
        FROM portfolios p
        JOIN god_prices gp ON p.god_name = gp.god_name
        WHERE p.shares > 0.001
        GROUP BY p.username
        ORDER BY portfolio_value DESC
        LIMIT 5
    """) as cursor:
        async for row in cursor:
            leaders.append((row[0], row[1]))

    for i, (user, value) in enumerate(leaders, 1):
        print(f"  #{i} {user}: {value:,.0f} hats")

    print()

    # Cleanup
    await economy.cleanup()
    if test_db.exists():
        os.remove(test_db)

    print("=" * 60)
    print("  All tests completed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_tests())
