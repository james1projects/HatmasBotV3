"""
plugins/economy/match.py
========================
Match lifecycle hooks + backfill.

This file owns the boundary between the visual / overlay world (what
the OBS portrait matcher and HUD KDA reader see) and the canonical
economic world (what tracker.gg has confirmed). The boundary is
strict: the economy NEVER moves shares, dividends, or persisted
prices on a non-tracker.gg signal.

The smite plugin exposes two callback lists:

  * **on_god_detected** — fired by the OBS portrait matcher as soon as
    a god is identified visually. Happens in jungle practice, custom
    games, and other modes tracker.gg doesn't cover. The economy
    subscribes via `on_god_detected_visual` so the live K/D/A overlay
    can animate, but NO dividend / settlement / persisted state
    movement happens from this signal — purely cosmetic.

  * **on_match_confirmed** — fired by the tracker.gg live-API poll loop
    when a real match enters the SEARCHING → FOUND state with a real
    match_id. The economy subscribes to THIS one. Payload shape:
        {"match_id": str, "god": str, "team": str}

Match-end hooks the economy also subscribes to:

  * **on_match_end** → clear in-memory active-match state. Cosmetic
    ticks stop firing because `_match_active` flips False.
  * **on_match_result** → trigger an immediate backfill cycle. The
    backfill pulls the canonical tracker.gg listing entry (with the
    real K/D/A and outcome), then calls settle_match() exactly the
    same way the scheduled backfill does. So whether the broadcaster
    resolves the prediction manually or the 5-min loop catches it,
    the price math is identical and the KDA used is canonical.

`settle_match()` is THE single source of truth for "this match
resolved with outcome X and KDA Y, what does that do to the price?".
Idempotent via the `processed_matches` PK on match_id.

Side-effect gating (free shares, leaderboard overlay, match-end popup,
dividend catch-up) reads `_is_broadcaster_live()` at settlement time
instead of the old hardcoded `source == "live"` check. The result:
backfill picks up matches that were missed live AND fires the
celebration suite when the broadcaster is still streaming. Matches
played off-stream just update the price math silently.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional

from core.config import ECONOMY_STARTING_PRICE

from .fair_value import calculate_fair_value


class _MatchMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self._db, self._prices, self._games_played
      self._match_active, self._match_god, self._match_id,
      self._match_start_price, self._match_kda
      self.bot                  — for accessing smite plugin in backfill
    Calls into _DBMixin (_ensure_god_exists, _update_price), _DividendsMixin
    (_pay_dividend, _dividend_already_paid), _OverlaysMixin
    (_emit_overlay_event, _trigger_voiceline, _emit_leaderboard),
    _HelpersMixin (_distribute_free_shares, _is_broadcaster_live),
    _FairValueMixin (_get_volatility).
    """

    # Backfill safety cap: hard ceiling per launch (catches tracker.gg
    # API hiccups that suddenly return way too many matches).
    BACKFILL_CAP = 200
    # How many recent matches to fetch from tracker.gg in the listing call.
    BACKFILL_FETCH = 50

    async def on_god_detected_visual(self, god_info: Dict):
        """
        Fired by smite plugin on ANY god identification - both the
        portrait matcher (jungle practice / custom games / lobby
        false-positives) AND the tracker.gg poll. Pure visual: it
        arms the cosmetic state so the live K/D/A overlay can
        animate, but it does NOT pay any dividend or mark the match
        authoritative. Tracker.gg confirmation (via on_match_confirmed)
        is what unlocks the economic side effects.

        Idempotent: if we're already tracking the same god visually,
        this is a no-op so we don't reset the cosmetic KDA on every
        portrait re-fire.
        """
        if not self._db or not god_info or not god_info.get("name"):
            return
        god_name = god_info["name"]

        # Same god already visually armed - keep accumulated KDA.
        if self._match_god == god_name:
            return

        await self._ensure_god_exists(god_name)

        # Visual-only state. _match_active stays False until tracker.gg
        # confirms. _match_id stays None for the same reason - it's the
        # gate the dividend / catch-up logic key on.
        self._match_god = god_name
        self._match_start_price = self._prices[god_name]
        self._match_kda = [0, 0, 0]

        print(f"[Economy] Visual god set: {god_name} "
              f"(cosmetic base price: {self._match_start_price:.0f}) "
              f"[no dividend - awaiting tracker.gg confirmation]")

        # Animate the live overlays. Same event name as the
        # authoritative path so the overlay client doesn't care
        # which path opened the panel.
        self._emit_overlay_event("economy_god_detected", {
            "god": god_name,
            "price": self._prices[god_name],
            "volatility": self._get_volatility(god_name)[1],
        })

    async def on_match_confirmed(self, data: Dict):
        """
        Fired by smite plugin when tracker.gg confirms a real match
        (SEARCHING → FOUND with a real match_id). THIS is the only
        authoritative match-start signal the economy listens to.

        Captures the pre-match price as the basis for cosmetic ticks,
        arms _match_active so the kill-detector hooks animate
        overlays, and pays the start-of-match 5% dividend (if the
        broadcaster is live). Dividend records the match_id so the
        backfill catch-up path can avoid double-paying.

        Payload: {"match_id": str, "god": str, "team": str}
        """
        if not self._db or not data:
            return
        match_id = data.get("match_id")
        god_name = data.get("god")
        if not god_name or not match_id:
            # Tracker.gg should always give us both. Defensive bail.
            return

        # Ignore duplicates within the same match. Match_id is the
        # canonical key, so this is safer than comparing god names
        # (visual-only state may have armed _match_god already to
        # the same god). We promote visual -> authoritative on the
        # first tracker.gg confirmation.
        if self._match_active and self._match_id == match_id:
            print(f"[Economy] Ignoring duplicate match confirmation for {match_id}")
            return

        await self._ensure_god_exists(god_name)

        self._match_active = True
        self._match_god = god_name
        self._match_id = match_id
        self._match_start_price = self._prices[god_name]
        self._match_kda = [0, 0, 0]

        print(f"[Economy] Match confirmed by tracker.gg: {god_name} "
              f"(match_id={match_id}, price={self._match_start_price:.0f})")

        # Pay 5% dividend to all holders. Gated on broadcaster_live
        # so a Smite session played offline doesn't pay viewers who
        # aren't watching. match_id stamped on the row so backfill
        # won't catch-up double-pay if it later picks up this same
        # match.
        if self._is_broadcaster_live():
            await self._pay_dividend(god_name, match_id=match_id)
        else:
            print(f"[Economy] Skipping start dividend for {god_name} — "
                  f"broadcaster not live on Twitch")

        # Emit economy event for overlays (animates regardless of live)
        self._emit_overlay_event("economy_god_detected", {
            "god": god_name,
            "price": self._prices[god_name],
            "volatility": self._get_volatility(god_name)[1],
        })

    async def on_match_end(self, data: Dict):
        """
        Match ended — stop cosmetic ticking. Actual settlement (W/L
        price change) is driven by tracker.gg via on_match_result or
        the scheduled backfill loop, NOT by this callback.

        Also called by SmitePlugin.force_end_match when the kill
        detector sees non-gameplay (lobby / results / menus) before
        tracker.gg drops the live match. Either way: cosmetic ticks
        stop here. Settlement waits for tracker.gg.
        """
        if not self._match_god:
            return  # nothing to clear

        print(f"[Economy] Match ended for {self._match_god} "
              f"(cosmetic KDA: "
              f"{self._match_kda[0]}/{self._match_kda[1]}/{self._match_kda[2]})")

        was_authoritative = self._match_active
        ending_god = self._match_god
        ending_match_id = self._match_id   # capture before any clear
        self._match_active = False

        # Schedule a one-shot backfill 30s from now for authoritative
        # matches. Gives tracker.gg time to publish the match listing,
        # then settles via the same path the 5-min scheduled loop and
        # on_match_result use. Idempotent via processed_matches dedup,
        # so a manual prediction-resolve in the dashboard between now
        # and the 30s mark just no-ops here.
        if was_authoritative and ending_match_id:
            asyncio.create_task(
                self._delayed_post_match_backfill(ending_match_id)
            )

        # Hide the live K/D/A overlay on EVERY match-end, regardless
        # of mode. For visual-only sessions (jungle / custom), this is
        # the only hide-trigger. For authoritative matches, settle_match
        # later emits match_end_economy (which also hides the live
        # overlay per overlay_rules.json AND shows the settlement
        # celebration), but we don't want to wait 30s for the overlay
        # to disappear - the kill-detector force-end already cleared
        # the OBS god portrait, so the live panel should follow suit.
        self._emit_overlay_event("economy_visual_end", {
            "god": ending_god,
        })

        # Clear cosmetic state ONLY for visual-only sessions. Real
        # matches keep state around so settle_match (run via backfill)
        # has the god name + match_id for the settlement record.
        if not was_authoritative or not self._match_id:
            self._match_god = None
            self._match_id = None
            self._match_start_price = 0.0
            self._match_kda = [0, 0, 0]

    async def on_match_result(self, data: Dict):
        """
        Broadcaster resolved the Twitch prediction (or auto-resolver
        fired). Trigger an immediate backfill cycle to settle the
        match using tracker.gg's canonical KDA + outcome. If
        tracker.gg already has the match listing, settlement (and
        the celebration overlays + free shares) fire now. If not
        (the 2-5 min lag), the scheduled 5-min backfill loop will
        catch it.

        This intentionally does NOT use self._match_kda from the
        kill detector — that's a cosmetic counter only. The
        authoritative KDA comes from tracker.gg via the backfill
        path's parser.
        """
        if not self._db:
            return

        # Clear live state — the match is done as far as overlays
        # are concerned. Settlement happens through backfill which
        # is keyed on match_id from processed_matches dedup.
        match_id = self._match_id
        god_name = self._match_god
        self._match_god = None
        self._match_id = None
        self._match_start_price = 0.0
        self._match_kda = [0, 0, 0]

        print(f"[Economy] on_match_result for {god_name} "
              f"(match_id={match_id}) — triggering immediate backfill")

        # Kick off an immediate backfill. Idempotent — if the
        # scheduled loop also fires concurrently, dedup handles it.
        try:
            await self.backfill_recent_matches()
        except Exception as e:
            print(f"[Economy] on_match_result backfill error: {e}")

    async def settle_match(self, match_id: Optional[str], god_name: str,
                           outcome: str, kills: int, deaths: int, assists: int,
                           *, source: str = "live",
                           match_start_price: Optional[float] = None) -> bool:
        """
        Apply the canonical match-end settlement to god prices.

        This is THE single source of truth for "a match resolved with
        outcome X and KDA Y, what does that do to the price?". Both
        the live path (via on_match_result → backfill kick) and the
        scheduled offline backfill call this. Identical formula →
        identical price change for the same input regardless of how
        the match got here.

        Returns True if settlement actually ran, False if it was
        deduped (already processed) or refused (missing args).

        Side-effects (match-end overlay popup, free-share distribution
        to current chatters, leaderboard refresh, catch-up dividend)
        are gated on `_is_broadcaster_live()` at settlement time. If
        the broadcaster is live, they fire — whether settlement came
        through the immediate-backfill kick from on_match_result or
        through the scheduled 5-min loop. Off-stream settlements run
        the price math silently.
        """
        if not self._db:
            return False
        if not outcome or not god_name:
            return False
        if not match_id:
            # No match_id → no dedup → no settlement. Both paths now
            # require a real tracker.gg match_id. (Used to log a
            # warning and proceed on the live side — that path is
            # gone in the airtight-economy pass.)
            print(f"[Economy] settle_match({source}): refusing to settle "
                  f"without match_id ({god_name} {outcome})")
            return False

        # Dedup against processed_matches.
        async with self._db.execute(
            "SELECT 1 FROM processed_matches WHERE match_id = ?",
            (match_id,)
        ) as cur:
            if await cur.fetchone():
                print(f"[Economy] settle_match: match {match_id} already "
                      f"processed, skipping ({source})")
                return False

        # Establish the basis price. If the caller supplied a
        # pre-match snapshot (the live path passes _match_start_price
        # before on_match_confirmed wiped it), use that. Otherwise
        # fall back to current cached price — cosmetic ticks never
        # persist, so the cached price IS the pre-match price under
        # the new design.
        if match_start_price is None or match_start_price <= 0:
            match_start_price = self._prices.get(
                god_name, ECONOMY_STARTING_PRICE)

        # Make sure the god has a god_prices row (first-ever match for
        # this god might be a backfill of a never-priced god).
        await self._ensure_god_exists(god_name)

        # Update aggregates atomically — the new fair-value formula
        # derives price from these running totals.
        win_inc = 1 if outcome == "win" else 0
        loss_inc = 1 if outcome == "loss" else 0
        await self._db.execute("""
            UPDATE god_prices SET
                games_played   = games_played + 1,
                total_wins     = total_wins   + ?,
                total_losses   = total_losses + ?,
                total_kills    = total_kills  + ?,
                total_deaths   = total_deaths + ?,
                total_assists  = total_assists + ?
            WHERE god_name = ?
        """, (win_inc, loss_inc, kills, deaths, assists, god_name))
        self._games_played[god_name] = self._games_played.get(god_name, 0) + 1

        # Read back updated totals and compute the new fair-value price.
        async with self._db.execute("""
            SELECT total_wins, total_losses,
                   total_kills, total_deaths, total_assists
              FROM god_prices WHERE god_name = ?
        """, (god_name,)) as cur:
            row = await cur.fetchone()
        if row is None:
            # Shouldn't happen post-_ensure_god_exists, but be defensive.
            return False
        agg_wins, agg_losses, agg_k, agg_d, agg_a = row

        settlement_price = calculate_fair_value(
            agg_wins, agg_losses, agg_k, agg_d, agg_a)

        await self._update_price(
            god_name, settlement_price, event=f"match_{outcome}_{source}")

        actual_change_pct = (
            ((settlement_price - match_start_price) / match_start_price) * 100.0
            if match_start_price > 0 else 0.0
        )

        # Is the broadcaster currently streaming? Single decision
        # point for ALL celebration side-effects below.
        is_live = self._is_broadcaster_live()

        # Record in processed_matches for dedup. match_id is now
        # mandatory (checked above), so the row always writes.
        await self._db.execute("""
            INSERT INTO processed_matches
                (match_id, god_name, outcome, kills, deaths, assists,
                 price_change, source, was_live_at_settle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (match_id, god_name, outcome, kills, deaths, assists,
              actual_change_pct, source, 1 if is_live else 0))

        await self._db.commit()

        tag = "[live]" if source == "live" else "[backfill]"
        live_tag = " [streaming]" if is_live else " [off-stream]"
        print(f"[Economy] {tag}{live_tag} {god_name} {outcome.upper()}: "
              f"{match_start_price:.0f} → {settlement_price:.0f} "
              f"({actual_change_pct:+.1f}%) "
              f"[KDA {kills}/{deaths}/{assists}] "
              f"match {match_id}")

        # ─── Celebration side-effects (broadcaster-live gated) ──────
        # Fire whenever the broadcaster is currently live, regardless
        # of whether this settlement came through the immediate kick
        # from on_match_result or the scheduled 5-min backfill loop.
        # Off-stream settlements skip all of these — they just update
        # the price math.
        if is_live:
            # Catch-up dividend: if no dividend was ever paid for this
            # match_id (e.g., bot was offline at match-start, or
            # broadcaster wasn't live then), pay it now. Normal case
            # (live path already paid at on_match_confirmed) skips
            # this — the SELECT finds the existing row.
            if not await self._dividend_already_paid(match_id):
                print(f"[Economy] No prior dividend recorded for match "
                      f"{match_id} — paying catch-up dividend now")
                await self._pay_dividend(god_name, match_id=match_id)

            free_share_info = await self._distribute_free_shares(god_name)

            _vol_mult, vol_tier = self._get_volatility(god_name)
            self._emit_overlay_event("match_end_economy", {
                "god": god_name,
                "outcome": outcome,
                "kda": [kills, deaths, assists],
                "old_price": round(match_start_price),
                "new_price": round(settlement_price),
                "change_pct": round(actual_change_pct, 1),
                "volatility_tier": vol_tier,
                "free_shares": free_share_info,
                "games_played": self._games_played[god_name],
            })

            # VGS: "Awesome!" on win, "That's too bad" on loss
            # (no-op behind ECONOMY_VOICELINES_ENABLED for now)
            self._trigger_voiceline(outcome, god_name)

            # Emit leaderboard update
            await self._emit_leaderboard()

        return True

    async def _delayed_post_match_backfill(self, match_id: str):
        """
        Wait SMITE2_BACKFILL_POST_MATCH_DELAY seconds (default 30) and
        then trigger a backfill cycle. Scheduled from on_match_end when
        a tracker.gg-confirmed match ends. The wait gives tracker.gg
        time to publish the match listing in their /matches endpoint.

        If the broadcaster resolves the Twitch prediction (which fires
        on_match_result -> immediate backfill) before our 30s timer
        elapses, this still runs at T+30 but is a silent no-op thanks
        to processed_matches dedup. Same if the scheduled 5-min loop
        beats us to it.
        """
        from core.config import SMITE2_BACKFILL_POST_MATCH_DELAY
        try:
            await asyncio.sleep(SMITE2_BACKFILL_POST_MATCH_DELAY)
        except asyncio.CancelledError:
            return
        try:
            print(f'[Economy] Post-match backfill firing '
                  f'(T+{SMITE2_BACKFILL_POST_MATCH_DELAY}s after match {match_id})')
            await self.backfill_recent_matches()
        except Exception as e:
            print(f'[Economy] Post-match backfill error: {e}')

    # ══════════════════════════════════════════════════════════════════════
    #  PERIODIC MATCH BACKFILL
    # ══════════════════════════════════════════════════════════════════════
    #
    # Background task started in EconomyPlugin.on_ready. Every
    # SMITE2_BACKFILL_INTERVAL seconds, asks smite for the broadcaster's
    # recent match history and settles any match that isn't already in
    # processed_matches. Bootstrap on first run: writes the latest
    # match's id as a marker without settling, so we don't retroactively
    # process pre-feature matches and explode the price model.
    #
    # The settlement runs through the same settle_match() that the live
    # path uses, with source='backfill' so live-only side-effects
    # (free shares, overlays, leaderboard, voicelines) are skipped.
    # Match math is identical in both paths.

    async def backfill_recent_matches(self) -> Dict[str, int]:
        """
        Settle any tracker.gg matches we haven't seen yet.

        Returns a summary dict: {fetched, new, settled, skipped, errors}
        for visibility/diagnostics.
        """
        summary = {"fetched": 0, "new": 0, "settled": 0,
                   "skipped": 0, "errors": 0, "bootstrap": False}

        if not self._db or not self.bot:
            return summary

        smite_plugin = self.bot.plugins.get("smite")
        if not smite_plugin or not hasattr(smite_plugin, "get_match_history"):
            print("[Economy] backfill: smite plugin missing or "
                  "doesn't expose get_match_history — skipping")
            return summary

        # Pull listing.
        history = await smite_plugin.get_match_history(
            limit=self.BACKFILL_FETCH)
        summary["fetched"] = len(history)
        if not history:
            print("[Economy] backfill: no matches returned from tracker.gg")
            return summary

        # Determine which match_ids we've already processed.
        seen_ids = set()
        async with self._db.execute(
                "SELECT match_id FROM processed_matches") as cur:
            async for row in cur:
                seen_ids.add(row[0])

        new_matches = [m for m in history if m["match_id"] not in seen_ids]
        summary["new"] = len(new_matches)

        # ─── Bootstrap path: first run, nothing seen yet ────────────────
        # Write only the LATEST match as a marker so we don't reach back
        # in time and settle every match the broadcaster ever played.
        # Future launches will only see matches AFTER the marker.
        if not seen_ids:
            latest = history[0]  # tracker.gg returns newest-first
            await self._db.execute