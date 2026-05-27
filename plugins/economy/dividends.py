"""
plugins/economy/dividends.py
============================
Dividend payouts on god detection.

When a match starts (god detected), holders of that god's shares get
a 5% dividend. Two payment paths because the audience is split across
two systems:

  * **Twitch holders** - paid in Hats credited via the MixItUp Dev API.
    Their share count stays the same; they get hats deposited.

  * **YouTube holders** - compound the same dividend rate as fractional
    bonus shares of the same god. They aren't in MixItUp's user
    system (no Twitch account), so a 5% dividend becomes "your
    position grew by 5%". Mathematically equivalent to the Twitch
    payout: hats_due / current_price = bonus_shares.

Excluded users (the bot itself, StreamElements, Nightbot, etc.) are
filtered at the SQL WHERE clause so legacy rows for those accounts
don't accidentally pay out.

Post-airtight-economy pass: dividends record the tracker.gg match_id
on the row so the backfill catch-up path can avoid double-paying.
`_dividend_already_paid(match_id)` is the dedup check.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from core.config import ECONOMY_DIVIDEND_RATE

from .helpers import EXCLUDED_USERS_LOWER


class _DividendsMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self._db            - for portfolios + youtube_holdings + dividends
      self._prices        - dividend amount = price * rate
      self._last_dividend - cached for the !dividend command + overlay
    Calls _MixItUpMixin (_adjust_balance) to credit Twitch holders,
    _OverlaysMixin (_emit_overlay_event, _trigger_voiceline) for the popup.
    """

    async def _dividend_already_paid(self, match_id: Optional[str]) -> bool:
        """
        Has a dividend been recorded for this match_id already?

        Used by `settle_match` to decide whether backfill should fire
        a catch-up dividend. The normal case is: live path paid the
        dividend at match-start when tracker.gg confirmed -> row exists
        -> backfill skips the dividend and just settles the price math.

        Returns False if `match_id` is None or empty (can't dedup
        without a key, so we assume not paid - safer for catch-up).
        Pre-feature dividend rows have NULL match_id; those are
        invisible to this lookup and won't block a new dividend on a
        different match.
        """
        if not match_id or not self._db:
            return False
        async with self._db.execute(
                "SELECT 1 FROM dividends WHERE match_id = ? LIMIT 1",
                (match_id,)) as cur:
            return await cur.fetchone() is not None

    async def _pay_dividend(self, god_name: str,
                            *, match_id: Optional[str] = None):
        """Pay 5% dividend to all holders of a god's shares.

        Twitch holders receive Hats credited via the MixItUp Dev API.
        YouTube holders compound the same dividend rate as fractional
        bonus shares of the same god - they have no Hats to credit
        because they aren't in MixItUp's user system, so the dividend
        becomes "your position grew by ECONOMY_DIVIDEND_RATE percent."
        Mathematically equivalent: hats_due / current_price = bonus_shares
        which simplifies to old_shares * ECONOMY_DIVIDEND_RATE.

        `match_id` (when provided) is recorded on the dividends row so
        the backfill catch-up path can detect "this dividend was
        already paid live" and skip a re-pay. Live callers pass the
        tracker.gg match_id from `on_match_confirmed`. Backfill catch-up
        passes the same match_id it's settling. Simulator passes a
        synthetic 'sim-...' id.
        """
        price = self._prices.get(god_name, 0)
        if price <= 0:
            return

        dividend_per_share = price * ECONOMY_DIVIDEND_RATE

        # --- Twitch holders: pay in Hats ----------------------------------
        # SQL filters out excluded users (StreamElements, bots, etc.)
        # at the WHERE clause so even legacy rows for those accounts
        # are silently skipped.
        holders = []
        total_hats = 0
        excluded_list = list(EXCLUDED_USERS_LOWER)
        if excluded_list:
            placeholders = ",".join("?" for _ in excluded_list)
            sql = (f"SELECT username, shares FROM portfolios "
                   f"WHERE god_name = ? AND shares > 0.001 "
                   f"AND LOWER(username) NOT IN ({placeholders})")
            params = (god_name,) + tuple(excluded_list)
        else:
            sql = ("SELECT username, shares FROM portfolios "
                   "WHERE god_name = ? AND shares > 0.001")
            params = (god_name,)
        async with self._db.execute(sql, params) as cursor:
            async for row in cursor:
                username, shares = row
                payout = int(shares * dividend_per_share)
                if payout > 0:
                    holders.append((username, payout))
                    total_hats += payout

        # Twitch payouts (skipped silently if no Twitch holders).
        for username, payout in holders:
            await self._adjust_balance(username, payout)
            await self._db.execute("""
                INSERT INTO transactions (username, god_name, type, shares, price, total, fee)
                VALUES (?, ?, 'dividend', 0, ?, ?, 0)
            """, (username, god_name, price, payout))

        # --- YouTube holders: pay in fractional bonus shares --------------
        yt_holders, yt_bonus_total = await self._pay_youtube_dividend(
            god_name, price)

        # Bail only if BOTH sides are empty.
        if not holders and yt_holders == 0:
            print(f"[Economy] No holders for {god_name} dividend")
            return

        # Record the dividend event (covers both sides - total_hats is
        # the Twitch hat payout; YouTube side recorded in
        # youtube_transactions for history). match_id is recorded for
        # dedup against the backfill catch-up path.
        await self._db.execute("""
            INSERT INTO dividends
                (god_name, rate, price, total_hats, holders, match_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (god_name, ECONOMY_DIVIDEND_RATE, price,
              total_hats, len(holders) + yt_holders, match_id))
        await self._db.commit()

        self._last_dividend = {
            "god_name": god_name,
            "rate": ECONOMY_DIVIDEND_RATE,
            "price": price,
            "per_share": dividend_per_share,
            "total_hats": total_hats,
            "holders": len(holders),
            "yt_holders": yt_holders,
            "yt_bonus_shares": yt_bonus_total,
            "match_id": match_id,
            "timestamp": datetime.now().isoformat(),
        }

        print(f"[Economy] Dividend: {god_name} - "
              f"{len(holders)} Twitch holders ({total_hats:,} hats), "
              f"{yt_holders} YouTube holders ({yt_bonus_total:.3f} bonus shares), "
              f"{ECONOMY_DIVIDEND_RATE*100:.0f}%")

        # Emit overlay event
        self._emit_overlay_event("dividend_paid", self._last_dividend)

        # VGS: "You Rock!" on dividend
        self._trigger_voiceline("dividend", god_name)

    async def _pay_youtube_dividend(self, god_name: str, price: float
                                    ) -> Tuple[int, float]:
        """
        Compound a per-share dividend onto every YouTube holder of
        `god_name` as additional fractional shares.

        Each holder's new position becomes:
            new_shares  = old_shares * (1 + ECONOMY_DIVIDEND_RATE)
            new_avg_cost = old_avg_cost / (1 + ECONOMY_DIVIDEND_RATE)
        The avg_cost adjustment keeps total cost basis constant - the
        holder didn't pay anything for the bonus shares, so their
        per-share cost basis decreases proportionally.

        Returns (num_holders, total_bonus_shares_distributed).
        """
        rate = ECONOMY_DIVIDEND_RATE
        scale = 1.0 + rate

        rows: List[Tuple[str, float, float]] = []
        async with self._db.execute("""
            SELECT yt_channel_id, shares, avg_cost
              FROM youtube_holdings
             WHERE god_name = ? AND shares > 0.001
        """, (god_name,)) as cursor:
            async for row in cursor:
                rows.append((row[0], float(row[1]), float(row[2])))

        if not rows:
            return 0, 0.0

        bonus_total = 0.0
        for channel_id, old_shares, old_avg in rows:
            bonus = old_shares * rate
            new_shares = old_shares * scale
            new_avg = old_avg / scale if scale > 0 else 0.0

            await self._db.execute("""
                UPDATE youtube_holdings
                   SET shares = ?, avg_cost = ?
                 WHERE yt_channel_id = ? AND god_name = ?
            """, (new_shares, new_avg, channel_id, god_name))

            await self._db.execute("""
                INSERT INTO youtube_transactions
                    (yt_channel_id, god_name, type, shares, price)
                VALUES (?, ?, 'dividend_share', ?, ?)
            """, (channel_id, god_name, bonus, price))

            bonus_total += bonus

        return len(rows), bonus_total
