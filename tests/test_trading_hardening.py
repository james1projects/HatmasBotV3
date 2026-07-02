"""
Trading hardening regression tests
==================================
Covers the 2026-07-02 overnight hardening of execute_buy/execute_sell:
  1. Buy refunds the MixItUp deduction if the portfolio write fails.
  2. Sell restores removed shares if the MixItUp credit fails.
  3. Sell payout is exactly the requested hat amount (round, not floor).
  4. The per-user trade lock prevents concurrent double-spends.

Usage: python tests/test_trading_hardening.py   (exit 0 = pass)
"""

import asyncio, sys, os
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import core.config as config
config.ECONOMY_DB_PATH = PROJECT_ROOT / 'data' / 'economy_hardening_test.db'
from plugins.economy import EconomyPlugin

class MockBot:
    def __init__(self):
        self.web_server = None
        self.plugins = {}
    def register_command(self, name, handler, **kwargs):
        pass
    def is_feature_enabled(self, feature):
        return True

class MockMixItUp:
    def __init__(self):
        self.balances = {}
    def set_balance(self, u, a):
        self.balances[u] = a
    def get_balance(self, u):
        return self.balances.get(u, 0)
    def adjust(self, u, a):
        self.balances[u] = self.balances.get(u, 0) + a
        return True

async def run_tests(economy, mock):
    # TEST 1: buy refunds on portfolio-write failure
    mock.set_balance('v1', 5000)
    original_add_shares = economy._add_shares
    async def failing_add_shares(u, g, s, p):
        raise RuntimeError('disk full')
    economy._add_shares = failing_add_shares
    result = await economy.execute_buy('v1', 'ymir', 1000)
    assert result['success'] is False, "Buy should fail when portfolio write fails"
    assert mock.get_balance('v1') == 5000, "Balance should be refunded after failed buy"
    economy._add_shares = original_add_shares
    print("TEST 1 passed: buy refunds hats on portfolio-write failure")

    # TEST 2: sell restores shares when MixItUp credit fails
    result = await economy.execute_buy('v1', 'ymir', 1000)
    assert result['success'] is True, "Setup buy should succeed"
    holding = await economy._get_holding('v1', 'Ymir')
    assert holding is not None and holding['shares'] > 0, "Should hold shares after buying"

    original_adjust = economy._adjust_balance
    async def credit_fails(u, a):
        if a > 0:   # credit (positive) fails
            return False
        return await original_adjust(u, a)   # debit still works
    economy._adjust_balance = credit_fails

    result = await economy.execute_sell('v1', 'ymir', 500)
    assert result['success'] is False, "Sell should fail when the credit fails"
    restored = await economy._get_holding('v1', 'Ymir')
    assert restored is not None, "Holding must still exist after failed sell"
    assert abs(restored['shares'] - holding['shares']) < 1e-9, \
        f"Shares should be restored ({restored['shares']} != {holding['shares']})"
    assert abs(restored['avg_cost'] - holding['avg_cost']) < 1e-9, \
        "Avg cost should be unchanged after restore"
    economy._adjust_balance = original_adjust
    print("TEST 2 passed: sell restores shares when credit fails")

    # TEST 3: sell payout exact rounding
    balance_before = mock.get_balance('v1')
    result = await economy.execute_sell('v1', 'ymir', 300)
    assert result['success'] is True, f"Sell should succeed: {result.get('error')}"
    assert result['net_received'] == 300, \
        f"Net received should be exactly 300, got {result['net_received']}"
    assert mock.get_balance('v1') == balance_before + 300, \
        "Balance should increase by exactly 300"
    print("TEST 3 passed: sell payout is exactly the requested amount")

    # TEST 4: per-user lock prevents concurrent double-spend
    mock.set_balance('v2', 1000)
    async def slow_get_balance(u):
        await asyncio.sleep(0.05)   # simulate slow MixItUp HTTP round-trip
        return mock.get_balance(u)
    economy._get_balance = slow_get_balance

    r1, r2 = await asyncio.gather(
        economy.execute_buy('v2', 'ymir', 600),
        economy.execute_buy('v2', 'ymir', 600),
    )
    success_count = sum([r1['success'], r2['success']])
    assert success_count == 1, \
        f"Exactly one concurrent buy should succeed, got {success_count}"
    assert mock.get_balance('v2') == 400, \
        f"Final balance should be 400, got {mock.get_balance('v2')}"
    print("TEST 4 passed: per-user lock serializes concurrent trades")

async def main():
    test_db = config.ECONOMY_DB_PATH
    if test_db.exists():
        os.remove(test_db)
    bot = MockBot()
    economy = EconomyPlugin()
    economy.setup(bot)
    from core import db as shared_db
    await shared_db.init_db()
    if economy._db is None:
        economy._db = await shared_db.get_db()
    await economy._load_prices()
    await economy._build_god_name_index()
    economy._connected = True
    economy._emit_trade_event = lambda *a, **k: None
    mock = MockMixItUp()
    async def mock_get_balance(u):
        return mock.get_balance(u)
    async def mock_adjust_balance(u, a):
        mock.adjust(u, a)
        return True
    economy._get_balance = mock_get_balance
    economy._adjust_balance = mock_adjust_balance
    await economy.seed_prices({'Ymir': {'price': 100, 'games': 10,
                                        'wins': 6, 'losses': 4}})
    await economy._build_god_name_index()

    # Cleanup runs even when an assertion fails: without close_db the
    # aiosqlite worker thread is non-daemon and the process would hang
    # forever instead of exiting non-zero.
    try:
        await run_tests(economy, mock)
    finally:
        await economy.cleanup()
        await shared_db.close_db()
        if test_db.exists():
            os.remove(test_db)

    print('ALL HARDENING TESTS PASSED')

if __name__ == '__main__':
    asyncio.run(main())
