"""
plugins/economy/dividends.py
============================
Dividend payouts on god detection.

When a match starts (god detected), holders of that god's shares get
a 5% dividend. Holders are user_uuids (core/users.py); how each one is
paid depends on whether they can receive Hats yet:

  * **Holders with a Twitch login** - paid in Hats credited via the
    MixItUp Dev API. Their share count stays the same; they get hats
    deposited.

  * **Holders without one (YouTube-only viewers)** - compound the same
    dividend rate as fractional bonus shares of the same god. MixItUp
    has no account for them, so a 5% dividend becomes "your position
    grew by 5%". Mathematically equivalent to the Hats payout:
    hats_due / current_price = bonus_shares. Once the local wallet
    (docs/WALLET_PLAN.md) replaces MixItUp this branch goes away and
    everyone is paid in Hats.

Excluded users (the bot itself, StreamElements, Nightbot, etc.) are
filtered at the SQL WHERE clause by uuid so legacy rows for those
accounts don't accidentally pay out.

Post-airtight-economy pass: dividends record the tracker.gg match_id
on the row so the backfill catch-up path can avoid double-paying.
`_dividend_already_paid(match_id)` is the dedup check.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from core.config import ECONOMY_DIVIDEND_RATE
from core import users as _users


class _DividendsMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self._db            - for portfolios + dividends
      self._prices        - dividend amount = price * rate
      self._last_dividend - cached for the !dividend command + overlay
    Calls _MixItUpMixin (_adjust_balance) to credit Hats holders,
    _OverlaysMixin (_emit_overlay_event, _trigger_voiceline) for the popup,
    _HelpersMixin (_excluded_uuids).
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

        excluded = await self._excluded_uuids()
        not_excl, params = _users.sql_not_excluded("user_uuid", excluded)
        holders: List[Tuple[str, float]] = []
        async with self._db.execute(
                f"SELECT user_uuid, shares FROM portfolios "
                f"WHERE god_name = ? AND shares > 0.001{not_excl}",
                (god_name,) + params) as cursor:
            async for row in cursor:
                holders.append((row[0], float(row[1])))

        # Only ledger payouts MixItUp actually accepted - a failed
        # credit must not show up in the books as paid. Holders who
        # cannot hold Hats yet (no Twitch login) get bonus shares.
        paid_holders = []
        paid_hats = 0
        share_holders = 0
        bonus_total = 0.0
        attempted_hats = 0
        for user_uuid, shares in holders:
            login = await self._twitch_login_for(user_uuid) \
                if hasattr(self, "_twitch_login_for") else None
            if not login:
                bonus = await self._pay_bonus_shares(
                    user_uuid, god_name, shares, price)
                if bonus > 0:
                    share_holders += 1
                    bonus_total += bonus
                continue
            payout = int(shares * dividend_per_share)
            if payout <= 0:
                continue
            attempted_hats += payout
            ok = await self._adjust_balance(user_uuid, payout)
            if not ok:
                print(f"[Economy] Dividend credit FAILED for {login} "
                      f"({payout} hats, {god_name}) - not recorded")
                continue
            paid_holders.append((user_uuid, payout))
            paid_hats += payout
            await self._db.execute("""
                INSERT INTO transactions (user_uuid, god_name, type, shares, price, total, fee)
                VALUES (?, ?, 'dividend', 0, ?, ?, 0)
            """, (user_uuid, god_name, price, payout))

        # Bail if nothing was actually paid on either side. Also skips
        # writing the dividends row, so the match_id stays unclaimed and
        # the settle-time catch-up can retry (e.g. MixItUp was down).
        if not paid_holders and share_holders == 0:
            if attempted_hats:
                print(f"[Economy] Dividend for {god_name}: every MixItUp "
                      f"credit failed - leaving match unclaimed for catch-up")
            else:
                print(f"[Economy] No holders for {god_name} dividend")
            await self._db.commit()
            return

        # Record the dividend event (covers both sides - total_hats is
        # the Hats payout; bonus shares are in transactions as
        # 'dividend_share'). match_id is recorded for dedup against the
        # backfill catch-up path.
        await self._db.execute("""
            INSERT INTO dividends
                (god_name, rate, price, total_hats, holders, match_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (god_name, ECONOMY_DIVIDEND_RATE, price,
              paid_hats, len(paid_holders) + share_holders, match_id))
        await self._db.commit()

        self._last_dividend = {
            "god_name": god_name,
            "rate": ECONOMY_DIVIDEND_RATE,
            "price": price,
            "per_share": dividend_per_share,
            "total_hats": paid_hats,
            "holders": len(paid_holders),
            "yt_holders": share_holders,
            "yt_bonus_shares": bonus_total,
            "match_id": match_id,
            "timestamp": datetime.now().isoformat(),
        }

        print(f"[Economy] Dividend: {god_name} - "
              f"{len(paid_holders)} Hats holders ({paid_hats:,} hats), "
              f"{share_holders} share-only holders ({bonus_total:.3f} bonus shares), "
              f"{ECONOMY_DIVIDEND_RATE*100:.0f}%")

        # Emit overlay event
        self._emit_overlay_event("dividend_paid", self._last_dividend)

        # VGS: "You Rock!" on dividend
        self._trigger_voiceline("dividend", god_name)

    async def _pay_bonus_shares(self, user_uuid: str, god_name: str,
                                old_shares: float, price: float) -> float:
        """
        Compound the dividend onto a holder who cannot receive Hats as
        additional fractional shares:
            new_shares   = old_shares * (1 + ECONOMY_DIVIDEND_RATE)
            new_avg_cost = old_avg_cost / (1 + ECONOMY_DIVIDEND_RATE)
        The avg_cost adjustment keeps total cost basis constant - the
        holder didn't pay anything for the bonus shares, so their
        per-share cost basis decreases proportionally.

        Returns the bonus share count.
        """
        rate = ECONOMY_DIVIDEND_RATE
        scale = 1.0 + rate
        async with self._db.execute(
                "SELECT shares, avg_cost FROM portfolios "
                "WHERE user_uuid = ? AND god_name = ?",
                (user_uuid, god_name)) as cur:
            row = await cur.fetchone()
        if row is None:
            return 0.0
        old_shares = float(row[0] or 0)
        old_avg = float(row[1] or 0)
        if old_shares <= 0.001:
            return 0.0
        bonus = old_shares * rate
        await self._db.execute(
            "UPDATE portfolios SET shares = ?, avg_cost = ? "
            "WHERE user_uuid = ? AND god_name = ?",
            (old_shares * scale, old_avg / scale, user_uuid, god_name))
        await self._db.execute("""
            INSERT INTO transactions (user_uuid, god_name, type, shares, price, total, fee)
            VALUES (?, ?, 'dividend_share', ?, ?, 0, 0)
        """, (user_uuid, god_name, bonus, price))
        return bonus
