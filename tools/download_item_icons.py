"""
Download Smite 2 Item Icons (128x128 wiki PNGs)
================================================
Sibling to tools/download_god_icons.py and tools/download_god_cards.py.
Pulls every Smite 2 item icon from wiki.smite2.com and saves them as
data/item_icons/<slug>.png (lowercased, hyphenated, apostrophes
stripped — same slugify rule the rest of the thumbnail tools use).

Also emits data/item_icons/_manifest.json with one entry per item:
    {
      "<slug>": {
        "display_name": "Bumba's Cudgel",
        "category":     "Starter",
        "tier":         "T1",
        "god":          null,          # set for GodSpecific items
        "source_file":  "Starter_T1_Bumba's_Cudgel.png"
      }
    }
The thumbnail studio reads this manifest to populate category-aware
dropdowns. Plain consumers can also walk data/item_icons/*.png — both
work.

Why this exists
---------------
The build_guide thumbnail preset needs three item-icon slots populated
from a dropdown. The dropdown should show clean display names ("Bumba's
Cudgel") grouped by category (Consumable / Curio / Relic / Starter /
GodSpecific / T1 / T2 / T3), not raw wiki filenames. The manifest
captures that grouping at download time so the UI doesn't have to parse
it on every page load.

Cloudflare bypass
-----------------
wiki.smite2.com is fronted by Cloudflare and 403s plain urllib.request
due to the TLS fingerprint check. curl_cffi impersonating Chrome gets
through cleanly — same trick the existing god-card and voice-line
downloaders use. `pip install curl_cffi` is required.

Usage
-----
    python tools/download_item_icons.py                # Download all
    python tools/download_item_icons.py -v             # Verbose: show every URL tried
    python tools/download_item_icons.py --force        # Re-download everything
    python tools/download_item_icons.py --check        # List missing items
    python tools/download_item_icons.py --only Executioner,Bloodforge,Rage
    python tools/download_item_icons.py --throttle 0.2 # Slow down per-request delay
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as _cffi_requests
except ImportError:
    print("ERROR: curl_cffi is required for the Cloudflare bypass.\n"
          "       pip install curl_cffi", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "item_icons"
MANIFEST_PATH = OUTPUT_DIR / "_manifest.json"

WIKI_BASE = "https://wiki.smite2.com"
WIKI_ITEMS_INDEX = f"{WIKI_BASE}/w/Items"

# Force unbuffered stdout so progress shows in real time when piped.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


# ── slugify --------------------------------------------------------------
# 'Bumba\'s Cudgel' → 'bumbas-cudgel'; 'Time-lock Aegis' → 'time-lock-aegis'
def slugify(display_name: str) -> str:
    s = display_name.lower().replace("'", "").replace("’", "")
    s = s.replace("_", "-").replace(" ", "-")
    # Collapse runs of hyphens to one — handles inputs like 'A--B' or
    # 'Time--lock' just in case the wiki ever changes spacing.
    s = re.sub(r"-+", "-", s).strip("-")
    return s


# ── parsing the index page ----------------------------------------------
# Wiki filenames look like:
#   Consumable_Health_Potion.png
#   Relic_Purification_Beads.png
#   Starter_T1_Bumba%27s_Cudgel.png
#   T1_Healing_Potion.png
#   T2_Asi.png       (actually T2_Bumba%27s_Spear etc — Asi happens to not exist in S2)
#   T3_The_Executioner.png
#   Curio_Bifrost_Shard.png
#   GodSpecific_Aladdin_Genie%27s_Lamp.png
#
# The category is the prefix before the first underscore. For
# Starter/T-tier items there's a TIER segment too. GodSpecific items
# embed the god display name (with underscores for spaces).

_ITEM_PREFIXES = ("Consumable", "Curio", "GodSpecific", "Relic",
                  "Starter", "T1", "T2", "T3")


def _decode_wiki_filename(src_attr: str) -> Optional[str]:
    """Pull the source filename out of a wiki <img src> attribute.

    Inputs look like ``/images/thumb/T3_Foo.png/30px-T3_Foo.png?abc`` or
    ``/images/T3_Foo.png?abc``. We want ``T3_Foo.png``.
    """
    # Strip query suffix
    src = src_attr.split("?", 1)[0]
    name = src.rsplit("/", 1)[-1]
    # Strip thumbnail size prefix like "30px-"
    name = re.sub(r"^\d+px-", "", name)
    if not name.endswith(".png"):
        return None
    return name


def _parse_item_entry(filename: str) -> Optional[Dict[str, object]]:
    """Parse a wiki filename into category + display name.

    Returns None for filenames that aren't item icons (page chrome,
    footer logos, etc). The `filename` arg is the raw URL-encoded
    form as it appears in the page HTML (e.g. T2_Magi%27s_Cloak.png);
    we decode it once here, both for parsing and for the source_file
    field that later code re-quotes for the download URL.
    """
    # URL-decode (the wiki HTML uses %27 for apostrophes)
    decoded = urllib.parse.unquote(filename)
    stem = decoded[:-len(".png")]  # strip extension

    parts = stem.split("_")
    if not parts:
        return None
    prefix = parts[0]
    if prefix not in _ITEM_PREFIXES:
        return None

    # Default carries
    category = prefix
    tier: Optional[str] = None
    god: Optional[str] = None
    name_parts: List[str]

    if prefix == "Starter":
        # Starter_T1_Bumba's_Cudgel -> tier=T1, name='Bumba's Cudgel'
        if len(parts) < 3 or parts[1] not in ("T1", "T2"):
            return None
        tier = parts[1]
        name_parts = parts[2:]
    elif prefix in ("T1", "T2", "T3"):
        # T3_The_Executioner -> tier=T3, name='The Executioner'
        # T2_Magi%27s_Cloak  -> tier=T2, name="Magi's Cloak"
        tier = prefix
        category = "Item"  # cleaner UI label than the raw tier
        name_parts = parts[1:]
    elif prefix == "GodSpecific":
        # GodSpecific_Aladdin_Genie's_Lamp -> god='Aladdin', name="Genie's Lamp"
        # GodSpecific_Hua_Mulan_Training_Grounds -> god='Hua Mulan', name='Training Grounds'
        # We don't have a perfect way to know where the god name ends
        # without a god list. Best-effort: known multi-word gods get
        # special-cased; otherwise the first segment is the god.
        rest = parts[1:]
        if len(rest) < 2:
            return None
        multi_word_gods = {("Hua", "Mulan"): "Hua Mulan",
                           ("Hou", "Yi"): "Hou Yi",
                           ("Da", "Ji"): "Da Ji",
                           ("Ne", "Zha"): "Ne Zha",
                           ("Nu", "Wa"): "Nu Wa",
                           ("Ah", "Muzen", "Cab"): "Ah Muzen Cab",
                           ("Sun", "Wukong"): "Sun Wukong",
                           ("Princess", "Bari"): "Princess Bari",
                           ("Baron", "Samedi"): "Baron Samedi",
                           ("The", "Morrigan"): "The Morrigan",
                           ("Morgan", "Le", "Fay"): "Morgan Le Fay",
                           ("Hun", "Batz"): "Hun Batz",
                           ("Guan", "Yu"): "Guan Yu",
                           ("Jing", "Wei"): "Jing Wei"}
        god_resolved = None
        for tup, label in multi_word_gods.items():
            n = len(tup)
            if tuple(rest[:n]) == tup:
                god_resolved = label
                name_parts = rest[n:]
                break
        if god_resolved is None:
            god_resolved = rest[0]
            name_parts = rest[1:]
        god = god_resolved
    else:
        # Consumable_*, Curio_*, Relic_*
        name_parts = parts[1:]

    if not name_parts:
        return None
    display_name = " ".join(name_parts)
    # Some wiki filenames preserve hyphens via literal hyphen in the
    # underscore-joined form (e.g. "Time-lock_Aegis"); a hyphen is
    # left intact by the split, so display_name is already correct.

    return {
        "display_name": display_name,
        "category": category,
        "tier": tier,
        "god": god,
        # Store the DECODED filename. download_icon() URL-encodes
        # exactly once when building the request URL, so storing the
        # raw HTML-encoded version here would double-encode and 404.
        "source_file": decoded,
    }


def fetch_index_html(session) -> str:
    r = session.get(WIKI_ITEMS_INDEX, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Wiki index returned HTTP {r.status_code}")
    return r.text


def parse_index(html: str) -> List[Dict[str, object]]:
    """Return one entry per item found on the wiki Items page.

    Skips non-item images (page chrome, footers). Deduplicates by
    source filename — the page sometimes lists the same item twice in
    different tables.
    """
    urls = re.findall(r'<img[^>]+src="([^"]+\.png[^"]*)"[^>]*>', html)
    seen_files: set = set()
    entries: List[Dict[str, object]] = []
    for u in urls:
        filename = _decode_wiki_filename(u)
        if not filename or filename in seen_files:
            continue
        entry = _parse_item_entry(filename)
        if not entry:
            continue
        seen_files.add(filename)
        entry["slug"] = slugify(str(entry["display_name"]))
        entries.append(entry)
    entries.sort(key=lambda e: (str(e["category"]),
                                 str(e.get("tier") or ""),
                                 str(e["display_name"])))
    return entries


# ── downloading ----------------------------------------------------------
def _try_decode_and_save_as_png(content: bytes, dest: Path) -> bool:
    """Open `content` with PIL and save as PNG. Returns True on success.

    Used to recover when the wiki serves a .png URL with WebP bytes
    (Rage was a known case). Returns False if PIL can't decode — that
    usually means we got a Cloudflare HTML challenge page instead of
    image data.
    """
    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(content)).convert("RGBA")
        img.save(dest, "PNG")
        return True
    except Exception:
        return False


def download_icon(session, entry: Dict[str, object], dest: Path,
                  verbose: bool = False) -> bool:
    """Fetch the full-resolution PNG and write to dest. Returns True on
    success. Logs the error and returns False otherwise."""
    src_file = str(entry["source_file"])
    # URL-encode the filename so apostrophes/spaces survive Cloudflare.
    url_path = urllib.parse.quote(src_file, safe="-_.")
    url = f"{WIKI_BASE}/images/{url_path}"
    if verbose:
        print(f"  GET {url}", flush=True)
    try:
        r = session.get(url, timeout=20)
    except Exception as exc:
        print(f"  [!] {entry['display_name']}: request failed — {exc}")
        return False
    if r.status_code != 200:
        print(f"  [!] {entry['display_name']}: HTTP {r.status_code}")
        return False
    # Wiki sometimes serves a .png URL with WebP bytes (e.g. T3_Rage.png).
    # Try PNG magic first (cheapest), else try to decode with PIL. PIL
    # decode failure usually means Cloudflare returned an HTML challenge
    # page — retry once after a brief sleep.
    if r.content.startswith(b"\x89PNG"):
        dest.write_bytes(r.content)
        return True
    if _try_decode_and_save_as_png(r.content, dest):
        if verbose:
            print(f"  (transcoded from non-PNG response)")
        return True
    # First decode attempt failed — likely rate-limited. Retry once.
    print(f"  [!] {entry['display_name']}: not a decodable image "
          f"(likely rate-limit), sleeping 5s and retrying once")
    time.sleep(5.0)
    try:
        r = session.get(url, timeout=20)
    except Exception as exc:
        print(f"  [!] {entry['display_name']}: retry failed — {exc}")
        return False
    if r.status_code != 200:
        print(f"  [!] {entry['display_name']}: retry HTTP {r.status_code}")
        return False
    if r.content.startswith(b"\x89PNG"):
        dest.write_bytes(r.content)
        return True
    if _try_decode_and_save_as_png(r.content, dest):
        if verbose:
            print(f"  (transcoded on retry)")
        return True
    print(f"  [!] {entry['display_name']}: retry still not a decodable image")
    return False


def write_manifest(entries: List[Dict[str, object]]) -> None:
    """Persist the parsed item metadata for the studio UI."""
    by_slug: Dict[str, Dict[str, object]] = {}
    for e in entries:
        slug = str(e["slug"])
        by_slug[slug] = {
            "display_name": e["display_name"],
            "category":     e["category"],
            "tier":         e.get("tier"),
            "god":          e.get("god"),
            "source_file":  e["source_file"],
        }
    MANIFEST_PATH.write_text(json.dumps(by_slug, indent=2, sort_keys=True),
                             encoding="utf-8")


# ── CLI ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="Re-download every icon, overwriting existing files.")
    ap.add_argument("--check", action="store_true",
                    help="List missing icons and exit (no downloads).")
    ap.add_argument("--only", default="",
                    help="Comma-separated list of display names to limit to.")
    ap.add_argument("--throttle", type=float, default=0.5,
                    help="Seconds to sleep between requests (default 0.5). "
                         "Anything below ~0.2 trips Cloudflare's rate limit, "
                         "which then returns an HTML challenge page instead "
                         "of the image (logged as 'not a PNG response').")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print every URL tried + status code.")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    session = _cffi_requests.Session(impersonate="chrome")

    print(f"[items] Fetching wiki index: {WIKI_ITEMS_INDEX}")
    html = fetch_index_html(session)
    entries = parse_index(html)
    print(f"[items] Parsed {len(entries)} items from index")
    if args.verbose:
        # Sanity-check distribution
        by_cat: Dict[str, int] = {}
        for e in entries:
            by_cat[str(e["category"])] = by_cat.get(str(e["category"]), 0) + 1
        for cat in sorted(by_cat):
            print(f"  {cat:14s}: {by_cat[cat]}")

    # --only filter
    only_set = {n.strip().lower() for n in args.only.split(",") if n.strip()}
    if only_set:
        entries = [e for e in entries
                   if str(e["display_name"]).lower() in only_set]
        print(f"[items] --only filter narrows to {len(entries)} items")

    if args.check:
        missing = [e for e in entries
                   if not (OUTPUT_DIR / f"{e['slug']}.png").exists()]
        if not missing:
            print("[items] No missing icons.")
            return 0
        print(f"[items] {len(missing)} missing icons:")
        for e in missing:
            print(f"  - {e['display_name']} ({e['category']})  → {e['slug']}.png")
        return 1

    # Download pass
    downloaded, skipped, failed = 0, 0, 0
    for i, entry in enumerate(entries, 1):
        slug = str(entry["slug"])
        dest = OUTPUT_DIR / f"{slug}.png"
        if dest.exists() and not args.force:
            skipped += 1
            continue
        print(f"[{i:3d}/{len(entries)}] {entry['display_name']}  →  {slug}.png")
        if download_icon(session, entry, dest, verbose=args.verbose):
            downloaded += 1
        else:
            failed += 1
        if args.throttle > 0:
            time.sleep(args.throttle)

    # Write manifest regardless of --force so it stays current with the
    # wiki even when no icons were re-downloaded.
    write_manifest(entries)
    print(f"[items] Manifest written: {MANIFEST_PATH.relative_to(REPO_ROOT)}")
    print(f"[items] Done. downloaded={downloaded} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
