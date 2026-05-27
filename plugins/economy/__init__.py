"""
plugins.economy — God stock-market economy for HatmasBot.

This was a single 2,635-line plugins/economy.py file before P1-7 of the
cleanup sweep. It's now split into a package of concern-specific
modules (db, fair_value, trading, match, etc.) composed onto a single
EconomyPlugin class via mixin inheritance.

External imports stay the same as the monolith — main.py, the test
suite, and tools/replay_economy.py all import from
`plugins.economy` and get back the same names they always did:

    from plugins.economy import EconomyPlugin
    from plugins.economy import calculate_fair_value, FAIR_VALUE_BASE, ...

Re-exports below preserve that surface. Internal modules import each
other relatively (`from .fair_value import ...`).
"""

from __future__ import annotations

from .plugin import EconomyPlugin

# Pure-function entry points + constants used by external tools
# (tools/replay_economy.py, core/public_webserver.py's formula
# breakdown panel). Kept at the package root so the existing
# `from plugins.economy import calculate_fair_value` style import
# continues to work without any changes elsewhere.
from .fair_value import (
    calculate_fair_value,
    FAIR_VALUE_BASE,
    FAIR_VALUE_CONFIDENCE_K,
    FAIR_VALUE_WINRATE_WEIGHT,
    FAIR_VALUE_GAMES_LOG_BONUS,
    FAIR_VALUE_VOLUME_LOG_BONUS,
    FAIR_VALUE_KDA_TARGET,
    FAIR_VALUE_KDA_PER_UNIT,
    FAIR_VALUE_KDA_CAP,
    FAIR_VALUE_DOWNSIDE_CONFIDENCE_FLOOR,
)


__all__ = [
    "EconomyPlugin",
    "calculate_fair_value",
    "FAIR_VALUE_BASE",
    "FAIR_VALUE_CONFIDENCE_K",
    "FAIR_VALUE_WINRATE_WEIGHT",
    "FAIR_VALUE_GAMES_LOG_BONUS",
    "FAIR_VALUE_VOLUME_LOG_BONUS",
    "FAIR_VALUE_KDA_TARGET",
    "FAIR_VALUE_KDA_PER_UNIT",
    "FAIR_VALUE_KDA_CAP",
    "FAIR_VALUE_DOWNSIDE_CONFIDENCE_FLOOR",
]
