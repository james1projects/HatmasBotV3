#!/usr/bin/env python3
"""
Download all Smite god voice lines from the fandom wiki.

Usage:
    python tools/download_voicelines.py                  # Download all gods
    python tools/download_voicelines.py achilles zeus    # Download specific gods
    python tools/download_voicelines.py --list           # List available gods
    python tools/download_voicelines.py --resume         # Skip already-downloaded gods

Voice lines are saved to data/smite_voicelines/<god_name>/ with subfolders
for each category. Jokes, Taunts, and Directed Taunts get their own folders.

File structure example:
    data/smite_voicelines/achilles/
        god_selection/
        introduction/
        abilities/
        low_health/
        items/
        kills/
        death/
        taunts/            <-- separated
        directed_taunts/   <-- separated
        jokes/             <-- separated
        laughs/
        vgs/
"""

import argparse
import re
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from curl_cffi import requests as cffi_requests
    _SESSION = cffi_requests.Session(impersonate="chrome")
    _USE_CURL_CFFI = True
except ImportError:
    import urllib.request
    import urllib.error
    _USE_CURL_CFFI = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://smite.fandom.com/wiki"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "smite_voicelines"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
MAX_WORKERS = 5          # concurrent downloads per god
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2          # seconds between retries
REQUEST_DELAY = 0.1      # small delay between page fetches to be polite

# Map wiki section IDs to folder names.  h2 sections are the primary
# categories; h3 sections are subcategories that get merged into the
# parent folder unless they have their own mapping here.
#
# Jokes, Taunts, Directed Taunts are separated per user request.

SECTION_FOLDER_MAP = {
    # Top-level h2 sections
    "God_Selection":     "god_selection",
    "Introduction":      "introduction",
    "Abilities":         "abilities",
    "Low_Health":        "low_health",
    "Items":             "items",
    "Kills":             "kills",
    "Death":             "death",
    "Taunts":            "taunts",
    "Directed_Taunts":   "directed_taunts",
    "Jokes":             "jokes",
    "Laughs":            "laughs",
    "VGS":               "vgs",
    "Other":             "other",
}

# h3 subsections that should be merged into their parent h2 folder.
# Anything not listed here also falls back to parent.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(url: str, retries: int = RETRY_ATTEMPTS) -> bytes:
    """Fetch URL with retries. Uses curl_cffi (Cloudflare bypass) if available."""
    for attempt in range(retries):
        try:
            if _USE_CURL_CFFI:
                resp = _SESSION.get(url, timeout=30)
                resp.raise_for_status()
                return resp.content
            else:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                raise RuntimeError(f"Failed to fetch {url}: {e}") from e


def fetch_god_list() -> list[str]:
    """Fetch the master list of god names from the voicelines index page."""
    html = _make_request(f"{BASE_URL}/God_voicelines").decode("utf-8")
    pattern = r'href="/wiki/([^"]+)_voicelines"'
    gods = sorted(set(re.findall(pattern, html)))
    return gods


def _section_id_to_folder(section_id: str, parent_h2: str | None) -> str:
    """Convert a wiki section ID to a folder name."""
    # Direct match in our map
    if section_id in SECTION_FOLDER_MAP:
        return SECTION_FOLDER_MAP[section_id]

    # h3 subsection — merge into parent h2 folder
    if parent_h2 and parent_h2 in SECTION_FOLDER_MAP:
        return SECTION_FOLDER_MAP[parent_h2]

    # Fallback: clean up the ID into a folder-safe name
    clean = section_id.replace("_-_", "_").strip("_()").lower()
    clean = re.sub(r"[^a-z0-9_]", "_", clean)
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean or "other"


def parse_voiceline_page(html: str) -> dict[str, list[tuple[str, str]]]:
    """
    Parse a god voiceline page and return a dict of:
        folder_name -> [(ogg_url, filename), ...]
    """
    # Build ordered list of all h2 and h3 headers with positions
    h2_pattern = r"<h2>.*?<span class=\"mw-headline\"[^>]*id=\"([^\"]+)\""
    h3_pattern = r"<h3>.*?<span class=\"mw-headline\"[^>]*id=\"([^\"]+)\""

    h2s = [(m.start(), "h2", m.group(1)) for m in re.finditer(h2_pattern, html, re.DOTALL)]
    h3s = [(m.start(), "h3", m.group(1)) for m in re.finditer(h3_pattern, html, re.DOTALL)]
    all_headers = sorted(h2s + h3s, key=lambda x: x[0])

    # Find all audio source URLs with positions
    audio_pattern = r'src="(https://static\.wikia\.nocookie\.net/[^"]*\.ogg[^"]*)"'
    audios = [(m.start(), m.group(1)) for m in re.finditer(audio_pattern, html)]

    # Map each audio to its section
    result: dict[str, list[tuple[str, str]]] = {}
    current_h2 = None

    for apos, aurl in audios:
        # Find which section this audio belongs to
        section_id = "other"
        parent_h2 = None
        for hpos, hlevel, hid in all_headers:
            if hpos > apos:
                break
            if hlevel == "h2":
                current_h2 = hid
            section_id = hid
            parent_h2 = current_h2

        folder = _section_id_to_folder(section_id, parent_h2)

        # Extract filename from URL
        # URL: .../images/d/de/Achilles_Select.ogg/revision/latest?cb=...
        ogg_match = re.search(r"/([^/]+\.ogg)", aurl)
        if not ogg_match:
            continue
        filename = ogg_match.group(1)

        if folder not in result:
            result[folder] = []
        result[folder].append((aurl, filename))

    return result


def download_file(url: str, dest: Path) -> bool:
    """Download a single file. Returns True on success."""
    if dest.exists() and dest.stat().st_size > 0:
        return True  # already downloaded
    try:
        data = _make_request(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception as e:
        print(f"    FAILED: {dest.name} - {e}")
        return False


def download_god(god_slug: str, resume: bool = False) -> tuple[int, int]:
    """
    Download all voice lines for a single god.
    Returns (success_count, fail_count).
    """
    god_dir = OUTPUT_DIR / god_slug.lower()

    # Check if already fully downloaded (resume mode)
    marker = god_dir / ".download_complete"
    if resume and marker.exists():
        print(f"  Skipping {god_slug} (already complete)")
        return (0, 0)

    # Fetch the voiceline page
    page_url = f"{BASE_URL}/{god_slug}_voicelines"
    try:
        html = _make_request(page_url).decode("utf-8")
    except RuntimeError as e:
        print(f"  ERROR fetching page for {god_slug}: {e}")
        return (0, 1)

    # Parse sections and audio URLs
    sections = parse_voiceline_page(html)
    if not sections:
        print(f"  WARNING: No voice lines found for {god_slug}")
        return (0, 0)

    total_files = sum(len(files) for files in sections.values())
    print(f"  Found {total_files} voice lines across {len(sections)} categories")

    # Download all files with thread pool
    success = 0
    failed = 0
    tasks = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for folder, files in sections.items():
            folder_path = god_dir / folder
            folder_path.mkdir(parents=True, exist_ok=True)
            for url, filename in files:
                dest = folder_path / filename
                tasks.append(pool.submit(download_file, url, dest))

        for future in as_completed(tasks):
            if future.result():
                success += 1
            else:
                failed += 1

    # Write completion marker
    if failed == 0:
        marker.write_text(f"completed at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    return (success, failed)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Smite god voice lines from the fandom wiki."
    )
    parser.add_argument(
        "gods", nargs="*",
        help="Specific god names to download (e.g. achilles zeus). "
             "If omitted, downloads all gods."
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all available gods and exit."
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip gods that have already been fully downloaded."
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Override output directory (default: data/smite_voicelines)."
    )
    args = parser.parse_args()

    global OUTPUT_DIR
    if args.output:
        OUTPUT_DIR = Path(args.output)

    print("Fetching god list from wiki...")
    all_gods = fetch_god_list()
    print(f"Found {len(all_gods)} gods on the wiki.\n")

    if args.list:
        for g in all_gods:
            print(f"  {g}")
        return

    # Filter to requested gods
    if args.gods:
        # Match case-insensitively
        requested = {g.lower().replace(" ", "_") for g in args.gods}
        gods = [g for g in all_gods if g.lower() in requested]
        not_found = requested - {g.lower() for g in gods}
        if not_found:
            print(f"WARNING: Gods not found on wiki: {', '.join(not_found)}")
            # Try with first letter capitalized
            for nf in list(not_found):
                cap = nf.replace("_", " ").title().replace(" ", "_")
                matches = [g for g in all_gods if g.lower() == nf]
                if matches:
                    gods.extend(matches)
                    not_found.discard(nf)
            if not_found:
                print(f"  Could not match: {', '.join(not_found)}")
    else:
        gods = all_gods

    if not gods:
        print("No gods to download.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Downloading voice lines for {len(gods)} gods...\n")

    total_success = 0
    total_failed = 0
    start_time = time.time()

    for i, god in enumerate(gods, 1):
        print(f"[{i}/{len(gods)}] {god.replace('_', ' ')}")
        success, failed = download_god(god, resume=args.resume)
        total_success += success
        total_failed += failed

        # Be polite to the wiki
        if i < len(gods):
            time.sleep(REQUEST_DELAY)

    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Downloaded: {total_success} files")
    if total_failed:
        print(f"  Failed: {total_failed} files")
    print(f"  Location: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
