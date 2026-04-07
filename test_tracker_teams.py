"""
Test script to explore tracker.gg live match team data.
Shows all raw team/role fields so we can understand what the API provides.

Usage:
  python test_tracker_teams.py                    — Use configured platform ID
  python test_tracker_teams.py <steam_id>         — Use a specific Steam ID
"""

import sys
import json

sys.path.insert(0, ".")
from core.config import SMITE2_PLATFORM, SMITE2_PLATFORM_ID

from curl_cffi.requests import Session as CffiSession

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://tracker.gg/smite2/",
    "Origin": "https://tracker.gg",
}


def main():
    platform_id = sys.argv[1] if len(sys.argv) > 1 else SMITE2_PLATFORM_ID
    print(f"Platform: {SMITE2_PLATFORM}")
    print(f"Player ID: {platform_id}")
    print(f"{'='*70}")

    session = CffiSession(impersonate="chrome124", headers=HEADERS)

    # Fetch live match
    url = f"https://api.tracker.gg/api/v2/smite2/standard/matches/{SMITE2_PLATFORM}/{platform_id}/live"
    print(f"\nFetching: {url}")

    resp = session.get(url)
    if resp.status_code != 200:
        print(f"ERROR: Status {resp.status_code}")
        if "been blocked" in resp.text.lower():
            print("Cloudflare blocked the request")
        else:
            print(resp.text[:500])
        return

    data = resp.json()

    if "data" not in data:
        print("No match data found — player is probably not in a game.")
        print(f"Response keys: {list(data.keys())}")
        return

    match_data = data["data"]

    # Top-level match attributes
    print(f"\n{'='*70}")
    print("MATCH ATTRIBUTES:")
    print(f"{'='*70}")
    attrs = match_data.get("attributes", {})
    for key, val in sorted(attrs.items()):
        print(f"  {key}: {val}")

    # Match metadata
    meta = match_data.get("metadata", {})
    if meta:
        print(f"\nMATCH METADATA:")
        for key, val in sorted(meta.items()):
            if isinstance(val, (str, int, float, bool)):
                print(f"  {key}: {val}")
            else:
                print(f"  {key}: {json.dumps(val, indent=4)[:200]}")

    # Player segments
    segments = match_data.get("segments", [])
    print(f"\n{'='*70}")
    print(f"PLAYERS ({len(segments)} found):")
    print(f"{'='*70}")

    for i, seg in enumerate(segments):
        seg_attrs = seg.get("attributes", {})
        seg_meta = seg.get("metadata", {})
        seg_stats = seg.get("stats", {})
        is_target = seg_attrs.get("platformUserIdentifier") == platform_id

        name = seg_meta.get("platformUserHandle", seg_attrs.get("platformUserIdentifier", "?"))
        god = seg_meta.get("godName", "Unknown")
        team = seg_meta.get("teamId", "?")

        marker = " <-- TARGET" if is_target else ""
        print(f"\n  [{i+1}] {name} — {god} (Team: {team}){marker}")

        # Show ALL metadata fields to discover role/queue info
        print(f"      Metadata keys: {sorted(seg_meta.keys())}")
        for key in sorted(seg_meta.keys()):
            val = seg_meta[key]
            if key in ("platformUserHandle", "godName", "god", "godImageUrl"):
                continue  # Already shown or not interesting
            if isinstance(val, (str, int, float, bool)):
                print(f"        {key}: {val}")
            else:
                print(f"        {key}: {json.dumps(val)[:100]}")

        # Show ALL attribute fields
        print(f"      Attribute keys: {sorted(seg_attrs.keys())}")
        for key in sorted(seg_attrs.keys()):
            val = seg_attrs[key]
            if isinstance(val, (str, int, float, bool)):
                print(f"        {key}: {val}")

        # Show stat keys (just names, not values — too noisy)
        stat_keys = sorted(seg_stats.keys())
        print(f"      Stat keys ({len(stat_keys)}): {', '.join(stat_keys[:15])}")
        if len(stat_keys) > 15:
            print(f"        ... and {len(stat_keys) - 15} more")

    # Save raw JSON for detailed inspection
    raw_file = "test_tracker_teams_raw.json"
    with open(raw_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n{'='*70}")
    print(f"Raw JSON saved to: {raw_file}")
    print("Inspect this file for the full response structure.")

    session.close()


if __name__ == "__main__":
    main()
