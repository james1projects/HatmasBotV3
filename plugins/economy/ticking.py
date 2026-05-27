"""
plugins/economy/ticking.py
==========================
Cosmetic live in-match price ticks driven by the kill detector.

The kill detector calls `on_kill` / `on_death` / `on_assist` (registered
as listeners in main.py) whenever the K/D/A on the HUD changes. The
HUD reader is best-effort and works in any gameplay state - including
jungle practice, custom games, and modes tracker.gg doesn't fully cover.
That's intentional: the OBS overlays should animate in those states too.

But the overlay is allowed to lie; the economy is not. Before the
airtight-economy pass, these handlers mutated god_prices.price and
appended rows to price_history on every detected event. That meant a
practice session moved real share prices and corrupted the sparkline.
Worse, viewers could trade against the polluted price during the leak
window.

Now the handlers are PURELY COSMETIC. They:

  * Update the in-memory `_match_kda` counter (for overlay display).
  * Compute a cosmetic price locally from
        _match_start_price * (1 + KILL_TICK)^k * (1 + DEATH_TICK)^d * (1 + ASSIST_TICK)^a
    where _match_start_price was captured at on_match_confirmed time.
  * Emit `god_stock_update_kd` / `god_stock_update` overlay events with
    that cosmetic price.

They never write to god_prices or price_history. The persisted price
only ever moves through `settle_match()` (in match.py), which is
gated on a tracker.gg-confirmed match_id and uses the canonical
fair-value formula.

The big-spike / big-crash overlay event still fires when the cumulative
cosmetic change crosses +/-15% - these are pure animation triggers,
not economic events.
"""

from __future__ import annotations

from core.config import (
    ECONOMY_KILL_TICK, ECONOMY_DEATH_TICK, ECONOMY_ASSIST_TICK,
    ECONOMY_PRICE_FLOOR,
)


class _TickingMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self._match_active           True only when tracker.gg has confirmed
                                   the current match (gates economic side
                                   effects, not the overlay animation).
      self._match_god               current god's display name - set by EITHER
                                   on_god_detected_visual (jungle practice /
                                   custom games) OR on_match_confirmed. The
                                   handlers in this file gate on THIS, so
                                   overlays animate in both modes.
      self._match_kda               [K, D, A] running totals (cosmetic counter)
      self._match_start_price       price at match start, captured by
                                    on_match_confirmed. Cosmetic base.
      self._prices                  read-only here - we don't mutate it
      self._price_history           read-only here - we don't append
    Calls into _OverlaysMixin (_emit_overlay_event, _trigger_voiceline).
    """

    def _cosmetic_price(self, start_price: float, k: int, d: int, a: int
                        ) -> float:
        """
        Compute a cosmetic price from the match-start price and the
        running K/D/A counter. Pure math, no I/O, no persistence.

        Floored at ECONOMY_PRICE_FLOOR so a long death streak in
        practice doesn't display impossibly low values, matching the
        floor the real settlement applies.
        """
        if start_price <= 0:
            return float(ECONOMY_PRICE_FLOOR)
        price = (start_price
                 * ((1 + ECONOMY_KILL_TICK) ** k)
                 * ((1 + ECONOMY_DEATH_TICK) ** d)
                 * ((1 + ECONOMY_ASSIST_TICK) ** a))
        if price < ECONOMY_PRICE_FLOOR:
            price = ECONOMY_PRICE_FLOOR
        return float(price)

    async def on_kill(self, kill_type: str, count: int = 1):
        """Cosmetic price tick on kill - fires whenever a god is visually
        identified (portrait matcher OR tracker.gg). Persisted price
        is never touched here regardless."""
        if not self._match_god:
            return  # no visual god identified -> nothing to animate

        self._match_kda[0] += count
        god_name = self._match_god
        k, d, a = self._match_kda
        new_price = self._cosmetic_price(self._match_start_price, k, d, a)

        change_pct = (
            ((new_price - self._match_start_price) / self._match_start_price) * 100
            if self._match_start_price > 0 else 0.0
        )

        print(f"[Economy] Kill x{count}! {god_name} cosmetic price "
              f"{self._match_start_price:.0f} -> {new_price:.0f} "
              f"(KDA: {k}/{d}/{a}) [persisted price unchanged]")

        self._emit_overlay_event("god_stock_update_kd", {
            "god": god_name,
            "price": round(new_price),
            "change_pct": round(change_pct, 1),
            "event": "kill",
            "kda": {"k": k, "d": d, "a": a},
            "history": self._price_history.get(god_name, [])[-10:],
            "cosmetic": True,
        })

        # Big spike voiceline trigger (15%+) - overlay flash only
        if change_pct >= 15:
            self._emit_overlay_event("economy_big_spike", {
                "god": god_name, "change_pct": round(change_pct, 1)
            })
            self._trigger_voiceline("big_spike", god_name)

    async def on_death(self, count: int = 1):
        """Cosmetic price tick on death - fires whenever a god is visually
        identified. Persisted price is never touched."""
        if not self._match_god:
            return

        self._match_kda[1] += count
        god_name = self._match_god
        k, d, a = self._match_kda
        new_price = self._cosmetic_price(self._match_start_price, k, d, a)

        change_pct = (
            ((new_price - self._match_start_price) / self._match_start_price) * 100
            if self._match_start_price > 0 else 0.0
        )

        self._emit_overlay_event("god_stock_update_kd", {
            "god": god_name,
            "price": round(new_price),
            "change_pct": round(change_pct, 1),
            "event": "death",
            "kda": {"k": k, "d": d, "a": a},
            "history": self._price_history.get(god_name, [])[-10:],
            "cosmetic": True,
        })

        # Big crash voiceline trigger (15%+ drop) - overlay flash only
        if change_pct <= -15:
            self._emit_overlay_event("economy_big_crash", {
                "god": god_name, "change_pct": round(change_pct, 1)
            })
            self._trigger_voiceline("big_crash", god_name)

    async def on_assist(self, count: int = 1):
        """Cosmetic price tick on assist - fires whenever a god is visually
        identified. Persisted price is never touched."""
        if not self._match_god:
            return

        self._match_kda[2] += count
        god_name = self._match_god
        k, d, a = self._match_kda
        new_price = self._cosmetic_price(self._match_start_price, k, d, a)

        change_pct = (
            ((new_price - self._match_start_price) / self._match_start_price) * 100
            if self._match_start_price > 0 else 0.0
        )

        self._emit_overlay_event("god_stock_update", {
            "god": god_name,
            "price": round(new_price),
            "change_pct": round(change_pct, 1),
            "event": "assist",
            "kda": {"k": k, "d": d, "a": a},
            "history": self._price_history.get(god_name, [])[-10:],
            "cosmetic": True,
        })
