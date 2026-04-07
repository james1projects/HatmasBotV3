"""
Quick test script for tracker.gg Smite 2 API endpoints.
Run this while your friend is in a live match to verify the API works.

Uses curl_cffi to bypass Cloudflare's TLS fingerprinting.
Install: pip install curl_cffi

Usage: python test_tracker.py
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")
from core.config import SMITE2_PLATFORM, SMITE2_PLATFORM_ID

BASE = "https://api.tracker.gg/api/v2/smite2/standard"

STEAM_ID = SMITE2_PLATFORM_ID
PLATFORM = SMITE2_PLATFORM

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://tracker.gg/smite2/",
    "Origin": "https://tracker.gg",
}


def make_session():
    """Create a curl_cffi session that impersonates Chrome."""
    from curl_cffi.requests import AsyncSession
    return AsyncSession(
        impersonate="chrome124",
        headers=HEADERS,
    )


async def test_profile(session):
    url = f"{BASE}/profile/{PLATFORM}/{STEAM_ID}"
    print(f"\n{'='*60}")
    print(f"TEST 1: Profile")
    print(f"GET {url}")
    print(f"{'='*60}")

    resp = await session.get(url)
    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        meta = data.get("data", {}).get("metadata", {})
        print(f"Player: {meta.get('platformUserHandle', '?')}")
        print(f"Live match: {meta.get('liveMatch', False)}")

        segments = data.get("data", {}).get("segments", [])
        gods = [s for s in segments if s.get("type") == "god"]
        gamemodes = [s for s in segments if s.get("type") == "gamemode"]
        print(f"God stats: {len(gods)} gods")
        print(f"Gamemode stats: {len(gamemodes)} modes")

        if gods:
            top_god = max(gods, key=lambda g: g.get("stats", {}).get("matchesPlayed", {}).get("value", 0))
            print(f"Most played: {top_god.get('metadata', {}).get('name', '?')} "
                  f"({top_god.get('stats', {}).get('matchesPlayed', {}).get('displayValue', '?')} games)")

        print("\n  PROFILE OK")
        return True
    else:
        print(f"Error body: {resp.text[:500]}")
        return False


async def test_summary(session):
    url = f"{BASE}/profile/{PLATFORM}/{STEAM_ID}/summary"
    print(f"\n{'='*60}")
    print(f"TEST 2: Summary")
    print(f"GET {url}")
    print(f"{'='*60}")

    resp = await session.get(url)
    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(json.dumps(data, indent=2)[:1000])
        print("\n  SUMMARY OK")
        return True
    else:
        print(f"Error body: {resp.text[:500]}")
        return False


async def test_live_match(session):
    url = f"{BASE}/matches/{PLATFORM}/{STEAM_ID}/live"
    print(f"\n{'='*60}")
    print(f"TEST 3: Live Match")
    print(f"GET {url}")
    print(f"{'='*60}")

    resp = await session.get(url)
    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()

        match_attrs = data.get("data", {}).get("attributes", {})
        match_meta = data.get("data", {}).get("metadata", {})
        print(f"Match ID: {match_attrs.get('id', 'N/A')}")
        print(f"Duration: {match_meta.get('duration', 0)}s")
        print(f"Snapshot: {match_meta.get('snapshotTimestamp', 'N/A')}")

        segments = data.get("data", {}).get("segments", [])
        print(f"\nPlayers ({len(segments)}):")
        print(f"{'Name':<20} {'God':<15} {'Team':<8} {'K/D/A':<12} {'SR'}")
        print("-" * 65)

        for seg in segments:
            meta = seg.get("metadata", {})
            attrs = seg.get("attributes", {})
            stats = seg.get("stats", {})

            name = meta.get("platformUserHandle",
                             attrs.get("platformUserIdentifier", "?"))
            god = meta.get("godName", meta.get("god", "---"))
            team = meta.get("teamId", "?")
            kills = stats.get("kills", {}).get("value", "-")
            deaths = stats.get("deaths", {}).get("value", "-")
            assists = stats.get("assists", {}).get("value", "-")
            sr = stats.get("skillRating", {}).get("displayValue", "?")

            is_me = " <-- TARGET" if attrs.get("platformUserIdentifier") == STEAM_ID else ""
            kda = f"{kills}/{deaths}/{assists}"

            print(f"{name:<20} {god:<15} {team:<8} {kda:<12} {sr}{is_me}")

        my_seg = None
        for seg in segments:
            if seg.get("attributes", {}).get("platformUserIdentifier") == STEAM_ID:
                my_seg = seg
                break

        if my_seg:
            my_god = my_seg.get("metadata", {}).get("godName")
            if my_god:
                print(f"\n  GOD DETECTED: {my_god}")
            else:
                print(f"\n  God not yet available (match may still be in picks/bans)")
                print(f"  Try again in 1-2 minutes")

        print("\n  LIVE MATCH OK")
        return True

    elif resp.status_code == 404:
        print("Not in a match (404 — expected when not in game)")
        return True

    else:
        print(f"Error body: {resp.text[:500]}")
        if resp.status_code == 403:
            print("\n  Still getting 403 — Cloudflare may have updated their checks")
        return False


async def main():
    print(f"Testing tracker.gg API for Steam ID: {STEAM_ID}")
    print(f"Platform: {PLATFORM}")

    # Check for curl_cffi
    try:
        from curl_cffi.requests import AsyncSession
        print("Using: curl_cffi (Chrome TLS impersonation)")
    except ImportError:
        print("\ncurl_cffi not installed! Install it with:")
        print("  pip install curl_cffi")
        print("\nThis is required to bypass Cloudflare's bot detection.")
        return

    session = make_session()
    results = {}

    try:
        results["profile"] = await test_profile(session)
        results["summary"] = await test_summary(session)
        results["live_match"] = await test_live_match(session)
    finally:
        await session.close()

    print(f"\n{'='*60}")
    print("RESULTS:")
    print(f"{'='*60}")
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  {name:<15} {status}")

    if all(results.values()):
        print("\nAll tests passed! The bot should work correctly.")
    else:
        print("\nSome tests failed — check the output above for details.")


if __name__ == "__main__":
    asyncio.run(main())
