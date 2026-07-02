"""
catchup_backfill.py — settle unprocessed tracker.gg matches (deep fetch)
=========================================================================

One-off / occasional catch-up for when the bot's periodic backfill has
been down for a while (e.g. the May–July 2026 match.py truncation) and
more matches are outstanding than the normal 50-match listing window
covers.

Fetches a DEEP match history from tracker.gg (paginated, default cap
500 matches / 20 pages — pagination is best-effort: if tracker.gg
returns no cursor, you get one page and that's the fallback), diffs it
against processed_matches, and settles every unseen match oldest→newest
through the exact same EconomyPlugin.settle_match() path the bot uses.
Dedup, price math, and fair-value recompute are identical to the live
backfill; celebration side-effects (dividends, free shares, overlays)
stay off because the tool runs with no bot / stream-status attached, so
settlement is silent price math — same as any off-stream backfill.

Unlike tools/replay_economy.py this does NOT wipe anything — it only
adds settlements the bot missed. A timestamped economy.db backup is
taken before writing anyway.

Usage:
    python tools/catchup_backfill.py                 # backup, fetch, settle
    python tools/catchup_backfill.py --dry-run       # show what would settle
    python tools/catchup_backfill.py --max 1000 --pages 40

Run while the bot is STOPPED if possible: the bot caches prices in
memory, so settlements written behind its back won't show on stream
until it restarts (the DB itself is WAL and safe either way).
"""

import argparse
import asyncio
import shutil
import sys
import time

# settle_match() logs contain non-cp1252 characters (e.g. the "→" in
# its price line) which raise UnicodeEncodeError AFTER the DB commit
# when stdout is a legacy Windows codepage. Harmless to the data but
# noisy — force UTF-8 with replacement so logs always print.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import aiosqlite
except ImportError:
    print("Missing aiosqlite. Install with: pip install aiosqlite")
    sys.exit(1)

try:
    from curl_cffi.requests import Session as CffiSession
except ImportError:
    print("Missing curl_cffi. Install with: pip install curl_cffi")
    sys.exit(1)

from core.config import ECONOMY_DB_PATH, SMITE2_PLATFORM_ID
from plugins.smite import SmitePlugin
from plugins.economy import EconomyPlugin

DEFAULT_MAX_MATCHES = 500
DEFAULT_MAX_PAGES = 20


async def cmd_catchup(dry_run: bool, max_matches: int, max_pages: int) -> int:
    if not SMITE2_PLATFORM_ID:
        print("SMITE2_PLATFORM_ID is empty — set it in config_local.py first.")
        return 1

    db_path = Path(ECONOMY_DB_PATH)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    if not dry_run:
        backup = db_path.with_name(f"economy_backup_{int(time.time())}.db")
        shutil.copy(db_path, backup)
        print(f"Backed up to: {backup}")

    # Tracker.gg client wiring mirrors tools/replay_economy.py.
    smite = SmitePlugin()
    smite._cffi_session = CffiSession(
        impersonate="chrome124", headers=smite.HEADERS)
    smite._cffi_executor = ThreadPoolExecutor(max_workers=2)

    db = await aiosqlite.connect(str(db_path))
    await db.execute("PRAGMA journal_mode=WAL")

    economy = EconomyPlugin()
    economy._db = db

    try:
        await economy._migrate_god_prices_kda_columns()
        await economy._load_prices()

        print(f"Fetching up to {max_matches} matches "
              f"(max {max_pages} page(s)) from tracker.gg…")
        history = await smite.get_match_history(
            limit=max_matches, max_pages=max_pages)
        if not history:
            print("No matches returned from tracker.gg — nothing to do.")
            return 1
        print(f"Fetched {len(history)} match(es) from tracker.gg.")

        seen_ids = set()
        async with db.execute(
                "SELECT match_id FROM processed_matches") as cur:
            async for row in cur:
                seen_ids.add(row[0])

        new_matches = [m for m in history if m["match_id"] not in seen_ids]
        if not new_matches:
            print("Every fetched match is already settled. Nothing to do.")
            return 0
        print(f"{len(new_matches)} unprocessed match(es) found.")

        # Oldest → newest so price compounding follows real time
        # (tracker.gg returns newest-first).
        new_matches.reverse()

        settled = skipped = errors = 0
        per_god: dict = {}
        for entry in new_matches:
            mid = entry["match_id"]
            try:
                parsed = smite.parse_listing_entry(entry.get("raw_entry"))
                if not parsed:
                    detail = await smite._fetch_match_detail(mid)
                    parsed = smite.parse_match_for_settlement(detail)
                if not parsed:
                    print(f"  couldn't parse match {mid}, skipping")
                    skipped += 1
                    continue
                god, outcome, k, d, a = parsed

                if dry_run:
                    print(f"  [dry-run] would settle: {god:16s} "
                          f"{outcome:4s}  {k}/{d}/{a}  ({mid})")
                    settled += 1
                    per_god[god] = per_god.get(god, 0) + 1
                    continue

                ok = await economy.settle_match(
                    match_id=mid, god_name=god, outcome=outcome,
                    kills=k, deaths=d, assists=a, source="backfill")
                if ok:
                    settled += 1
                    per_god[god] = per_god.get(god, 0) + 1
                else:
                    skipped += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                errors += 1
                print(f"  error on match {mid}: {e}")

        verb = "would settle" if dry_run else "settled"
        print()
        print(f"Catch-up complete: {verb} {settled}, "
              f"skipped {skipped}, errors {errors}")
        if per_god:
            print("Per god:")
            for god, n in sorted(per_god.items(), key=lambda kv: -kv[1]):
                print(f"  {god:20s} {n}")
        if not dry_run and settled:
            print()
            print("Note: if the bot is currently running, restart it so its "
                  "in-memory price cache picks up the new settlements.")
        return 0

    finally:
        await db.close()
        try:
            smite._cffi_session.close()
        except Exception:
            pass
        smite._cffi_executor.shutdown(wait=False)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Settle tracker.gg matches the bot's backfill missed.")
    p.add_argument("--dry-run", action="store_true",
        help="List matches that would settle; don't write anything.")
    p.add_argument("--max", type=int, default=DEFAULT_MAX_MATCHES,
        metavar="N", help=f"Max matches to fetch (default {DEFAULT_MAX_MATCHES}).")
    p.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES,
        metavar="N", help=f"Max listing pages (default {DEFAULT_MAX_PAGES}).")
    args = p.parse_args()
    return asyncio.run(cmd_catchup(
        dry_run=args.dry_run, max_matches=args.max, max_pages=args.pages))


if __name__ == "__main__":
    sys.exit(main())
