"""
tools/wallet_audit.py — prove wallet_balances == SUM(wallet_ledger.delta)
=========================================================================

    python tools/wallet_audit.py           # report mismatches (exit 1 if any)
    python tools/wallet_audit.py --fix     # rewrite balances from the ledger,
                                           # recording an 'adjust' row each

Also prints the last passive-earning ticks and the import runs so the
whole money path can be eyeballed in one go. Safe with the bot running
(WAL); --fix should be done with it stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite  # noqa: E402

from core import wallet as _wallet  # noqa: E402
from core.config import ECONOMY_DB_PATH  # noqa: E402


async def run(fix: bool) -> int:
    db = await aiosqlite.connect(str(ECONOMY_DB_PATH))
    try:
        await _wallet.ensure_schema(db)
        async with db.execute(
                "SELECT asset, COUNT(*), COALESCE(SUM(amount), 0) FROM wallet_balances "
                "GROUP BY asset") as c:
            for asset, n, total in await c.fetchall():
                print(f"{asset:10s} {n:6d} wallets  {int(total):,} total")
        bad = await _wallet.audit(db)
        if not bad:
            print("Ledger and balances agree.")
        else:
            print(f"{len(bad)} mismatch(es):")
            for uid, asset, bal, total in bad:
                print(f"  {uid} {asset}: balance {bal} vs ledger {total}")
            if fix:
                for uid, asset, _b, _t in bad:
                    d = await _wallet.fix_balance(db, uid, asset, actor="wallet_audit")
                    print(f"  fixed {uid} {asset} ({d:+d})")
        ticks = await _wallet.recent_ticks(db, 5)
        if ticks:
            print("Recent earning ticks:")
            for t in ticks:
                print(f"  #{t['id']} {t['ticked_at']} live={t['was_live']} "
                      f"chatters={t['chatters']} credited={t['credited']} "
                      f"skipped={t['skipped']} hats={t['hats_total']}"
                      + (f" error={t['error']}" if t["error"] else ""))
        async with db.execute(
                "SELECT id, dry_run, started_at, users_seen, users_imported, hats_total, "
                "tokens_total FROM wallet_imports ORDER BY id DESC LIMIT 5") as c:
            rows = await c.fetchall()
        if rows:
            print("Import runs:")
            for r in rows:
                print(f"  #{r[0]} {'dry ' if r[1] else ''}{r[2]}: seen {r[3]}, "
                      f"imported {r[4]}, hats {r[5]:,}, tokens {r[6]:,}")
        return 1 if (bad and not fix) else 0
    finally:
        await db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--fix", action="store_true")
    return asyncio.run(run(ap.parse_args().fix))


if __name__ == "__main__":
    sys.exit(main())
