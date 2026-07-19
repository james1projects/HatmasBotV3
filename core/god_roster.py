"""
core/god_roster.py — single source of truth for the SMITE 2 god list.
=====================================================================
Before this module, four places each had their own idea of which gods
exist: a hardcoded list in plugins/godrequest.py (froze at 82 gods in
April 2026), the saved "Gods - SMITE 2 Wiki.html" page that both
download tools parse (same 82), the data/god_icons/ folder scan in
core/youtube_parser.py, and the economy's DB+icons index. When Bastet
and Chronos released, !godreq called them unknown even though the wiki
knew better.

This module owns the roster. Sources, in priority order:

  1. data/god_roster.json — cache written by refresh(); carries a
     first_seen date per god so callers can prioritize new releases.
  2. core/god_roster_data.py — bundled snapshot, so a fresh checkout
     with no network still knows every god as of the snapshot date.

refresh() fetches the live wiki god list. wiki.smite2.com is fronted
by Cloudflare, which 403s plain urllib (TLS fingerprint), so the fetch
uses curl_cffi when installed and falls back to the system curl.exe
(ships with Windows 10+). Any failure leaves the current roster
untouched — a wiki outage must never break god requests.

It also bundles the full SMITE 1 catalog (frozen at 130 gods). Almost
every SMITE 2 release is a port, so the catalog lets chat commands say
"Bake Kujira isn't in SMITE 2 yet" instead of "unknown god", and lets
the download tools pre-fetch SMITE 1 art as a display fallback.

Sync module — callers on the event loop wrap refresh() in
asyncio.to_thread().
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.config import DATA_DIR
from core.god_roster_data import (
    BUNDLED_S2_GODS, SMITE1_CATALOG, BUNDLED_SNAPSHOT_DATE,
)

ROSTER_CACHE = DATA_DIR / "god_roster.json"

GODS_PAGE_API = (
    "https://wiki.smite2.com/api.php"
    "?action=parse&page=Gods&prop=text&format=json&formatversion=2"
)
ICON_FILE_RE = re.compile(r'File:(T_[^"]+Default_Icon\.png)')

# A live fetch that parses fewer gods than this is a broken/partial
# page, not a real roster — never overwrite the cache with it. The
# roster was 88 when this was written; it only ever grows.
MIN_PLAUSIBLE_GODS = 60

_roster: Optional[list] = None       # in-memory [{name, slug, ...}]
_updated_at: Optional[str] = None


# ============================================================
# NAME / FILENAME HELPERS (shared with the download tools)
# ============================================================

def split_camel(name: str) -> str:
    """'CuChulainn' -> 'Cu Chulainn', 'MorganLeFay' -> 'Morgan Le Fay'.
    Replaces the old hand-maintained concat_map, which silently missed
    every new concatenated wiki name (XingTian came out 'Xingtian')."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def wiki_filename_to_name(filename: str) -> str:
    """'T_Hou_Yi(S2)_Default_Icon.png' -> 'Hou Yi'."""
    name = filename
    if name.startswith("T_"):
        name = name[2:]
    for suffix in ["(S2)_Default_Icon.png", "_Default_Icon.png"]:
        if suffix in name:
            name = name[: name.index(suffix)]
            break
    name = name.replace("_", " ")
    name = " ".join(split_camel(tok) for tok in name.split(" "))
    return name.strip()


def name_to_slug(name: str) -> str:
    """'Morgan Le Fay' -> 'morgan-le-fay' (matches data/god_icons/ stems)."""
    return name.lower().replace("'", "").replace(" ", "-")


# ============================================================
# ROSTER ACCESS
# ============================================================

def _bundled_roster() -> list:
    return [
        {"name": n, "slug": s, "wiki_filename": w, "first_seen": fs}
        for n, s, w, fs in BUNDLED_S2_GODS
    ]


def _load() -> list:
    """Populate the in-memory roster from cache, else bundled data."""
    global _roster, _updated_at
    if _roster is not None:
        return _roster

    if ROSTER_CACHE.exists():
        try:
            with open(ROSTER_CACHE, encoding="utf-8") as f:
                data = json.load(f)
            gods = data.get("gods", [])
            if len(gods) >= MIN_PLAUSIBLE_GODS:
                _roster = gods
                _updated_at = data.get("updated_at")
                return _roster
        except Exception as e:
            print(f"[GodRoster] Bad cache {ROSTER_CACHE.name}: {e} — "
                  f"using bundled snapshot")

    _roster = _bundled_roster()
    _updated_at = BUNDLED_SNAPSHOT_DATE
    return _roster


def gods() -> list:
    """The SMITE 2 roster: [{name, slug, wiki_filename, first_seen}].
    Returns the live list object's copy — callers may not mutate it."""
    return list(_load())


def names() -> list:
    """Display names, for resolver candidate lists."""
    return [g["name"] for g in _load()]


def slug_for(name: str) -> str:
    """Canonical slug for a display name (roster hit or derived)."""
    target = name.lower()
    for g in _load():
        if g["name"].lower() == target:
            return g["slug"]
    return name_to_slug(name)


def new_gods(days: int = 21) -> list:
    """Gods whose first_seen is within `days`. Newest first."""
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for g in _load():
        fs = g.get("first_seen")
        if not fs:
            continue
        try:
            if datetime.fromisoformat(fs) >= cutoff:
                out.append(g)
        except ValueError:
            continue
    out.sort(key=lambda g: g["first_seen"], reverse=True)
    return out


def updated_at() -> Optional[str]:
    _load()
    return _updated_at


def refresh_needed(max_age_hours: float = 24.0) -> bool:
    """True when the cache is older than max_age_hours (or absent)."""
    _load()
    if not _updated_at:
        return True
    try:
        age = datetime.now() - datetime.fromisoformat(_updated_at)
    except ValueError:
        return True
    return age > timedelta(hours=max_age_hours)


# ============================================================
# SMITE 1 CATALOG
# ============================================================

def smite1_catalog() -> list:
    """[{name, slug, s1_icon}] for all 130 SMITE 1 gods."""
    return [{"name": n, "slug": s, "s1_icon": i} for n, s, i in SMITE1_CATALOG]


def smite1_only_names() -> list:
    """SMITE 1 gods not (yet) released in SMITE 2 — the 'coming
    someday' set that !godreq should name-check instead of calling
    unknown."""
    s2 = {g["slug"] for g in _load()}
    # Mulan shipped in SMITE 2 as "Hua Mulan"; don't report the S1
    # name as missing when the S2 port is already on the roster.
    port_slugs = {"mulan": "hua-mulan"}
    out = []
    for name, slug, _icon in SMITE1_CATALOG:
        if slug in s2 or port_slugs.get(slug) in s2:
            continue
        out.append(name)
    return out


# ============================================================
# LIVE REFRESH
# ============================================================

def _fetch_url(url: str, timeout: float = 20.0) -> Optional[bytes]:
    """GET a Cloudflare-fronted URL. curl_cffi (browser TLS
    fingerprint) when available, else system curl.exe. None on any
    failure — plain urllib is not attempted because it always 403s
    against wiki.smite2.com."""
    try:
        from curl_cffi import requests as cffi_requests
        # "chrome124" first: as of July 2026 Cloudflare challenges the
        # generic "chrome" fingerprint (403 + challenge page) but lets
        # the pinned chrome124 profile through. Keep "chrome" as a
        # second attempt in case a curl_cffi update drops the pinned
        # profile before we notice.
        for imp in ("chrome124", "chrome"):
            try:
                resp = cffi_requests.get(url, timeout=timeout,
                                         impersonate=imp)
            except Exception as e:
                print(f"[GodRoster] curl_cffi ({imp}) fetch failed: {e}")
                continue
            if resp.status_code == 200:
                return resp.content
            print(f"[GodRoster] fetch {resp.status_code} from wiki "
                  f"(impersonate={imp})")
        return None
    except ImportError:
        pass

    try:
        proc = subprocess.run(
            ["curl", "-s", "--max-time", str(int(timeout)), url],
            capture_output=True, timeout=timeout + 10,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except Exception as e:
        print(f"[GodRoster] curl fetch failed: {e}")
    return None


def fetch_live_gods() -> Optional[list]:
    """Parse the live wiki Gods page. Returns [{name, slug,
    wiki_filename}] or None. Applies the same plausibility floor as
    the cache loader so a half-rendered page can't shrink the roster."""
    body = _fetch_url(GODS_PAGE_API)
    if not body:
        return None
    try:
        html = json.loads(body)["parse"]["text"]
    except Exception as e:
        print(f"[GodRoster] wiki response unparseable: {e}")
        return None

    files = sorted(set(ICON_FILE_RE.findall(html)))
    if len(files) < MIN_PLAUSIBLE_GODS:
        print(f"[GodRoster] live page parsed only {len(files)} gods — "
              f"ignoring (floor is {MIN_PLAUSIBLE_GODS})")
        return None

    out = []
    for f in files:
        name = wiki_filename_to_name(f)
        out.append({"name": name, "slug": name_to_slug(name),
                    "wiki_filename": f})
    return out


def refresh(force: bool = False, max_age_hours: float = 24.0) -> list:
    """Fetch the live roster and merge it into the cache.

    Returns the list of NEWLY seen gods (empty on no-change, skip, or
    any failure). Existing gods keep their first_seen; gods that
    vanish from the wiki are kept (a wiki edit war must not eat the
    roster). Never raises.
    """
    global _roster, _updated_at
    if not force and not refresh_needed(max_age_hours):
        return []

    live = fetch_live_gods()
    if live is None:
        return []

    # Copy entries before mutating: _load() returns the live dicts
    # that concurrent readers (names()/gods() on the event loop) are
    # holding, so the merge must never edit them in place.
    current = {g["slug"]: dict(g) for g in _load()}
    today = datetime.now().date().isoformat()
    added = []
    for g in live:
        slug = g["slug"]
        if slug in current:
            # Wiki filename can change (art updates) — track it.
            current[slug]["wiki_filename"] = g["wiki_filename"]
        else:
            entry = {**g, "first_seen": today}
            current[slug] = entry
            added.append(entry)

    merged = sorted(current.values(), key=lambda g: g["slug"])
    _roster = merged
    _updated_at = datetime.now().isoformat(timespec="seconds")
    try:
        from core.atomic_io import atomic_write_json
        atomic_write_json(ROSTER_CACHE, {
            "updated_at": _updated_at,
            "gods": merged,
        })
    except Exception as e:
        print(f"[GodRoster] cache write failed: {e}")

    if added:
        print(f"[GodRoster] {len(added)} new god(s): "
              + ", ".join(g["name"] for g in added))
    return added
