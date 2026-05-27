"""
purge_excluded.py — wipe bot-account rows from the economy DB
==============================================================

Deletes portfolios + transactions rows for any username in
ECONOMY_EXCLUDED_USERNAMES (StreamElements, Nightbot, etc.) plus the
bot's own TWITCH_BOT_USERNAME. The exclusion list is filtered at query
time everywhere else, but if you also want those accounts permanently
removed from the database (for example, so you don't see legacy data
if you ever take a name off the exclusion list), this tool does that.

YouTube tables are NOT touched — they're keyed on YouTube channel ID,
not username, and YouTube doesn't have the same bot-account problem.

Usage:
    python tools/purge_excluded.py --dry-run      # preview only
    python tools/purge_excluded.py                # confirms before deleting
    python tools/purge_excluded.py --yes          # skip the confirmation

A timestamped backup of economy.db is taken before any deletion
(same pattern as replay_economy.py). To roll back, copy the backup
file over economy.db.
"""

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import aiosqlite
except ImportError:
    print("Missing aiosqlite. Install with: pip install aiosqlite")
    sys.exit(1)

from core.config import (
    ECONOMY_DB_PATH,
    ECONOMY_EXCLUDED_USERNAMES,
    TWITCH_BOT_USERNAME,
)


def build_excluded() -> list:
    """Same logic as economy.py — config list plus the bot's own name."""
    out = {u.lower() for u in (ECONOMY_EXCLUDED_USERNAMES or []) if u}
    if TWITCH_BOT_USERNAME and TWITCH_BOT_USERNAME != "YOUR_BOT_USERNAME":
        out.add(TWITCH_BOT_USERNAME.lower())
    return sorted(out)


async def cmd_purge(dry_run: bool, skip_confirm: bool) -> int:
    excluded = build_excluded()
    if not excluded:
        print("No excluded usernames configured. Nothing to purge.")
        return 0

    print("Excluded usernames:")
    for u in excluded:
        print(f"  - {u}")
    print()

    db_path = Path(ECONOMY_DB_PATH)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    placeholders = ",".join("?" for _ in excluded)

    async with aiosqlite.connect(str(db_path)) as db:
        # Count what we'd delete and report it.
        async with db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(shares), 0) FROM portfolios "
            f"WHERE LOWER(username) IN ({placeholders})",
            excluded
        ) as cur:
            row = await cur.fetchone()
            n_portfolios = row[0] if row else 0
            total_shares = float(row[1]) if row else 0.0

        async with db.execute(
            f"SELECT COUNT(*) FROM transactions "
            f"WHERE LOWER(username) IN ({placeholders})",
            excluded
        ) as cur:
            row = await cur.fetchone()
            n_txns = row[0] if row else 0

        # Per-user breakdown so you can see which bots have the most
        # accumulated cruft. Useful debug info.
        async with db.execute(
            f"SELECT username, COUNT(DISTINCT god_name) as gods, "
            f"  COALESCE(SUM(shares), 0) as total_shares "
            f"FROM portfolios "
            f"WHERE LOWER(username) IN ({placeholders}) "
            f"GROUP BY username ORDER BY total_shares DESC",
            excluded
        ) as cur:
            per_user = await cur.fetchall()

        print(f"Would delete:")
        print(f"  Portfolio rows:    {n_portfolios} "
              f"(totaling {total_shares:.2f} shares)")
        print(f"  Transaction rows:  {n_txns}")
        if per_user:
            print()
            print("  Per-account breakdown:")
            for r in per_user:
                print(f"    {r[0]:25s}  {r[1]:>3} gods  {r[2]:>8.2f} shares")
        print()

        if n_portfolios == 0 and n_txns == 0:
            print("Nothing matches. Already clean.")
            return 0

        if dry_run:
            print("[dry-run] No changes made.")
            return 0

        if not skip_confirm:
            ans = input(
                "Type 'purge' to delete the rows above (irreversible): "
            ).strip().lower()
            if ans != "purge":
                print("Aborted.")
                return 1

        # Backup before mutating.
        backup = db_path.with_name(f"economy_backup_{int(time.time())}.db")
        shutil.copy(db_path, backup)
        print(f"Backed up to: {backup}")

        await db.execute(
            f"DELETE FROM portfolios "
            f"WHERE LOWER(username) IN ({placeholders})",
            excluded
        )
        await db.execute(
            f"DELETE FROM transactions "
            f"WHERE LOWER(username) IN ({placeholders})",
            excluded
        )
        await db.commit()

        print(f"Purged {n_portfolios} portfolio rows + "
              f"{n_txns} transaction rows.")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Delete portfolios + transactions rows for users in "
                    "ECONOMY_EXCLUDED_USERNAMES.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be deleted; don't write.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the 'type purge to confirm' prompt.")
    args = p.parse_args()
    return asyncio.run(cmd_purge(dry_run=args.dry_run, skip_confirm=args.yes))


if __name__ == "__main__":
    sys.exit(main())
