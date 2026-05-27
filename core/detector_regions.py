"""
core/detector_regions.py
========================
Persisted detection-region coordinates for the kill detector + god
portrait matcher. Single source of truth for:

  * KDA bar crop                (kda_reader.KDA_REGION)
  * God portrait crop           (god_matcher.PORTRAIT_REGION)
  * HUD ability-bar variance check     (kda_reader.HUD_CHECK_REGION)
  * Portrait/health-bar gameplay check (kda_reader.GAMEPLAY_CHECK_REGION)
  * Store/scoreboard overlay check     (kda_reader.OVERLAY_CHECK_REGION)

Regions live on disk in data/detector_regions.json, one key per
region, value is a 4-tuple [x1, y1, x2, y2] in 1920x1080 source coords.
Missing file or missing keys fall back to DEFAULTS individually, so
a partial override works fine (only KDA tweaked, the rest stays at
the shipped defaults).

Used by:
  - core/kda_reader.py        (loads at module import time)
  - core/god_matcher.py       (loads at module import time)
  - core/webserver.py         (GET / POST /api/detector_regions)

Changes to the JSON file take effect on next bot restart - we don't
hot-reload the constants in the running plugin yet. That's the right
trade-off for stage 1: edit JSON, restart, verify. Stage 2 (the drag
UI) will add hot-reload via the POST endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Tuple


# JSON file path. Resolves relative to the repo root so it works
# regardless of how the bot is launched.
REGIONS_FILE = Path(__file__).parent.parent / "data" / "detector_regions.json"

# Hardcoded defaults. These match the values that lived as inline
# constants in kda_reader.py + god_matcher.py before this module
# existed - so removing/clearing the JSON file restores the
# ship-default behavior.
DEFAULTS: Dict[str, Tuple[int, int, int, int]] = {
    "kda":            (18, 1036, 105, 1056),
    "portrait":       (635, 942, 715, 1022),
    "overlay_check":  (0, 100, 1152, 900),
    "hud_check":      (600, 1000, 780, 1080),
    "gameplay_check": (600, 900, 760, 1080),
}


def load_regions() -> Dict[str, Tuple[int, int, int, int]]:
    """
    Read detector_regions.json and return a fully-populated dict
    keyed on region name. Missing file -> all defaults. Missing keys
    inside the file -> default for those keys only. Malformed entries
    (wrong arity, non-numeric) -> default for those keys, logged.
    """
    regions: Dict[str, Tuple[int, int, int, int]] = {
        k: tuple(v) for k, v in DEFAULTS.items()
    }
    if not REGIONS_FILE.exists():
        return regions
    try:
        with open(REGIONS_FILE, "r", encoding="utf-8") as f:
            overrides = json.load(f)
    except Exception as e:
        print(f"[detector_regions] Failed to read {REGIONS_FILE}: {e}")
        return regions
    if not isinstance(overrides, dict):
        print(f"[detector_regions] JSON root is not an object, ignoring")
        return regions
    for key in DEFAULTS:
        if key not in overrides:
            continue
        val = overrides[key]
        if not isinstance(val, (list, tuple)) or len(val) != 4:
            print(f"[detector_regions] Bad shape for {key!r}, using default")
            continue
        try:
            regions[key] = (int(val[0]), int(val[1]),
                            int(val[2]), int(val[3]))
        except (TypeError, ValueError) as e:
            print(f"[detector_regions] Bad value for {key!r}: {e}; using default")
    return regions


def save_regions(regions: Dict) -> Dict[str, list]:
    """
    Persist regions to JSON. Filters to known keys + validates shape
    so a bad POST can't corrupt the file. Returns the dict actually
    written (useful for the API to echo back).

    Note: this overwrites the whole file. Callers should pass the
    complete set of regions they want persisted, not a delta.
    """
    REGIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    safe: Dict[str, list] = {}
    for key in DEFAULTS:
        if key not in regions:
            continue
        val = regions[key]
        if not isinstance(val, (list, tuple)) or len(val) != 4:
            raise ValueError(f"Region {key!r} must be a 4-element list")
        try:
            safe[key] = [int(val[0]), int(val[1]),
                         int(val[2]), int(val[3])]
        except (TypeError, ValueError) as e:
            raise ValueError(f"Region {key!r} has non-integer value: {e}")
        # Sanity: x1 < x2, y1 < y2
        x1, y1, x2, y2 = safe[key]
        if x1 >= x2 or y1 >= y2:
            raise ValueError(
                f"Region {key!r} has inverted coords: "
                f"x1={x1} x2={x2} y1={y1} y2={y2}"
            )
    with open(REGIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(safe, f, indent=2)
    return safe
