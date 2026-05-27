"""
replay_economy.py — rebuild god prices from tracker.gg profile aggregates
==========================================================================

Resets god_prices, price_history, and processed_matches, then fetches
tracker.gg's per-god LIFETIME stats (games, wins, losses, KDA totals)
from the broadcaster's profile and recomputes each god's price via the
fair-value formula.

This is way more accurate than walking the last N matches because
tracker.gg's profile holds aggregates across the broadcaster's entire
recorded history, not just the recent window.

PRESERVED (NOT wiped):
  * portfolios + youtube_holdings (user share counts)
  * transactions + youtube_transactions (history of buys/sells/dividends)
  * dividends (historical dividend events)
  * youtube_portfolios + youtube_video_gods + youtube_processed_comments

After replay, every god's price reflects only the broadcaster's actual
all-time performance under the current fair-value formula. Holders'
positions are preserved; their P&L just reflects the new prices.

Usage:
    python tools/replay_economy.py             # confirms before running
    python tools/replay_economy.py --yes       # skip confirmation
    python tools/replay_economy.py --dry-run   # show what would happen

A timestamped backup of economy.db is taken before any changes. To
roll back, copy the backup over economy.db. The raw profile JSON is
also saved to data/profile_debug.json on each run for diagnostic use.
"""

import argparse
import asyncio
import shutil
import sys
import time
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

from core.config import (
    ECONOMY_DB_PATH, ECONOMY_STARTING_PRICE,
    SMITE2_PLATFORM, SMITE2_PLATFORM_ID,
)
# Import the plugin classes so we can leverage their parsing + settle
# logic without standing up the entire bot.
from plugins.smite import SmitePlugin
from plugins.economy import EconomyPlugin


DEFAULT_MAX_MATCHES = 2000


async def cmd_replay(skip_confirm: bool, dry_run: bool,
                     max_matches: int) -> int:
    if not SMITE2_PLATFORM_ID:
        print("SMITE2_PLATFORM_ID is empty — set it in config_local.py first.")
        return 1

    db_path = Path(ECONOMY_DB_PATH)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        print("Run the bot at least once before running this tool.")
        return 1

    # ─── Confirm ──────────────────────────────────────────────────────
    if not dry_run and not skip_confirm:
        print("=" * 60)
        print("REPLAY ECONOMY — this will:")
        print(f"  * Back up: {db_path}")
        print( "  * Wipe god_prices to defaults (price=100, totals=0)")
        print( "  * Delete every row from price_history")
        print( "  * Delete every row from processed_matches")
        print(f"  * Walk tracker.gg history (cap {max_matches} matches)")
        print( "  * Replay each match through the fair-value formula")
        print()
        print("PRESERVED: portfolios, transactions, dividends,")
        print("           youtube_holdings, youtube_portfolios.")
        print("=" * 60)
        ans = input("Type 'reset' to proceed: ").strip().lower()
        if ans != "reset":
            print("Aborted.")
            return 1

    # ─── Backup ───────────────────────────────────────────────────────
    if not dry_run:
        backup = db_path.with_name(
            f"economy_backup_{int(time.time())}.db")
        shutil.copy(db_path, backup)
        print(f"Backed up to: {backup}")

    # ─── Set up tracker.gg client (curl_cffi mirrors smite.py) ───────
    smite = SmitePlugin()
    smite._cffi_session = CffiSession(
        impersonate="chrome124", headers=smite.HEADERS)
    smite._cffi_executor = ThreadPoolExecutor(max_workers=2)

    # ─── Open DB ──────────────────────────────────────────────────────
    db = await aiosqlite.connect(str(db_path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=OFF")  # easier wipes

    try:
        # Run the migration so the new k/d/a columns exist.
        await _run_kda_migration(db)

        # ─── Fetch profile aggregates (canonical source) ──────────────
        # Tracker.gg's profile endpoint returns lifetime per-god stats
        # (games, wins, losses, KDA) across the broadcaster's entire
        # history. Way more accurate than walking the last 25 matches.
        print("Fetching tracker.gg profile aggregates…")
        # Save the raw profile too, in case we need to debug field names.
        try:
            import json as _json
            raw_profile = await smite._fetch_profile(force_refresh=True)
            if raw_profile is not None:
                ppath = Path(ECONOMY_DB_PATH).parent / "profile_debug.json"
                ppath.write_text(_json.dumps(raw_profile, indent=2,
                                             default=str))
                print(f"Profile saved to: {ppath}")
        except Exception as _e:
            print(f"(couldn't save profile dump: {_e})")

        # force_refresh=True so each tracked gamemode gets a fresh
        # fetch with ?forceCollect=true. Without that, recently played
        # matches (especially in non-ranked modes) might not yet be
        # reflected in tracker.gg's aggregate cache.
        aggregates = await smite.get_god_aggregates(force_refresh=True)
        if not aggregates:
            print("No god aggregates returned. Profile endpoint may "
                  "have an unexpected shape — check profile_debug.json "
                  "and we'll fix the parser.")
            return 1

        print(f"Got per-god stats for {len(aggregates)} god(s).")

        if dry_run:
            print("[dry-run] Would wipe state and apply these prices:")
            from plugins.economy import calculate_fair_value
            for god, agg in sorted(aggregates.items()):
                fv = calculate_fair_value(
                    agg["wins"], agg["losses"],
                    agg["kills"], agg["deaths"], agg["assists"])
                wr = (100.0 * agg["wins"] / agg["games"]
                      if agg["games"] else 0)
                print(f"  {god:20s}  {fv:6.0f}  "
                      f"{agg['games']}g  {agg['wins']}-{agg['losses']} "
                      f"({wr:.0f}%)  "
                      f"K/D/A {agg['kills']}/{agg['deaths']}/{agg['assists']}")
            return 0

        # ─── Wipe state ──────────────────────────────────────────────
        print("Wiping god_prices, price_history, processed_matches…")
        await db.execute("""
            UPDATE god_prices SET
                price          = ?,
                games_played   = 0,
                total_wins     = 0,
                total_losses   = 0,
                total_kills    = 0,
                total_deaths   = 0,
                total_assists  = 0,
                updated_at     = datetime('now')
        """, (ECONOMY_STARTING_PRICE,))
        await db.execute("DELETE FROM price_history")
        await db.execute("DELETE FROM processed_matches")
        await db.commit()
        print("State wiped.")

        # ─── Apply aggregate stats + fair-value price per god ───────
        from plugins.economy import calculate_fair_value
        applied = 0
        for god, agg in aggregates.items():
            if agg["games"] <= 0:
                continue
            fv = calculate_fair_value(
                agg["wins"], agg["losses"],
                agg["kills"], agg["deaths"], agg["assists"])

            # INSERT OR REPLACE so a god that doesn't yet have a row
            # gets one; existing rows are updated atomically.
            await db.execute("""
                INSERT INTO god_prices (
                    god_name, price, games_played,
                    total_wins, total_losses,
                    total_kills, total_deaths, total_assists,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(god_name) DO UPDATE SET
                    price          = excluded.price,
                    games_played   = excluded.games_played,
                    total_wins     = excluded.total_wins,
                    total_losses   = excluded.total_losses,
                    total_kills    = excluded.total_kills,
                    total_deaths   = excluded.total_deaths,
                    total_assists  = excluded.total_assists,
                    updated_at     = excluded.updated_at
            """, (god, fv, agg["games"],
                  agg["wins"], agg["losses"],
                  agg["kills"], agg["deaths"], agg["assists"]))

            # Seed price_history with one IPO-style entry so sparklines
            # have a starting point.
            await db.execute("""
                INSERT INTO price_history (god_name, price, event)
                VALUES (?, ?, 'replay_baseline')
            """, (god, fv))
            applied += 1

        await db.commit()

        print()
        print(f"Replay complete: applied prices for {applied} god(s).")
        print()
        print("Final prices:")
        async with db.execute("""
            SELECT god_name, price, games_played, total_wins,
                   total_losses, total_kills, total_deaths, total_assists
              FROM god_prices
             WHERE games_played > 0
             ORDER BY price DESC
        """) as cur:
            async for row in cur:
                g, p, gp, w, l, k, d, a = row
                wr = (100.0 * w / gp) if gp else 0
                print(f"  {g:20s}  {p:6.0f}  "
                      f"{gp}g  {w}-{l} ({wr:.0f}%)  "
                      f"K/D/A {k}/{d}/{a}")

        return 0

    finally:
        await db.close()
        # Clean up cffi resources.
        try:
            smite._cffi_session.close()
        except Exception:
            pass
        smite._cffi_executor.shutdown(wait=False)


def _diagnose_unparsed_match(match_id: str, detail) -> None:
    """
    Dump enough of the raw tracker.gg /matches/{id} response so we can
    see WHY parse_match_for_settlement returned None.

    The four most common causes:
      1. detail is None — the HTTP fetch failed (auth, rate limit, 404)
      2. detail['data']['segments'] missing — schema mismatch
      3. No segment with platformUserIdentifier == SMITE2_PLATFORM_ID
         — wrong platform ID, or tracker.gg wraps it differently
      4. No outcome field — tracker.gg uses a different field name
    """
    print()
    print("=" * 60)
    print(f"DIAGNOSTIC: parse failed for match {match_id}")
    print("=" * 60)
    if detail is None:
        print("detail is None — _fetch_match_detail returned no data.")
        print("Possible causes:")
        print("  * Tracker.gg blocked the request (Cloudflare hiccup)")
        print("  * Match URL pattern wrong")
        print("  * Auth required for this endpoint")
        return
    if not isinstance(detail, dict):
        print(f"detail is type {type(detail).__name__}, not dict.")
        return

    print(f"top-level keys:    {list(detail.keys())}")

    # The detail-endpoint response wraps everything in "data". Listing
    # entries don't — they have attributes/metadata/segments at the
    # top level. Auto-unwrap so the rest of this diagnostic works for
    # both shapes.
    if "data" in detail:
        data = detail.get("data")
        if data is None:
            print("'data' key is null.")
            return
    else:
        data = detail
        print("(no 'data' wrapper; treating top-level as data — "
              "this is the listing-entry shape.)")
    print(f"data type:         {type(data).__name__}")
    if isinstance(data, dict):
        print(f"data keys:         {list(data.keys())}")
        attrs = data.get("attributes", {})
        if isinstance(attrs, dict):
            print(f"attributes keys:   {list(attrs.keys())}")
            interesting = ("id", "winner", "winnerTeamId",
                           "outcome", "result", "duration",
                           "gameMode", "playlist", "startTime")
            for k in interesting:
                if k in attrs:
                    print(f"  attributes.{k} = {attrs[k]!r}")

        segments = data.get("segments", [])
        print(f"segments count:    "
              f"{len(segments) if isinstance(segments, list) else 'NOT A LIST'}")
        if isinstance(segments, list) and segments:
            print(f"first segment keys: {list(segments[0].keys())}")
            seg_attrs = segments[0].get("attributes", {})
            if isinstance(seg_attrs, dict):
                print(f"first segment attrs: {list(seg_attrs.keys())}")
                # platformUserIdentifier values across all segments
                ids = []
                for seg in segments:
                    pid = (seg.get("attributes", {})
                              .get("platformUserIdentifier"))
                    if pid:
                        ids.append(pid)
                print(f"all platformUserIdentifiers: {ids}")
                print(f"SMITE2_PLATFORM_ID configured: "
                      f"{SMITE2_PLATFORM_ID!r}")
                if SMITE2_PLATFORM_ID not in ids:
                    print(">>> NO SEGMENT MATCHES SMITE2_PLATFORM_ID <<<")
                    print("   Either wrong ID in config_local.py, or")
                    print("   tracker.gg uses a different ID format here.")

            seg_meta = segments[0].get("metadata", {})
            if isinstance(seg_meta, dict):
                print(f"first segment metadata keys: {list(seg_meta.keys())}")
                meta_interesting = ("godName", "god", "result",
                                     "outcome", "teamId", "team")
                for k in meta_interesting:
                    if k in seg_meta:
                        print(f"  metadata.{k} = {seg_meta[k]!r}")

    # Save the full response so we can inspect later if needed.
    try:
        import json
        debug_path = Path(ECONOMY_DB_PATH).parent / "replay_debug_match.json"
        debug_path.write_text(json.dumps(detail, indent=2, default=str))
        print(f"Full response saved to: {debug_path}")
    except Exception as e:
        print(f"(couldn't save debug dump: {e})")
    print("=" * 60)
    print()


async def _run_kda_migration(db: "aiosqlite.Connection"):
    """Add k/d/a columns to god_prices if missing. Same logic as
    EconomyPlugin._migrate_god_prices_kda_columns."""
    existing = set()
    async with db.execute("PRAGMA table_info(god_prices)") as cur:
        async for row in cur:
            existing.add(row[1])
    for col in ("total_kills", "total_deaths", "total_assists"):
        if col not in existing:
            await db.execute(
                f"ALTER TABLE god_prices ADD COLUMN {col} "
                f"INTEGER NOT NULL DEFAULT 0")
            print(f"Migration: added god_prices.{col}")
    await db.commit()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Reset god prices and replay tracker.gg history.")
    p.add_argument("--yes", action="store_true",
        help="Skip the 'type reset to confirm' prompt.")
    p.add_argument("--dry-run", action="store_true",
        help="List matches that would be replayed; don't wipe or write.")
    p.add_argument("--max", type=int, default=DEFAULT_MAX_MATCHES,
        metavar="N", help=f"Max matches to replay (default {DEFAULT_MAX_MATCHES}).")
    args = p.parse_args()
    return asyncio.run(cmd_replay(
        skip_confirm=args.yes,
        dry_run=args.dry_run,
        max_matches=args.max,
    ))


if __name__ == "__main__":
    sys.exit(main())
