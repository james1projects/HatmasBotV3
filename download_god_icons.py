"""
Download God Icons for Portrait Detection
==========================================
Downloads all Smite 2 god default icons for use by the in-game god
portrait matcher (core/god_matcher.py).

The definitive god list comes from core/god_roster.py, which fetches
the LIVE https://wiki.smite2.com/w/Gods page on every run — new god
releases show up here without re-saving any HTML. The old saved
"Gods - SMITE 2 Wiki.html" page is kept only as an offline fallback
list source and a local thumbnail source.

Sources (tried in order for each god):
  1. wiki.smite2.com direct image URL — 256x256 S2 art
  2. Local saved wiki page files — 96px S2 art thumbnails (from the saved HTML)
  3. smite.fandom.com MediaWiki API — 256x256 (may be S1 art for some gods)
  4. tracker.gg CDN — 256x256 S2 art (missing newly released gods)

Icons are saved in data/god_icons/. New releases (per the roster's
first_seen date) are downloaded first.

data/god_icons/ holds ONLY SMITE 2 art — it feeds the portrait
matcher's fingerprints, so SMITE 1 art in there would cause false
matches. The --s1 flag downloads the full 130-god SMITE 1 icon
catalog into the separate data/god_icons_s1/ folder, used purely as
display fallback (OBS god-request portrait) for gods whose SMITE 2
art doesn't exist yet.

Usage:
    python download_god_icons.py              # Download all (live list)
    python download_god_icons.py --offline    # Skip the live wiki fetch
    python download_god_icons.py --force      # Re-download even if present
    python download_god_icons.py --check      # List missing icons
    python download_god_icons.py --add "God Name"  # Add a single god
    python download_god_icons.py --s1         # SMITE 1 catalog -> god_icons_s1/
"""

import os
import sys
import json
import re
import time
import shutil
import argparse
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from core.config import SMITE2_GOD_IMAGE_BASE
from core import god_roster

OUTPUT_DIR = Path(__file__).parent / "data" / "god_icons"
S1_OUTPUT_DIR = Path(__file__).parent / "data" / "god_icons_s1"
WIKI_HTML = Path(__file__).parent / "assets" / "smite2_wiki" / "Gods - SMITE 2 Wiki.html"
WIKI_FILES_DIR = Path(__file__).parent / "assets" / "smite2_wiki" / "Gods - SMITE 2 Wiki_files"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SMITE2_WIKI_BASE = "https://wiki.smite2.com/images"
FANDOM_API = "https://smite.fandom.com/api.php"


# ============================================================
# WIKI HTML PARSING — source of truth for the god list
# ============================================================

def parse_wiki_html():
    """
    Parse the saved "Gods - SMITE 2 Wiki.html" to extract all god
    icon filenames and display names.

    Returns: list of dicts [{name, wiki_filename, slug}, ...]
    """
    if not WIKI_HTML.exists():
        print(f"[!] Wiki HTML not found: {WIKI_HTML}")
        print(f"    Save https://wiki.smite2.com/w/Gods as 'Webpage, Complete'")
        return []

    with open(WIKI_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    # Extract icon filenames: File:T_SomeName(S2)_Default_Icon.png
    file_pattern = r'File:(T_[^"]+Default_Icon\.png)'
    raw_filenames = sorted(set(re.findall(file_pattern, html)))

    # Map filename → god name
    gods = []
    for wiki_filename in raw_filenames:
        name = _wiki_filename_to_name(wiki_filename)
        slug = _name_to_slug(name)
        gods.append({
            "name": name,
            "wiki_filename": wiki_filename,
            "slug": slug,
        })

    return gods


def _wiki_filename_to_name(filename):
    """
    Convert a wiki icon filename back to a display name.

    T_Ra(S2)_Default_Icon.png          → Ra
    T_Hou_Yi(S2)_Default_Icon.png      → Hou Yi
    T_MorganLeFay(S2)_Default_Icon.png → Morgan Le Fay
    T_CuChulainn(S2)_Default_Icon.png  → Cu Chulainn

    Delegates to god_roster's generic camel-splitter — the old
    hand-maintained concat_map here mangled every new concatenated
    name (Xing Tian came out as "Xingtian").
    """
    return god_roster.wiki_filename_to_name(filename)


def _name_to_slug(name):
    """Convert display name to file slug: lowercase, spaces→hyphens, strip apostrophes."""
    return god_roster.name_to_slug(name)


# ============================================================
# GOD LIST — roster-backed, live-refreshed
# ============================================================

def get_god_list(offline=False):
    """
    The god list to download, newest releases first.

    Tries a live wiki refresh through core/god_roster.py (so brand-new
    gods appear without any manual step), then falls back to the
    roster cache / bundled snapshot, then to the saved wiki HTML.
    """
    if not offline:
        added = god_roster.refresh(force=True)
        for g in added:
            print(f"[+] New god on the wiki: {g['name']}")

    gods = god_roster.gods()
    if not gods:
        return parse_wiki_html()

    fresh = [g for g in gods if g.get("first_seen")]
    fresh.sort(key=lambda g: g["first_seen"], reverse=True)
    rest = sorted((g for g in gods if not g.get("first_seen")),
                  key=lambda g: g["slug"])
    return fresh + rest


# ============================================================
# DOWNLOAD SOURCES
# ============================================================

def _download_url(url):
    """Download bytes from a URL. Returns bytes or None."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/*,*/*",
        })
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.read()
    except Exception:
        return None


def try_smite2_wiki_direct(wiki_filename):
    """
    Source 1: Direct download from wiki.smite2.com/images/.
    Constructs the URL from the known filename.
    Returns image bytes or None.
    """
    encoded = urllib.parse.quote(wiki_filename, safe="")
    url = f"{SMITE2_WIKI_BASE}/{encoded}"
    return _download_url(url)


def try_local_wiki_files(wiki_filename):
    """
    Source 2: Copy from the locally saved wiki page files.
    The saved HTML's companion folder has 96px thumbnails.
    Returns image bytes or None.
    """
    # The local files are named: 96px-T_Name(S2)_Default_Icon.png
    local_name = f"96px-{wiki_filename}"
    local_path = WIKI_FILES_DIR / local_name

    if local_path.exists():
        with open(local_path, "rb") as f:
            return f.read()
    return None


def try_fandom_wiki(wiki_filename):
    """
    Source 3: Download from smite.fandom.com MediaWiki API.
    Returns image bytes or None.
    """
    # Try exact filename first, then without (S2)
    filenames_to_try = [wiki_filename]
    alt = wiki_filename.replace("(S2)_", "_")
    if alt != wiki_filename:
        filenames_to_try.append(alt)

    for fname in filenames_to_try:
        file_title = f"File:{fname}"
        params = urllib.parse.urlencode({
            "action": "query",
            "titles": file_title,
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json",
        })
        try:
            req = urllib.request.Request(
                f"{FANDOM_API}?{params}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read().decode())
            pages = data.get("query", {}).get("pages", {})

            for page_id, page_data in pages.items():
                if int(page_id) < 0:
                    continue
                imageinfo = page_data.get("imageinfo", [])
                if imageinfo:
                    url = imageinfo[0].get("url", "")
                    if url:
                        img_data = _download_url(url)
                        if img_data:
                            return img_data
        except Exception:
            pass

    return None


def fetch_fandom_file(filename):
    """
    Resolve an exact fandom wiki filename to its image URL via the
    MediaWiki API and download it. Returns bytes or None. Fandom is
    not Cloudflare-challenged, so plain urllib works here.
    """
    params = urllib.parse.urlencode({
        "action": "query",
        "titles": f"File:{filename}",
        "prop": "imageinfo",
        "iiprop": "url",
        "format": "json",
    })
    try:
        req = urllib.request.Request(
            f"{FANDOM_API}?{params}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        for page_id, page_data in data.get("query", {}).get("pages", {}).items():
            if int(page_id) < 0:
                continue
            imageinfo = page_data.get("imageinfo", [])
            if imageinfo and imageinfo[0].get("url"):
                return _download_url(imageinfo[0]["url"])
    except Exception:
        pass
    return None


def download_s1_library(force=False):
    """
    Download the full SMITE 1 icon catalog (130 gods) from
    smite.fandom.com into data/god_icons_s1/. Display-only fallback
    art — deliberately a separate folder from data/god_icons/, which
    feeds the portrait matcher and must stay SMITE 2 art only.
    """
    catalog = god_roster.smite1_catalog()
    S1_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"SMITE 1 catalog: {len(catalog)} gods")
    print(f"Output: {S1_OUTPUT_DIR}/\n")

    stats = {"downloaded": 0, "exists": 0, "missing": 0}
    for entry in catalog:
        output_path = S1_OUTPUT_DIR / f"{entry['slug']}.png"
        if not force and output_path.exists():
            stats["exists"] += 1
            continue
        data = fetch_fandom_file(entry["s1_icon"])
        if data and len(data) > 500:
            _save_icon(output_path, data)
            stats["downloaded"] += 1
            print(f"  [S1] {entry['name']}")
        else:
            stats["missing"] += 1
            print(f"  [--] {entry['name']} (not found)")
        time.sleep(0.15)

    print(f"\nDone! downloaded={stats['downloaded']} "
          f"already had={stats['exists']} missing={stats['missing']}")


def try_tracker_cdn(slug):
    """
    Source 4: tracker.gg CDN fallback.
    Returns image bytes or None.
    """
    candidates = [slug]
    if "-" in slug:
        candidates.append(slug.replace("-", ""))

    for candidate in candidates:
        url = f"{SMITE2_GOD_IMAGE_BASE}/{candidate}.jpg"
        data = _download_url(url)
        if data:
            return data
    return None


# ============================================================
# MAIN DOWNLOAD LOGIC
# ============================================================

def download_god(god_info, force=False):
    """
    Download one god's icon, trying all sources in order.
    Returns the source name or None.
    """
    slug = god_info["slug"]
    wiki_filename = god_info["wiki_filename"]
    output_path = OUTPUT_DIR / f"{slug}.png"

    if not force and output_path.exists():
        return "exists"

    # Source 1: wiki.smite2.com direct URL (256px, S2 art — best quality)
    data = try_smite2_wiki_direct(wiki_filename)
    if data and len(data) > 500:  # sanity check (not an error page)
        _save_icon(output_path, data)
        return "wiki.smite2"

    # Source 2: Local saved wiki files (96px, S2 art — accurate icons)
    data = try_local_wiki_files(wiki_filename)
    if data and len(data) > 500:
        _save_icon(output_path, data)
        return "local"

    # Source 3: tracker.gg CDN (256px, but often outdated S1 icons)
    data = try_tracker_cdn(slug)
    if data and len(data) > 500:
        _save_icon(output_path, data)
        return "cdn"

    # Source 4: Fandom wiki API (may be S1 art, last resort)
    data = try_fandom_wiki(wiki_filename)
    if data and len(data) > 500:
        _save_icon(output_path, data)
        return "fandom"

    return None


def _save_icon(path, data):
    """Save icon bytes to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Download Smite 2 god icons")
    parser.add_argument("--force", action="store_true", help="Re-download all")
    parser.add_argument("--check", action="store_true", help="Check which are missing")
    parser.add_argument("--add", type=str, help="Add a single god by name")
    parser.add_argument("--offline", action="store_true",
                        help="Skip the live wiki fetch (roster cache/bundled list)")
    parser.add_argument("--s1", action="store_true",
                        help="Download the SMITE 1 catalog into data/god_icons_s1/")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.s1:
        download_s1_library(force=args.force)
        return

    # Live-refreshed roster (falls back to cache/bundled, then saved HTML)
    gods = get_god_list(offline=args.offline)
    if not gods:
        print("No gods found. Make sure 'Gods - SMITE 2 Wiki.html' exists.")
        print("Save https://wiki.smite2.com/w/Gods as 'Webpage, Complete'.")
        return

    # Single god mode
    if args.add:
        target = args.add.lower()
        god_info = None
        for g in gods:
            if g["name"].lower() == target or g["slug"] == _name_to_slug(target):
                god_info = g
                break
        if not god_info:
            # Construct manually for gods not on the saved page
            name = args.add.title()
            slug = _name_to_slug(args.add)
            wiki_filename = f"T_{name.replace(' ', '_')}(S2)_Default_Icon.png"
            god_info = {"name": name, "slug": slug, "wiki_filename": wiki_filename}
            print(f"Not found in saved wiki page, trying constructed filename: {wiki_filename}")

        print(f"Adding: {god_info['name']} ({god_info['wiki_filename']})")
        source = download_god(god_info, force=True)
        if source and source != "exists":
            print(f"  Downloaded from {source}")
        else:
            print(f"  Not found on any source.")
        return

    # Check mode
    if args.check:
        missing = [g for g in gods if not (OUTPUT_DIR / f"{g['slug']}.png").exists()]
        if missing:
            print(f"{len(missing)} of {len(gods)} gods missing:")
            for g in missing:
                print(f"  - {g['name']}")
        else:
            print(f"All {len(gods)} gods present")
        return

    # Full download
    print(f"Gods from wiki: {len(gods)}")
    print(f"Output: {OUTPUT_DIR}/")
    print(f"Sources: wiki.smite2.com > local files > tracker.gg CDN > fandom\n")

    stats = {"wiki.smite2": 0, "local": 0, "fandom": 0, "cdn": 0, "exists": 0, "missing": 0}

    for god_info in gods:
        source = download_god(god_info, force=args.force)

        if source == "exists":
            stats["exists"] += 1
        elif source:
            stats[source] += 1
            label = {
                "wiki.smite2": "[S2Wiki]",
                "local": "[Local] ",
                "cdn": "[CDN]   ",
                "fandom": "[Fandom]",
            }.get(source, f"[{source}]")
            print(f"  {label} {god_info['name']}")
        else:
            stats["missing"] += 1
            print(f"  [--]     {god_info['name']} (not found)")

        time.sleep(0.1)

    total = len(list(OUTPUT_DIR.glob("*.png")))
    print(f"\nDone!")
    for src in ["wiki.smite2", "local", "cdn", "fandom"]:
        if stats[src]:
            print(f"  {src}: {stats[src]}")
    print(f"  Already had: {stats['exists']}")
    if stats["missing"]:
        print(f"  Not found: {stats['missing']}")
    print(f"  Total icon files: {total}")

    if stats["missing"]:
        print(f"\nTo add a new god: python download_god_icons.py --add \"God Name\"")


if __name__ == "__main__":
    main()
