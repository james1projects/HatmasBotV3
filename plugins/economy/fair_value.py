"""
plugins/economy/fair_value.py
=============================
Pure math + constants for the Hatmas Market price model.

Two pieces:

  * `calculate_fair_value(...)` — the steady-state price formula. Takes
    a god's running stats (wins/losses/total kda) and returns a price.
    Stateless; safe to call from anywhere, including the
    tools/replay_economy.py CLI.

  * `_FairValueMixin` — instance methods that need state from the
    plugin: `_get_volatility()` (looks at games-played cache) and
    `_calculate_match_end_change()` (which uses the volatility multiplier).

Module-level constants live here because they ARE the formula — tweaking
them changes how prices are computed across every consumer (live ticks,
replay tool, public webserver's formula breakdown).

When you tweak any of the FAIR_VALUE_* constants, run
`python tools/replay_economy.py` to recompute every god's price from
tracker.gg history under the new shape. Replay is idempotent (uses
the same inputs every time, just feeds them through the new constants)
and backs up economy.db before touching anything.
"""

from __future__ import annotations

import math
from typing import Tuple

from core.config import ECONOMY_PRICE_FLOOR


# ── Volatility tiers ────────────────────────────────────────────────────
# Used only for display labels now (volatility tier shown on overlays).
# The actual price math is fair-value driven, not volatility-multiplied.
VOLATILITY_TIERS = [
    (4,  2.0, "penny stock"),    # 1-4 games
    (10, 1.5, "mid-cap"),        # 5-10 games
    (20, 1.2, "large-cap"),      # 11-20 games
    (999999, 1.0, "blue chip"),  # 20+ games
]


# ── Fair-value formula constants ────────────────────────────────────────
# The price of a god is computed FROM the broadcaster's running stats
# rather than as a sum of per-match deltas. This means:
#   * 100% winrate over 2 games -> small premium (low confidence)
#   * 80% winrate over 100 games -> large premium (high confidence)
#   * The price is bounded; no exponential blowup from a long streak.
#
# Tweak these to dial in feel. After tweaking, run the replay tool
# (tools/replay_economy.py) to recompute every god's price from
# tracker.gg history under the new constants.

FAIR_VALUE_BASE = 100.0          # Starting price; also the "neutral" point
                                  # (50% winrate, average KDA -> ~100)
FAIR_VALUE_CONFIDENCE_K = 10.0    # Sample size for 50% confidence.
                                  # Smaller -> faster confidence ramp.
FAIR_VALUE_WINRATE_WEIGHT = 1.0   # Downside scale only. Max -50% from
                                  # winrate at full confidence below 50%.
FAIR_VALUE_GAMES_LOG_BONUS = 5.5  # Upside-only: how aggressively games
                                  # played boost a winning record. Higher
                                  # -> more reward for grinding a god.
                                  # 5.5 gives 80%/100 ~= 505 hats (before
                                  # volume premium).
FAIR_VALUE_VOLUME_LOG_BONUS = 0.30 # Upside-only: flat "you've grinded
                                   # this god" premium that rewards total
                                   # games independent of how high your
                                   # winrate is above 50%. So a 52%/300
                                   # comfort pick gets serious value, not
                                   # just gods with both high WR AND high
                                   # games. log10-scaled, so it can't run
                                   # away. Set to 0 to disable.
FAIR_VALUE_KDA_TARGET = 2.0       # Neutral KDA ratio (kills+0.5*assists)/deaths.
FAIR_VALUE_KDA_PER_UNIT = 0.05    # ±5% per unit of KDA above/below target.
FAIR_VALUE_KDA_CAP = 0.20         # Max ±20% from KDA at full confidence.
FAIR_VALUE_DOWNSIDE_CONFIDENCE_FLOOR = 0.5
                                  # Minimum effective confidence applied
                                  # to losing records. Prevents the
                                  # confidence-weighting from making
                                  # 1-game 0% losses look the same as
                                  # 100-game 43% losses. A "we know you
                                  # lost" signal lands harder even at
                                  # tiny sample size. Upside still uses
                                  # raw confidence (small samples don't
                                  # let you skyrocket on luck).


def calculate_fair_value(wins: int, losses: int,
                          total_kills: int, total_deaths: int,
                          total_assists: int) -> float:
    """
    Derive a god's price from the broadcaster's running stats.

    Asymmetric formula. Above 50% winrate the price scales aggressively
    with games played (so a 100-game 80% WR god is worth meaningfully
    more than a 2-game 100% WR god). Below 50% it stays bounded so a
    long losing streak can't drive the price into oblivion.

    Above 50% (premium path):
        games_factor = 1 + log10(games+1) * GAMES_LOG_BONUS
        winrate_pct  = (winrate - 0.5) * games_factor
        # Result: high winrate + high games -> bigger premium.
        # log10 keeps it sub-linear, no exponential blowup.

    Below 50% (discount path):
        winrate_pct  = (winrate - 0.5) * WINRATE_WEIGHT     # in [-0.5, 0)

    Both paths then:
        confidence   = games / (games + K)                  # shrinkage
        kda_ratio    = (kills + assists*0.5) / max(deaths, 0.5*games)
        kda_pct      = clip((kda_ratio - KDA_TARGET) * PER_UNIT, ±KDA_CAP)
        price        = BASE * (1 + winrate_pct * confidence)
                            * (1 + kda_pct * confidence)

    Reference points (with current constants):
        100% WR, 1 game     ~  125 hats   (small sample = small premium)
        80%  WR, 100 games  ~  805 hats   (mastered + volume = expensive)
        70%  WR, 200 games  ~  730 hats   (well-grinded solid main)
        60%  WR, 50 games   ~  287 hats   (decent + some volume)
        52%  WR, 300 games  ~  236 hats   (long-term comfort pick)
        50%  WR, 300 games  ~  175 hats   (pure volume, no skill premium)
        50%  WR, 1 game     ~  100 hats   (neutral)
        30%  WR, 50 games   ~   81 hats   (struggle — downside, no volume)
        0%   WR, 1 game     ~   75 hats   (clear loss, lifted by floor)
        0%   WR, 100 games  ~   55 hats   (long-term disaster god)
        100% WR, 1000 games ~ 1490 hats   (theoretical asymptote, capped slow)

    Volume premium fires only on the upside (winrate >= 50%) so
    grinding a god you LOSE on doesn't inflate the price.

    Downside confidence is floored (see FAIR_VALUE_DOWNSIDE_CONFIDENCE_FLOOR)
    so a 1-game 0% loss penalizes meaningfully even at tiny sample size.

    Always >= ECONOMY_PRICE_FLOOR.
    """
    games = wins + losses
    if games == 0:
        return float(FAIR_VALUE_BASE)

    winrate = wins / games
    confidence = games / (games + FAIR_VALUE_CONFIDENCE_K)

    if winrate >= 0.5:
        # Premium path: log-scaled games factor on excess winrate, plus
        # a separate flat volume premium that rewards grinding a god
        # even when winrate is barely above 50%. Volume premium is
        # applied at the end (after computing winrate_pct) so the two
        # multiply rather than compete.
        excess = winrate - 0.5  # in [0, 0.5]
        games_factor = 1.0 + math.log10(games + 1) * FAIR_VALUE_GAMES_LOG_BONUS
        winrate_pct = excess * games_factor
        volume_premium = math.log10(games + 1) * FAIR_VALUE_VOLUME_LOG_BONUS
        effective_confidence = confidence
    else:
        # Discount path: bounded. No volume premium for losing records —
        # grinding a god you don't win on shouldn't inflate the price.
        winrate_pct = (winrate - 0.5) * FAIR_VALUE_WINRATE_WEIGHT
        if winrate_pct < -0.5:
            winrate_pct = -0.5
        volume_premium = 0.0
        # Floor effective confidence on the downside so a 1-game 0%
        # WR penalizes meaningfully (otherwise tiny confidence × big
        # winrate penalty barely budges the price). See constant docs.
        effective_confidence = max(confidence,
                                    FAIR_VALUE_DOWNSIDE_CONFIDENCE_FLOOR)

    # KDA ratio. Use total_deaths floored at half a death per game so
    # zero-death streaks don't divide by zero AND don't pretend you're
    # immortal.
    deaths_basis = max(total_deaths, 0.5 * games)
    kda_ratio = (total_kills + total_assists * 0.5) / deaths_basis if deaths_basis > 0 else FAIR_VALUE_KDA_TARGET
    kda_pct = (kda_ratio - FAIR_VALUE_KDA_TARGET) * FAIR_VALUE_KDA_PER_UNIT
    if kda_pct > FAIR_VALUE_KDA_CAP:
        kda_pct = FAIR_VALUE_KDA_CAP
    elif kda_pct < -FAIR_VALUE_KDA_CAP:
        kda_pct = -FAIR_VALUE_KDA_CAP

    price = FAIR_VALUE_BASE * (1.0 + winrate_pct * effective_confidence) \
                            * (1.0 + kda_pct * confidence) \
                            * (1.0 + volume_premium)
    if price < ECONOMY_PRICE_FLOOR:
        price = ECONOMY_PRICE_FLOOR
    return float(price)


# ── Base price change targets (win/loss + KDA quality) ──────────────────
# The match-end-change formula produces a base_change_pct, then multiplied
# by volatility. Win base: +3% to +15% depending on KDA ratio.
# Loss base: -5% to -13% depending on KDA ratio.
WIN_BASE_MIN = 3.0    # Carried win (bad KDA)
WIN_BASE_MAX = 15.0   # Domination (great KDA)
LOSS_BASE_MIN = -5.0   # Close loss (decent KDA)
LOSS_BASE_MAX = -13.0  # Feeding (terrible KDA)

# KDA ratio thresholds for scaling (separate for wins and losses,
# because loss KDAs cluster in a tighter range than win KDAs)
WIN_KDA_LOW = 0.3      # Carried win: (1+1)/6 = 0.33
WIN_KDA_HIGH = 5.0     # Domination:  (12+3)/1 = 15 (capped)
LOSS_KDA_LOW = 0.1     # Feeding:     (1+0.5)/10 = 0.15
LOSS_KDA_HIGH = 1.2    # Close loss:  (5+2)/6 = 1.17


class _FairValueMixin:
    """
    Instance methods that consume the fair-value module constants and
    plugin state (`self._games_played`).

    Mixed into EconomyPlugin alongside the other concern-specific mixins.
    Has no __init__ — relies on EconomyPlugin's __init__ to set up the
    instance attributes it reads.
    """

    # The plugin's __init__ initializes this; declared here for clarity.
    _games_played: dict

    def _get_volatility(self, god_name: str) -> Tuple[float, str]:
        """Get volatility multiplier and tier name for a god."""
        games = self._games_played.get(god_name, 0)
        for max_games, multiplier, tier_name in VOLATILITY_TIERS:
            if games <= max_games:
                return multiplier, tier_name
        return 1.0, "blue chip"

    def _calculate_match_end_change(self, outcome: str, kills: int, deaths: int,
                                     assists: int, god_name: str) -> float:
        """
        Calculate the percentage price change at match end.

        Formula:
          1. Compute KDA ratio: (kills + assists*0.5) / max(deaths, 1)
          2. Map ratio to base change range (win or loss)
          3. Multiply by volatility
        """
        # KDA quality ratio
        kda_ratio = (kills + assists * 0.5) / max(deaths, 1)

        if outcome == "win":
            # Wins: higher ratio = bigger gain
            t = (kda_ratio - WIN_KDA_LOW) / (WIN_KDA_HIGH - WIN_KDA_LOW)
            t = max(0.0, min(1.0, t))
            base_change = WIN_BASE_MIN + t * (WIN_BASE_MAX - WIN_BASE_MIN)
        else:
            # Losses: higher ratio = milder loss (close loss vs feeding)
            t = (kda_ratio - LOSS_KDA_LOW) / (LOSS_KDA_HIGH - LOSS_KDA_LOW)
            t = max(0.0, min(1.0, t))
            base_change = LOSS_BASE_MAX + t * (LOSS_BASE_MIN - LOSS_BASE_MAX)

        # Apply volatility multiplier
        vol_mult, _ = self._get_volatility(god_name)
        final_change = base_change * vol_mult

        return final_change
