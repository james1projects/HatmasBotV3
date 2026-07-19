"""
core/aspect_roster.py — which SMITE 2 gods have an Aspect.
==========================================================
Aspects are alternate versions of a god's kit that can be toggled on
in the lobby. Viewers can request them ("!nominate Khepri aspect"),
so the bot needs to know which gods actually have one — a request for
an aspect that doesn't exist should be rejected up front, not paid
for and then discovered.

The wiki is the source of truth. Every god page with an aspect embeds
one of the shared aspect icons (File:Aspect Burst.png etc., all in
Category:Aspect icons), so the scrape is two cheap api.php calls:

  1. categorymembers of "Category:Aspect icons"  -> the icon files
  2. fileusage for those files                   -> pages embedding them

Pages that aren't gods (patch notes announce new aspects and embed the
same icons) are dropped by intersecting with the god roster. No god
pages are fetched individually, so a refresh is ~2 requests regardless
of roster size.

Sources, in priority order (mirrors core/god_roster.py):

  1. data/aspect_roster.json — cache written by refresh()
  2. BUNDLED_ASPECT_SLUGS below — snapshot so a fresh checkout with no
     network still validates aspects

refresh() never raises and never shrinks the list on a failed or
implausible fetch — a wiki outage must not turn every aspect request
into "unknown".

Sync module — callers on the event loop wrap refresh() in
asyncio.to_thread(). GodRequestPlugin's roster-refresh loop does this
once at startup (James's "auto update on bot launch") and then every
6 h alongside the god roster refresh.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from core.config import DATA_DIR
from core.god_roster import _fetch_url, name_to_slug
from core import god_roster

ASPECT_CACHE = DATA_DIR / "aspect_roster.json"

API_BASE = "https://wiki.smite2.com/api.php"

# Fallback in case the category query fails: the shared aspect icon
# files as of 2026-07-17. New aspects reuse these; a brand-new icon
# would be picked up by the category query on the next healthy fetch.
FALLBACK_ICON_FILES = (
    "File:Aspect Blind.png", "File:Aspect Burst.png",
    "File:Aspect CrossedSwords.png", "File:Aspect Fist.png",
    "File:Aspect FullHeart.png", "File:Aspect HeartScaling.png",
    "File:Aspect Radius.png", "File:Aspect Scaling.png",
    "File:Aspect Shield.png", "File:Aspect Slow.png",
    "File:Aspect Stun.png", "File:Aspect SwingingSword.png",
    "File:Aspect UpwardArrow.png", "File:Aspect Wave.png",
)

# Snapshot of gods with an aspect, scraped 2026-07-17 (70 gods).
BUNDLED_ASPECT_SLUGS = (
    "achilles", "agni", "ah-puch", "amaterasu", "anhur", "apollo",
    "ares", "artemis", "artio", "athena", "atlas", "bacchus",
    "baron-samedi", "bastet", "bellona", "cabrakan", "cerberus",
    "cernunnos", "chaac", "charon", "chiron", "chronos", "cupid",
    "da-ji", "danzaburou", "discordia", "eset", "fenrir", "ganesha",
    "geb", "gilgamesh", "guan-yu", "hecate", "hercules", "horus",
    "hou-yi", "hun-batz", "ishtar", "jormungandr", "kali", "khepri",
    "kukulkan", "loki", "merlin", "mordred", "morgan-le-fay", "ne-zha",
    "neith", "nemesis", "nu-wa", "nut", "osiris", "pele", "poseidon",
    "ra", "rama", "ratatoskr", "scylla", "sobek", "sol", "sun-wukong",
    "sylvanus", "thanatos", "the-morrigan", "thor", "tsukuyomi",
    "vulcan", "xbalanque", "xing-tian", "yemoja",
)
BUNDLED_SNAPSHOT_DATE = "2026-07-17"

# A live fetch that finds fewer aspect gods than this is a broken /
# partial response, not a real shrink — never overwrite the cache with
# it. 70 when written; Hi-Rez only adds aspects.
MIN_PLAUSIBLE_ASPECTS = 40

_aspects: Optional[set] = None       # in-memory {slug, ...}
_updated_at: Optional[str] = None


# ============================================================
# ACCESS
# ============================================================

def display_god(god: str, use_aspect) -> str:
    """'Khepri' / 'Khepri (Aspect)' — the one true rendering of a
    (god, aspect) request identity in chat and logs."""
    return f"{god} (Aspect)" if use_aspect else god

def _load() -> set:
    """Populate the in-memory aspect set from cache, else bundled."""
    global _aspects, _updated_at
    if _aspects is not None:
        return _aspects

    if ASPECT_CACHE.exists():
        try:
            with open(ASPECT_CACHE, encoding="utf-8") as f:
                data = json.load(f)
            slugs = data.get("slugs", [])
            if len(slugs) >= MIN_PLAUSIBLE_ASPECTS:
                _aspects = set(slugs)
                _updated_at = data.get("updated_at")
                return _aspects
        except Exception as e:
            print(f"[AspectRoster] Bad cache {ASPECT_CACHE.name}: {e} — "
                  f"using bundled snapshot")

    _aspects = set(BUNDLED_ASPECT_SLUGS)
    _updated_at = BUNDLED_SNAPSHOT_DATE
    return _aspects


def has_aspect(god_name: str) -> bool:
    """True if this god has an Aspect. Accepts display names in any
    casing (slug-matched against the roster)."""
    if not god_name:
        return False
    return god_roster.slug_for(god_name) in _load()


def aspect_slugs() -> set:
    """Copy of the aspect-capable slug set."""
    return set(_load())


def aspect_names() -> list:
    """Display names of aspect-capable gods, roster-ordered. Gods in
    the aspect set but missing from the roster (shouldn't happen) are
    dropped — every name returned is requestable."""
    slugs = _load()
    return [g["name"] for g in god_roster.gods() if g["slug"] in slugs]


def updated_at() -> Optional[str]:
    _load()
    return _updated_at


def refresh_needed(max_age_hours: float = 24.0) -> bool:
    _load()
    if not _updated_at:
        return True
    try:
        age = datetime.now() - datetime.fromisoformat(_updated_at)
    except ValueError:
        return True
    return age > timedelta(hours=max_age_hours)


# ============================================================
# LIVE REFRESH
# ============================================================

def _api_get(params: dict) -> Optional[dict]:
    """One api.php GET via the shared Cloudflare-capable fetcher."""
    qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    body = _fetch_url(f"{API_BASE}?{qs}&format=json&formatversion=2")
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception as e:
        print(f"[AspectRoster] wiki response unparseable: {e}")
        return None


def _fetch_icon_files() -> list:
    """The aspect icon File: titles from Category:Aspect icons.
    Falls back to the bundled list on any failure."""
    data = _api_get({
        "action": "query", "list": "categorymembers",
        "cmtitle": "Category:Aspect icons",
        "cmtype": "file", "cmlimit": "500",
    })
    try:
        members = [m["title"] for m in data["query"]["categorymembers"]]
    except Exception:
        members = []
    if not members:
        print("[AspectRoster] icon category fetch failed — using "
              "bundled icon list")
        return list(FALLBACK_ICON_FILES)
    return members


def fetch_live_aspects() -> Optional[set]:
    """Slugs of gods whose wiki page embeds an aspect icon, or None
    on fetch failure. Non-god pages (patch notes) are filtered out by
    intersecting with the god roster."""
    icon_files = _fetch_icon_files()

    data = _api_get({
        "action": "query", "titles": "|".join(icon_files),
        "prop": "fileusage", "fulimit": "500",
    })
    if data is None:
        return None
    try:
        pages = data["query"]["pages"]
    except Exception as e:
        print(f"[AspectRoster] fileusage response malformed: {e}")
        return None

    used_by = set()
    for page in pages:
        for fu in page.get("fileusage", []):
            used_by.add(fu.get("title", ""))
    if not used_by:
        print("[AspectRoster] fileusage returned no pages — ignoring")
        return None

    roster_slugs = {g["slug"] for g in god_roster.gods()}
    live = {name_to_slug(t) for t in used_by
            if name_to_slug(t) in roster_slugs}
    return live


def refresh(force: bool = False, max_age_hours: float = 24.0) -> list:
    """Fetch the live aspect list and merge it into the cache.

    Returns the NEWLY seen aspect god slugs (empty on no-change, skip,
    or failure). Gods never leave the set on a live fetch — an aspect
    that vanishes from the wiki (edit war, template rework) shouldn't
    invalidate pending requests. Never raises.
    """
    global _aspects, _updated_at
    if not force and not refresh_needed(max_age_hours):
        return []

    live = fetch_live_aspects()
    if live is None:
        return []
    if len(live) < MIN_PLAUSIBLE_ASPECTS:
        print(f"[AspectRoster] live fetch found only {len(live)} aspect "
              f"gods — ignoring (floor is {MIN_PLAUSIBLE_ASPECTS})")
        return []

    current = set(_load())
    added = sorted(live - current)
    merged = current | live

    _aspects = merged
    _updated_at = datetime.now().isoformat(timespec="seconds")
    try:
        from core.atomic_io import atomic_write_json
        atomic_write_json(ASPECT_CACHE, {
            "updated_at": _updated_at,
            "slugs": sorted(merged),
        })
    except Exception as e:
        print(f"[AspectRoster] cache write failed: {e}")

    if added:
        print(f"[AspectRoster] {len(added)} new aspect god(s): "
              + ", ".join(added))
    return added
