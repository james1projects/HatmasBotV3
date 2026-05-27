"""
YouTube Title Parser
====================
Extracts James's god from a YouTube video title.

Title convention (from James):
    "Full Gameplay: {My God name} vs {Their god name}"

Examples that should parse cleanly:
    "Full Gameplay: Ymir vs Loki"               -> "Ymir"
    "Full Gameplay: Hou Yi vs Athena"           -> "Hou Yi"
    "Full Gameplay: Ah Muzen Cab vs Sylvanus"   -> "Ah Muzen Cab"

The split-on-" vs " trick handles multi-word god names cleanly because no
god name contains the standalone token "vs". The captured left side is
then validated against the known god list (case-insensitive) so a
malformed title or typo is rejected rather than silently accepted.

Used by:
    - tools/mark_youtube_video.py  (CLI: --auto-scan)
    - plugins/youtube_rewards.py   (live poller)
"""

import re
from pathlib import Path
from typing import Iterable, List, Optional


# Compiled at import time. Captures the prefix-stripped, pre-"vs" segment.
# Tolerates a colon, dash, en-dash, or em-dash after "Full Gameplay" — and
# any amount of whitespace around the separators.
_TITLE_RE = re.compile(
    r"^\s*Full\s*Gameplay\s*[:\-–—]\s*(.+?)\s+vs\b",
    re.IGNORECASE,
)


def _kebab_to_title(stem: str) -> str:
    """
    'baron-samedi'  -> 'Baron Samedi'
    'hou-yi'        -> 'Hou Yi'
    'ah-muzen-cab'  -> 'Ah Muzen Cab'
    'ymir'          -> 'Ymir'
    """
    return " ".join(part.capitalize() for part in stem.split("-"))


def load_known_gods(repo_root: Path) -> List[str]:
    """
    Build the canonical list of proper-cased god names. Sources, in order:

      1. data/god_icons/   (tracker.gg CDN icons, kebab-case stems —
                            clean one-file-per-god). This is the
                            primary source.
      2. god_prices table  (gods that have been encountered live).
                            Read by the caller via DB and merged in if
                            wanted; not done here so this stays sync.

    Custom God Icons/ is intentionally NOT used here — its files are
    skin variants ("Achilles-Battleworn.png"), not base god names.
    """
    icons_dir = repo_root / "data" / "god_icons"
    gods: List[str] = []
    if icons_dir.exists():
        for f in icons_dir.iterdir():
            if f.suffix.lower() != ".png" or f.stem.startswith("."):
                continue
            gods.append(_kebab_to_title(f.stem))
    return sorted(set(gods))


def parse_my_god(title: str, known_gods: Iterable[str]) -> Optional[str]:
    """
    Return the proper-cased god name James played in the video, or None
    if the title doesn't match the convention or the captured name isn't
    in the known god list.

    `known_gods` should be an iterable of proper-cased god names — typically
    the keys of EconomyPlugin._god_names or the file stems from
    `Custom God Icons/`. The lookup is case-insensitive but the returned
    string preserves the proper casing from `known_gods`.
    """
    if not title:
        return None

    m = _TITLE_RE.match(title)
    if not m:
        return None

    candidate = m.group(1).strip()
    if not candidate:
        return None

    # Case-insensitive lookup against the known god list.
    candidate_lower = candidate.lower()
    for god in known_gods:
        if god.lower() == candidate_lower:
            return god

    return None


def is_valid_god(name: str, known_gods: Iterable[str]) -> bool:
    """True if `name` (case-insensitive) appears in the known god list."""
    if not name:
        return False
    lower = name.lower()
    return any(g.lower() == lower for g in known_gods)


def resolve_god(name: str, known_gods: Iterable[str]) -> Optional[str]:
    """
    Case-insensitive resolve of `name` against the known god list.
    Returns the proper-cased name from the list, or None if not found.
    Used by the CLI when the operator types a god name.
    """
    if not name:
        return None
    lower = name.lower().strip()
    for god in known_gods:
        if god.lower() == lower:
            return god
    return None
