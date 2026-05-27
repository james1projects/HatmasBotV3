"""
plugins/economy/god_names.py
============================
God-name normalization. Viewers type !buy ymir / !buy YMIR / !buy
"hou yi" / !buy "Hou Yi" — we want all of them to map to the same
canonical "Hou Yi" string.

Builds a lowercase→display-name lookup from two sources at startup:

  1. Every god that already has a row in god_prices (canonical names
     from past matches that the bot has already seen).
  2. Every PNG in `Custom God Icons/` (for gods we've never matched
     yet — viewers can pre-buy a god before Hatmaster has played it).

`_resolve_god_name()` does exact → prefix → contains lookup, returning
None if the input is ambiguous (more than one match) or unknown.
"""

from __future__ import annotations

from typing import Optional

from core.config import BASE_DIR


class _GodNamesMixin:
    """
    Mixed into EconomyPlugin. Reads/writes:
      self._db          (shared aiosqlite.Connection — for god_prices read)
      self._god_names   dict[lowercase -> ProperCase] populated here

    Both initialized in EconomyPlugin.__init__.
    """

    async def _build_god_name_index(self):
        """Build lookup table from lowercase → ProperCase god names."""
        async with self._db.execute("SELECT god_name FROM god_prices") as cursor:
            async for row in cursor:
                name = row[0]
                self._god_names[name.lower()] = name

        # Also scan the Custom God Icons folder for all known gods.
        # BASE_DIR points at the repo root, so "Custom God Icons" sits
        # right next to plugins/ regardless of how this file is nested
        # inside plugins/economy/.
        icons_dir = BASE_DIR / "Custom God Icons"
        if icons_dir.exists():
            for f in icons_dir.iterdir():
                if f.suffix == ".png" and not f.stem.startswith("."):
                    # Skip skin variants (contain spaces + numbers like "Ymir 2")
                    # Keep only base god names
                    name = f.stem
                    lower = name.lower()
                    if lower not in self._god_names:
                        self._god_names[lower] = name

    def _resolve_god_name(self, user_input: str) -> Optional[str]:
        """Resolve user input to a proper god name. Supports partial matching."""
        lower = user_input.lower().strip()
        if not lower:
            return None

        # Exact match
        if lower in self._god_names:
            return self._god_names[lower]

        # Partial match (prefix)
        matches = [v for k, v in self._god_names.items() if k.startswith(lower)]
        if len(matches) == 1:
            return matches[0]

        # Partial match (contains)
        if not matches:
            matches = [v for k, v in self._god_names.items() if lower in k]
            if len(matches) == 1:
                return matches[0]

        return None
