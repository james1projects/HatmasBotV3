"""
Seed the economy database with realistic price history for specified gods.

Fetches real god stats from tracker.gg (wins, losses, KDA per god) and
simulates match-by-match price movement using the same formulas as the
live economy plugin.

Usage:
    python tools/seed_economy.py                    # Seed all gods from profile
    python tools/seed_economy.py Ymir Geb Sylvanus  # Seed specific gods only

Requires: aiosqlite, curl_cffi
"""

import asyncio
import math
import random
import sqlite3
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import (
    ECONOMY_DB_PATH, ECONOMY_STARTING_PRICE, ECONOMY_PRICE_FLOOR,
    ECONOMY_KILL_TICK, ECONOMY_DEATH_TICK, ECONOMY_ASSIST_TICK,
    SMITE2_PLATFORM, SMITE2_PLATFORM_ID,
)

# Match-end settlement constants (same as economy.py)
WIN_BASE_MIN = 3.0
WIN_BASE_MAX = 15.0
LOSS_BASE_MIN = -5.0
LOSS_BASE_MAX = -13.0
WIN_KDA_LOW = 0.3
WIN_KDA_HIGH = 5.0
LOSS_KDA_LOW = 0.1
LOSS_KDA_HIGH = 1.2

# Volatility thresholds (same as economy.py)
def get_volatility(games_played):
    if games_played < 3:
        return 2.0, "NEW"
    elif games_played < 10:
        return 1.5, "HIGH"
    elif games_played < 25:
        return 1.0, "MEDIUM"
    else:
        return 0.7, "LOW"


def calculate_match_end_change(outcome, kills, deaths, assists, games_played):
    """Same formula as economy.py _calculate_match_end_change."""
    kda_ratio = (kills + assists * 0.5) / max(deaths, 1)

    if outcome == "win":
        t = (kda_ratio - WIN_KDA_LOW) / (WIN_KDA_HIGH - WIN_KDA_LOW)
        t = max(0.0, min(1.0, t))
        base_change = WIN_BASE_MIN + t * (WIN_BASE_MAX - WIN_BASE_MIN)
    else:
        t = (kda_ratio - LOSS_KDA_LOW) / (LOSS_KDA_HIGH - LOSS_KDA_LOW)
        t = max(0.0, min(1.0, t))
        base_change = LOSS_BASE_MAX + t * (LOSS_BASE_MIN - LOSS_BASE_MAX)

    vol_mult, _ = get_volatility(games_played)
    return base_change * vol_mult


def simulate_match_kda(outcome, avg_kills, avg_deaths, avg_assists):
    """Generate a plausible KDA for a simulated match based on averages."""
    # Add randomness (±50%) to make it feel realistic
    def jitter(avg, min_val=0):
        return max(min_val, int(avg * (0.5 + random.random())))

    k = jitter(avg_kills)
    d = jitter(avg_deaths)
    a = jitter(avg_assists)

    # Wins tend to have better KDAs, losses worse
    if outcome == "win":
        k = max(k, int(avg_kills * 0.7))
        d = min(d, int(avg_deaths * 1.3) + 1)
    else:
        k = min(k, int(avg_kills * 1.3) + 1)
        d = max(d, int(avg_deaths * 0.7))

    return k, d, a


def simulate_price_history(god_name, wins, losses, avg_kills, avg_deaths, avg_assists):
    """
    Simulate match-by-match price changes for a god.

    Returns: (final_price, games_played, total_wins, total_losses, price_history_entries)
    Each price_history_entry is (god_name, price, event, timestamp)
    """
    from datetime import datetime, timedelta, timezone

    total_games = wins + losses
    price = ECONOMY_STARTING_PRICE
    history = []

    # Price boundaries — keep things in a fun/readable range
    PRICE_CAP = 1000        # Hard ceiling
    MEAN_TARGET = 200       # Price gravitates toward this over many games
    MEAN_REVERSION = 0.02   # 2% pull toward target per match (dampens runaway prices)

    # Create match outcomes in a shuffled order
    outcomes = ["win"] * wins + ["loss"] * losses
    random.shuffle(outcomes)

    # Generate timestamps going back in time (one match per ~30 min)
    now = datetime.now(timezone.utc)
    match_duration_avg = timedelta(minutes=28)

    for i, outcome in enumerate(outcomes):
        games_so_far = i + 1
        match_time = now - (total_games - i) * match_duration_avg

        # Simulate KDA for this match
        k, d, a = simulate_match_kda(outcome, avg_kills, avg_deaths, avg_assists)

        # Record pre-match price as a history point
        history.append((god_name, round(price), "match_start", match_time.isoformat()))

        # Simulate KDA ticks during match (simplified — just net effect)
        tick_price = price
        for _ in range(k):
            tick_price *= (1 + ECONOMY_KILL_TICK)
        for _ in range(d):
            tick_price *= (1 + ECONOMY_DEATH_TICK)
            tick_price = max(tick_price, ECONOMY_PRICE_FLOOR)
        for _ in range(a):
            tick_price *= (1 + ECONOMY_ASSIST_TICK)

        # Record mid-match price (after KDA ticks)
        mid_time = match_time + timedelta(minutes=random.randint(10, 25))
        history.append((god_name, round(tick_price), "kda_tick", mid_time.isoformat()))

        # Match-end settlement
        change_pct = calculate_match_end_change(outcome, k, d, a, games_so_far)

        # Dampen settlement for seeding — cap individual match swings
        change_pct = max(-8.0, min(8.0, change_pct))

        price = tick_price * (1 + change_pct / 100)

        # Mean reversion: gently pull price toward target to prevent runaway
        if price > MEAN_TARGET:
            price -= (price - MEAN_TARGET) * MEAN_REVERSION
        elif price < MEAN_TARGET * 0.5:
            price += (MEAN_TARGET * 0.5 - price) * MEAN_REVERSION

        # Hard boundaries
        price = max(price, ECONOMY_PRICE_FLOOR)
        price = min(price, PRICE_CAP)

        # Record post-settlement price
        end_time = match_time + match_duration_avg
        history.append((god_name, round(price), f"match_{outcome}", end_time.isoformat()))

    return round(price), total_games, wins, losses, history


async def fetch_god_stats_from_tracker():
    """Fetch god stats from tracker.gg profile."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        print("[Seed] curl_cffi not installed — using hardcoded fallback stats")
        return None

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://tracker.gg/smite2/",
        "Origin": "https://tracker.gg",
    }

    url = f"https://api.tracker.gg/api/v2/smite2/standard/profile/{SMITE2_PLATFORM}/{SMITE2_PLATFORM_ID}"

    async with AsyncSession(impersonate="chrome124", headers=headers) as session:
        print(f"[Seed] Fetching profile from tracker.gg...")
        resp = await session.get(url)

        if resp.status_code != 200:
            print(f"[Seed] tracker.gg returned {resp.status_code} — using fallback stats")
            return None

        data = resp.json()
        segments = data.get("data", {}).get("segments", [])
        gods = {}

        for seg in segments:
            if seg.get("type") != "god":
                continue
            meta = seg.get("metadata", {})
            stats = seg.get("stats", {})
            name = meta.get("name")
            if not name:
                continue

            matches = int(stats.get("matchesPlayed", {}).get("value", 0))
            win_pct = float(stats.get("matchesWinPct", {}).get("value", 50))
            kills = float(stats.get("kills", {}).get("value", 0))
            deaths = float(stats.get("deaths", {}).get("value", 0))
            assists = float(stats.get("assists", {}).get("value", 0))

            if matches == 0:
                continue

            wins = round(matches * win_pct / 100)
            losses = matches - wins
            avg_k = kills / matches if matches else 3
            avg_d = deaths / matches if matches else 3
            avg_a = assists / matches if matches else 3

            gods[name] = {
                "wins": wins,
                "losses": losses,
                "avg_kills": avg_k,
                "avg_deaths": avg_d,
                "avg_assists": avg_a,
            }

        print(f"[Seed] Found {len(gods)} gods with stats")
        return gods


# Fallback stats if tracker.gg isn't available
FALLBACK_STATS = {
    "Ymir": {"wins": 16, "losses": 6, "avg_kills": 3.5, "avg_deaths": 4.2, "avg_assists": 8.5},
    "Geb": {"wins": 12, "losses": 8, "avg_kills": 1.8, "avg_deaths": 5.0, "avg_assists": 12.0},
    "Sylvanus": {"wins": 10, "losses": 5, "avg_kills": 2.0, "avg_deaths": 4.5, "avg_assists": 14.0},
}


def seed_database(gods_to_seed, force=False):
    """Write seeded data to the economy database."""
    # Ensure the data directory exists
    ECONOMY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(str(ECONOMY_DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    # Create tables if they don't exist
    db.executescript("""
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
            type      TEXT NOT NULL,
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

    for god_name, stats in gods_to_seed.items():
        # Check if god already exists
        existing = db.execute(
            "SELECT price, games_played FROM god_prices WHERE god_name = ?",
            (god_name,)
        ).fetchone()

        if existing:
            if force:
                print(f"  {god_name}: clearing existing data (was {existing[0]:.0f} hats, {existing[1]} games)")
                # Delete child rows first to satisfy foreign key constraints
                db.execute("DELETE FROM transactions WHERE god_name = ?", (god_name,))
                db.execute("DELETE FROM portfolios WHERE god_name = ?", (god_name,))
                db.execute("DELETE FROM dividends WHERE god_name = ?", (god_name,))
                db.execute("DELETE FROM price_history WHERE god_name = ?", (god_name,))
                db.execute("DELETE FROM god_prices WHERE god_name = ?", (god_name,))
            else:
                print(f"  {god_name}: already exists ({existing[0]:.0f} hats, {existing[1]} games) — SKIPPING")
                print(f"    (use --force to re-seed)")
                continue

        # Simulate price history
        final_price, games, wins, losses, history = simulate_price_history(
            god_name,
            stats["wins"], stats["losses"],
            stats["avg_kills"], stats["avg_deaths"], stats["avg_assists"]
        )

        # Insert god price
        db.execute(
            "INSERT OR REPLACE INTO god_prices (god_name, price, games_played, total_wins, total_losses) VALUES (?, ?, ?, ?, ?)",
            (god_name, final_price, games, wins, losses)
        )

        # Insert price history
        db.executemany(
            "INSERT INTO price_history (god_name, price, event, timestamp) VALUES (?, ?, ?, ?)",
            history
        )

        print(f"  {god_name}: {ECONOMY_STARTING_PRICE} → {final_price} hats "
              f"({games} games, {wins}W/{losses}L, {len(history)} price points)")

    db.commit()
    db.close()
    print(f"\n[Seed] Database saved to {ECONOMY_DB_PATH}")


async def main():
    gods_filter = [arg for arg in sys.argv[1:] if not arg.startswith("-")]

    print("=" * 50)
    print("  Hatmas Market — Economy Seeder")
    print("=" * 50)

    # Try to fetch real stats from tracker.gg
    all_stats = await fetch_god_stats_from_tracker()

    if all_stats is None:
        print("[Seed] Using fallback stats for Ymir, Geb, Sylvanus")
        all_stats = FALLBACK_STATS

    # Filter to requested gods (or all)
    if gods_filter:
        gods_to_seed = {}
        for god in gods_filter:
            # Case-insensitive match
            matched = None
            for name in all_stats:
                if name.lower() == god.lower():
                    matched = name
                    break
            if matched:
                gods_to_seed[matched] = all_stats[matched]
            else:
                print(f"  WARNING: {god} not found in stats — using defaults")
                gods_to_seed[god] = {
                    "wins": 8, "losses": 5,
                    "avg_kills": 3.0, "avg_deaths": 4.0, "avg_assists": 6.0,
                }
    else:
        gods_to_seed = all_stats

    force = "--force" in sys.argv
    print(f"\n[Seed] Seeding {len(gods_to_seed)} gods: {', '.join(gods_to_seed.keys())}")
    if force:
        print("[Seed] --force: will overwrite existing data\n")
    else:
        print()
    seed_database(gods_to_seed, force=force)


if __name__ == "__main__":
    asyncio.run(main())
