"""
plugins/smite/history.py
========================
Match-history + per-god aggregates parsing.

Two parallel paths:

  * `get_match_history(limit, max_pages)` — pulls the match LISTING
    endpoint (no per-match detail fetch). Returns reverse-chronological
    list of matches with their raw entries preserved. Used by the
    economy plugin's periodic backfill (limit=50) and the offline
    replay tool (limit=2000+, max_pages=40).

  * `get_god_aggregates(force_refresh)` — pulls the per-gamemode god
    aggregates endpoint for every mode in SMITE2_GAMEMODES_TO_TRACK,
    sums per-god values across modes, and returns a flat dict. Used
    by the replay tool to recompute every god's price under the
    current fair-value formula.

Plus two parsers that pull a settlement tuple `(god, outcome, k, d, a)`
out of either a listing entry (`parse_listing_entry`) or a per-match
detail response (`parse_match_for_settlement`). Listing parsing is
the primary path because the detail endpoint currently 401s on
?authlevel=user; detail parsing exists as a fallback if the listing
schema ever changes.
"""

from __future__ import annotations

from typing import Dict, Optional

from core.config import (
    SMITE2_PLATFORM, SMITE2_PLATFORM_ID, SMITE2_MATCH_URL,
)


def _clean_god_name(raw: Optional[str]) -> Optional[str]:
    """
    Normalize tracker.gg god names at ingestion.

    Hi-Rez stores gods internally as Unreal Engine class paths
    ("Gods.Atlas", "Gods.Sylvanus"). They maintain a display-name
    mapping that strips the "Gods." prefix on most gods before the
    API serves them — but the mapping is incomplete, and newer
    releases (Atlas, and likely future drops) leak the raw class
    path through. We strip it here at the boundary so every layer
    downstream (DB, web, OBS overlays, the YT comment scanner) sees
    one canonical name per god.

    Idempotent: a clean name (no "Gods." prefix) passes through
    untouched. Empty/None passes through as None so callers can
    keep their existing "skip if missing" branches.
    """
    if not raw:
        return raw
    name = raw.strip()
    if name.startswith("Gods."):
        name = name[len("Gods."):]
    return name or None


class _HistoryMixin:
    """
    Mixed into SmitePlugin. Calls into _TrackerClientMixin for the
    actual HTTP (`_tracker_get`, `_fetch_profile_for_mode`) and uses
    the static stat-parsing helpers (`_stat_val`) from the match-state
    mixin where available — those are class attributes, callable
    from any mixin since they all live on the same final class.
    """

    async def get_match_history(self, limit=50, max_pages=1):
        """
        Fetch the broadcaster's recent matches from tracker.gg.

        Returns a list of dicts in REVERSE-CHRONOLOGICAL order (newest
        first). Each entry includes:
            match_id (str)
            start_time (str or None)
            raw_entry (dict) — the original tracker.gg listing entry
                                with all fields preserved, so the
                                listing parser can extract god / outcome
                                / KDA without a separate detail fetch.

        Args:
          limit: max items to return total (across pages)
          max_pages: max pages to fetch. 1 for the periodic backfill path
                     (cheap, one HTTP call); larger for replay (paginate
                     up to thousands of matches).

        Used by the economy plugin's on-launch backfill (limit=50,
        max_pages=1) and the offline replay tool (limit=2000,
        max_pages=40). The endpoint URL is a best guess based on
        tracker.gg's URL conventions — if it 404s we log clearly and
        return [] so callers no-op gracefully.
        """
        base = f"{SMITE2_MATCH_URL}/{SMITE2_PLATFORM}/{SMITE2_PLATFORM_ID}"
        out = []
        next_cursor = None
        for _page in range(max_pages):
            url = base
            if next_cursor:
                # Several pagination patterns are common; tracker.gg
                # tends to use ?next=<token>. If theirs differs we'll
                # find out empirically.
                url += ("&" if "?" in url else "?") + f"next={next_cursor}"

            data = await self._tracker_get(url)
            if not data:
                if not out:
                    print(f"[Smite] get_match_history: no data from {url}")
                break

            # Tracker.gg shapes vary; try common locations for the list.
            items = data.get("data")
            if isinstance(items, dict):
                items = items.get("matches") or items.get("data")
            if not isinstance(items, list):
                items = data.get("matches")
            if not isinstance(items, list):
                print(f"[Smite] get_match_history: unexpected response, "
                      f"top-level keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
                break

            for entry in items:
                if len(out) >= limit:
                    break
                attrs = entry.get("attributes", {}) if isinstance(entry, dict) else {}
                mid = attrs.get("id") or (entry.get("id") if isinstance(entry, dict) else None)
                if not mid:
                    continue
                out.append({
                    "match_id": mid,
                    "start_time": (attrs.get("startTime")
                                   or attrs.get("start_time")),
                    "raw_entry": entry,
                })

            if len(out) >= limit:
                break

            # Look for a pagination cursor in common locations.
            meta = data.get("meta") if isinstance(data, dict) else None
            links = data.get("links") if isinstance(data, dict) else None
            next_cursor = None
            if isinstance(meta, dict):
                next_cursor = meta.get("next") or meta.get("nextCursor")
            if not next_cursor and isinstance(links, dict):
                next_cursor = links.get("next")
            if not next_cursor and isinstance(data, dict):
                next_cursor = data.get("next") or data.get("nextCursor")
            if not next_cursor:
                break  # no more pages

        return out

    @staticmethod
    def _extract_god_segments(data):
        """
        Normalize tracker.gg's response shapes for the /segments/god
        endpoint. Returns a list of segment dicts (each one a god),
        or [] if nothing usable.

        Tracker.gg's responses can be:
          {"data": [<seg>, ...]}              - flat list under data
          {"data": {"segments": [<seg>, ...]}} - wrapped (legacy /profile shape)
          [<seg>, ...]                         - bare list
          {"data": <seg>}                       - single segment under data
        """
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        d = data.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            segs = d.get("segments")
            if isinstance(segs, list):
                return segs
            # Single-segment response — treat as a 1-element list.
            if "metadata" in d or "stats" in d:
                return [d]
        return []

    async def get_god_aggregates(self, force_refresh=True):
        """
        Fetch tracker.gg's per-god lifetime stats for the broadcaster
        ACROSS EVERY GAMEMODE configured in SMITE2_GAMEMODES_TO_TRACK,
        and SUM the per-god values across modes.

        Tracker.gg's /profile endpoint only returns aggregates for one
        gamemode at a time (default ranked conquest), so calling it
        once leaves duels/arena/assault/etc. invisible. We loop over
        each tracked mode, parse the type=="god" segments, and combine.

        Returns a dict keyed on the proper-cased god name:
            {
              "Sylvanus": {
                "games":   303,   # combined across all tracked modes
                "wins":    159,
                "losses":  144,
                "kills":   3409,
                "deaths":  2425,
                "assists": 2387,
              },
              ...
            }

        Conquest (Bots) is excluded by virtue of not being in the list.
        If a mode key returns zero gods, that's logged so we can spot
        a wrong key on the next run.
        """
        from core.config import SMITE2_GAMEMODES_TO_TRACK

        combined: Dict[str, Dict[str, int]] = {}
        # Defensive dedup: if tracker.gg ever returns the same data
        # for two different mode params (or the response shape changes
        # so we can't differentiate), fingerprint and skip dupes.
        seen_fingerprints: set = set()

        for mode in SMITE2_GAMEMODES_TO_TRACK:
            try:
                data = await self._fetch_profile_for_mode(
                    mode, force_refresh=force_refresh)
            except Exception as e:
                print(f"[Smite] aggregate fetch failed for {mode}: {e}")
                continue
            if data is None:
                print(f"[Smite] {mode}: no data returned")
                continue

            # The /segments/god endpoint returns a flat list of god
            # segments. The shape can be {data: [...]}, {data: {segments: [...]}},
            # or just [...] depending on tracker.gg's mood. Normalize.
            segments = self._extract_god_segments(data)
            if not segments:
                print(f"[Smite] {mode}: no god segments in response")
                continue

            # Fingerprint = sorted (god_name, games) pairs.
            fp_parts = []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                meta = seg.get("metadata", {}) or {}
                stats = seg.get("stats", {}) or {}
                gn = _clean_god_name(
                    meta.get("name") or meta.get("godName")
                    or meta.get("god"))
                if not gn:
                    continue
                games_v = (self._stat_val(stats, "matchesPlayed")
                           or self._stat_val(stats, "matches")
                           or self._stat_val(stats, "totalGames") or 0)
                fp_parts.append((gn, int(games_v)))
            fingerprint = tuple(sorted(fp_parts))
            if fingerprint in seen_fingerprints:
                print(f"[Smite] {mode}: duplicate of prior mode, skipping")
                continue
            seen_fingerprints.add(fingerprint)

            mode_god_count = 0
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                meta = seg.get("metadata", {}) or {}
                stats = seg.get("stats", {}) or {}

                god_name = _clean_god_name(
                    meta.get("name")
                    or meta.get("godName")
                    or meta.get("god"))
                if not god_name:
                    continue

                def first_nonzero(*keys):
                    for k in keys:
                        v = self._stat_val(stats, k)
                        if v:
                            return int(v)
                    return 0

                games = first_nonzero("matchesPlayed", "matches",
                                       "totalGames")
                wins = first_nonzero("matchesWon", "wins")
                losses = first_nonzero("matchesLost", "losses")
                kills = first_nonzero("kills", "totalKills")
                deaths = first_nonzero("deaths", "totalDeaths")
                assists = first_nonzero("assists", "totalAssists")

                # KDA-as-averages heuristic (per-mode, since per-mode
                # game counts are smaller and the heuristic still
                # holds: kills < games strongly suggests average).
                if games > 5 and 0 < kills < games:
                    kills = int(kills * games)
                    deaths = int(deaths * games)
                    assists = int(assists * games)

                if games and not wins and losses:
                    wins = max(games - losses, 0)
                if games and wins and not losses:
                    losses = max(games - wins, 0)

                if games <= 0:
                    continue

                if god_name not in combined:
                    combined[god_name] = {"games": 0, "wins": 0,
                                           "losses": 0, "kills": 0,
                                           "deaths": 0, "assists": 0}
                combined[god_name]["games"] += games
                combined[god_name]["wins"] += wins
                combined[god_name]["losses"] += losses
                combined[god_name]["kills"] += kills
                combined[god_name]["deaths"] += deaths
                combined[god_name]["assists"] += assists
                mode_god_count += 1

            print(f"[Smite] {mode}: {mode_god_count} gods")

        return combined

    def parse_listing_entry(self, entry):
        """
        Parse a single match-history LISTING entry into a settlement
        tuple: (god_name, outcome, kills, deaths, assists). Returns
        None if any required field is missing. Use this BEFORE falling
        back to parse_match_for_settlement(detail) — listing entries
        already contain everything we need and avoid a per-match
        detail HTTP call.

        Tracker.gg listing schema (verified 2026-04 against a real
        match):

            entry = {
              "attributes": {"id", "gamemode", "region"},
              "metadata":   {"winningTeamId", "duration", ...},
              "segments": [
                {
                  "attributes": {"platformUserIdentifier", ...},
                  "metadata":   {"teamId", "godName", ...},
                  "stats":      {"kills": {"value": N}, "deaths": ..., "assists": ...}
                },
                ... (one per player, 10 total in conquest)
              ]
            }

        Outcome is derived: segment.metadata.teamId vs match-level
        metadata.winningTeamId. There's no per-segment 'result' field,
        which is why the prior parser kept missing.
        """
        if not isinstance(entry, dict):
            return None

        match_meta = entry.get("metadata", {}) if isinstance(entry.get("metadata"), dict) else {}
        winning_team = match_meta.get("winningTeamId")
        if not winning_team:
            return None

        segments = entry.get("segments")
        if not isinstance(segments, list):
            return None

        # Find the broadcaster's segment.
        my_seg = None
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_attrs = seg.get("attributes", {}) if isinstance(seg.get("attributes"), dict) else {}
            if seg_attrs.get("platformUserIdentifier") == SMITE2_PLATFORM_ID:
                my_seg = seg
                break
        if my_seg is None:
            return None

        seg_meta = my_seg.get("metadata", {}) if isinstance(my_seg.get("metadata"), dict) else {}
        seg_stats = my_seg.get("stats", {}) if isinstance(my_seg.get("stats"), dict) else {}

        god_name = _clean_god_name(
            seg_meta.get("godName") or seg_meta.get("god"))
        if not god_name:
            return None

        my_team = seg_meta.get("teamId")
        if not my_team:
            return None
        outcome = "win" if str(my_team) == str(winning_team) else "loss"

        kills = self._stat_val(seg_stats, "kills") or 0
        deaths = self._stat_val(seg_stats, "deaths") or 0
        assists = self._stat_val(seg_stats, "assists") or 0

        return (god_name, outcome, int(kills), int(deaths), int(assists))

    def parse_match_for_settlement(self, match_detail):
        """
        Parse a /matches/{id} response into a tuple suitable for the
        economy plugin's settle_match():
            (god_name, outcome, kills, deaths, assists)
        Returns None if the broadcaster's segment can't be found or any
        critical field is missing.

        outcome: 'win' or 'loss'. If tracker.gg reports a draw or the
        outcome can't be determined, returns None — better to skip than
        miscount a result.
        """
        if not match_detail or "data" not in match_detail:
            return None

        # Find the broadcaster's player segment using the same pattern
        # the live-match path uses.
        my_seg = None
        segments = match_detail["data"].get("segments", [])
        for seg in segments:
            attrs = seg.get("attributes", {})
            if attrs.get("platformUserIdentifier") == SMITE2_PLATFORM_ID:
                my_seg = seg
                break
        if my_seg is None:
            return None

        meta = my_seg.get("metadata", {})
        stats_raw = my_seg.get("stats", {})
        attrs = my_seg.get("attributes", {})

        god_name = _clean_god_name(meta.get("godName") or meta.get("god"))
        if not god_name:
            return None

        # Outcome — try several common locations. Tracker.gg sometimes
        # puts it on the segment metadata, sometimes on attributes,
        # sometimes derived from a winner team field on the match.
        raw_outcome = (meta.get("result")
                       or attrs.get("outcome")
                       or meta.get("outcome"))
        if not raw_outcome:
            # Fallback: compare segment.team to data.attributes.winner
            match_attrs = match_detail["data"].get("attributes", {})
            winner = match_attrs.get("winner") or match_attrs.get("winnerTeamId")
            my_team = meta.get("teamId") or attrs.get("teamId")
            if winner is not None and my_team is not None:
                raw_outcome = "win" if str(winner) == str(my_team) else "loss"

        if not raw_outcome:
            return None
        outcome_lower = str(raw_outcome).lower()
        if outcome_lower in ("win", "victory", "won"):
            outcome = "win"
        elif outcome_lower in ("loss", "defeat", "lost", "lose"):
            outcome = "loss"
        else:
            return None  # draw or unknown — skip

        kills = self._stat_val(stats_raw, "kills")
        deaths = self._stat_val(stats_raw, "deaths")
        assists = self._stat_val(stats_raw, "assists")

        return (god_name, outcome, int(kills or 0),
                int(deaths or 0), int(assists or 0))
