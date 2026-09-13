"""
plugins/economy/hats.py
=======================
The economy's view of viewer money: a thin layer over core/wallet.py.

Replaces the MixItUp Developer API client (plugins/economy/mixitup.py,
removed 2026-09-13). Hats live in economy.db now — the bot is the source
of truth, so the market is open whenever the bot is up, and a
YouTube-only viewer has a balance like anyone else.

Callers keep the two calls they always had:
  _get_balance(user_uuid)              -> int | None (None = DB not up)
  _adjust_balance(user_uuid, amount,   -> bool
                  reason=, ref=, note=, channel=)
so trading, dividends, bingo, the site and the tests are unchanged
apart from passing a reason (the wallet ledger needs one; the default
'adjust' is only for callers that have no better word).

`_connected` stays as the flag the site's market gates read; it is
simply "the shared DB is open".
"""

from __future__ import annotations

from typing import Optional

from core import wallet as _wallet


class _HatsMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self._db          shared aiosqlite connection
      self._connected   True once the wallet can be used
    """

    def _wallet_ready(self) -> bool:
        return getattr(self, "_db", None) is not None

    async def _get_balance(self, user_uuid: str) -> Optional[int]:
        if not self._wallet_ready() or not user_uuid:
            return None
        try:
            return await _wallet.get(self._db, user_uuid, "hats")
        except Exception as e:
            print(f"[Economy] balance read failed for {user_uuid}: {e}")
            return None

    async def _adjust_balance(self, user_uuid: str, amount: int,
                              reason: str = "adjust",
                              ref: Optional[str] = None,
                              note: Optional[str] = None,
                              channel: str = "chat",
                              actor: str = "system") -> bool:
        """Add or subtract hats. Positive = add, negative = subtract.
        False when the wallet is down, the balance is insufficient, or
        (reason, ref) was already applied."""
        if not self._wallet_ready() or not user_uuid:
            return False
        try:
            result = await _wallet.adjust(
                self._db, user_uuid, "hats", int(amount), reason,
                ref=ref, note=note, channel=channel, actor=actor)
        except Exception as e:
            print(f"[Economy] balance adjust failed for {user_uuid} "
                  f"({amount:+d} {reason}): {e}")
            return False
        return result is not None
