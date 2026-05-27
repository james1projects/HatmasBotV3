"""
Download God Cards (400x600 splash / loading-screen art)
=========================================================
Downloads the full-body 400x600 god card art from wiki.smite2.com for
every Smite 2 god. Used by tools/build_thumbnail.py to compose YouTube
thumbnails.

Companion to download_god_icons.py. Both tools share the same source of
truth (the saved "Gods - SMITE 2 Wiki.html" page); this one differs in
that it pulls the *card* art rather than the small icon.

Why we don't HTML-scrape the per-god wiki pages
-----------------------------------------------
First version of this tool fetched each god's individual wiki page and
read its og:image meta tag, because the SMITE 2 wiki uses three
different filename conventions for the same kind of asset:

    Ra:       T_RaS2_Default.png          (no parens, T_ prefix)
    Hou Yi:   SkinArt_Hou_YiS2_Default.png (SkinArt_ prefix)
    Ymir:     T_Ymir(S2)_Default.png       (with parens, T_ prefix)

That worked for me on the read-side (og:image is canonical) but failed
in practice for downloads — the wiki HTML routes appear to be
Cloudflare-challenged from urllib.request, while the /images/ static
endpoints are CDN-cached and respond fine. So now we *construct* image
URLs directly from the icon filenames the existing parser already
gives us, trying all four observed patterns. og:image scraping is kept
as a last-ditch fallback (off by default — enable with --use-og-scrape
if you want it).

Image URLs are saved to data/god_cards/<slug>.png at native 400x600.

Usage:
    python tools/download_god_cards.py                  # Download all
    python tools/download_god_cards.py -v               # Verbose: show every URL tried
    python tools/download_god_cards.py --force          # Re-download everything
    python tools/download_god_cards.py --check          # List missing cards
    python tools/download_god_cards.py --add "God Name" # Add a single god
    python tools/download_god_cards.py --only "Ymir,Loki,Hou Yi"
    python tools/download_god_cards.py --use-og-scrape  # Enable HTML og:image fallback
"""

import os
import sys
import re
import time
import argparse
import traceback
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# Force unbuffered stdout so progress shows up in real time even when
# output is piped or redirected. (cmd.exe sometimes buffers heavily.)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# wiki.smite2.com is fronted by Cloudflare, which blocks plain
# urllib.request with HTTP 403 because the TLS / HTTP/2 fingerprint
# doesn't look like a real browser. curl_cffi mimics Chrome's
# fingerprint exactly and gets through. Same trick that
# tools/download_voicelines.py uses for tracker.gg.
try:
    from curl_cffi import requests as _cffi_requests
    _CFFI_SESSION = _cffi_requests.Session(impersonate="chrome")
    _USE_CURL_CFFI = True
except Exception:
    _CFFI_SESSION = None
    _USE_CURL_CFFI = False

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Reuse the wiki HTML parser + slug helpers from the icon downloader.
# Wrap the import in a try/except so any failure shows up immediately
# instead of silently exiting before main() ever runs.
import importlib.util as _importlib_util
try:
    _icon_spec = _importlib_util.spec_from_file_location(
        "_download_god_icons", REPO_ROOT / "download_god_icons.py"
    )
    _icon_mod = _importlib_util.module_from_spec(_icon_spec)
    _icon_spec.loader.exec_module(_icon_mod)
    parse_wiki_html = _icon_mod.parse_wiki_html
    _name_to_slug = _icon_mod._name_to_slug
except Exception as _exc:
    print(f"[!] Failed to import download_god_icons.py: "
          f"{type(_exc).__name__}: {_exc}", file=sys.stderr)
    traceback.print_exc()
    sys.exit(2)

OUTPUT_DIR = REPO_ROOT / "data" / "god_cards"
WIKI_BASE = "https://wiki.smite2.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Sanity floor for downloaded image bytes.
MIN_VALID_BYTES = 5_000


# ============================================================
# NETWORK
# ============================================================

def _http_get(url, *, timeout=15, verbose=False):
    """
    GET a URL. Returns (status_code, body_bytes) or (None, None).
    Uses curl_cffi (Chrome TLS fingerprint) when available so Cloudflare
    doesn't 403 us; falls back to urllib.request otherwise (will fail
    against wiki.smite2.com).
    """
    if _USE_CURL_CFFI:
        try:
            resp = _CFFI_SESSION.get(url, timeout=timeout, allow_redirects=True)
            body = resp.content or b""
            if verbose:
                print(f"      GET {url} -> {resp.status_code} ({len(body)} bytes)")
            return resp.status_code, body
        except Exception as exc:
            if verbose:
                print(f"      GET {url} -> {type(exc).__name__}: {exc}")
            return None, None

    # Fallback: urllib.request — usually 403s against Cloudflare-fronted hosts.
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,image/*,*/*",
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = resp.read()
        if verbose:
            print(f"      GET {url} -> {resp.status} ({len(body)} bytes)")
        return resp.status, body
    except urllib.error.HTTPError as exc:
        if verbose:
            print(f"      GET {url} -> HTTP {exc.code}")
        return exc.code, None
    except Exception as exc:
        if verbose:
            print(f"      GET {url} -> {type(exc).__name__}: {exc}")
        return None, None


# ============================================================
# CARD URL DERIVATION
# ============================================================

ICON_NAME_RE = re.compile(r'^T_(?P<name>.+?)(?:\(S2\))?_Default_Icon\.png$')


def extract_wiki_name(wiki_filename):
    """
    'T_Hou_Yi(S2)_Default_Icon.png' -> 'Hou_Yi'
    'T_Ra(S2)_Default_Icon.png'     -> 'Ra'
    'T_Atlas_Default_Icon.png'      -> 'Atlas'
    'T_DaJi(S2)_Default_Icon.png'   -> 'DaJi'
    """
    m = ICON_NAME_RE.match(wiki_filename or "")
    return m.group("name") if m else None


def construct_card_urls(wiki_name, display_name=None):
    """
    Build the candidate card-art URLs for a god.

    The wiki has FIVE independent dimensions of variation observed in
    the wild:

    1. Filename pattern (5 templates):
         T_<Name>(S2)_Default       (Ymir, Da Ji card path)
         T_<Name>S2_Default         (Ra, Baron Samedi card path)
         SkinArt_<Name>S2_Default   (Hou Yi card path)
         SkinArt_<Name>(S2)_Default (catch-all)
         GodCard_<Name>             (Guan Yu — no S2, no parens)
    2. Extension: .png OR .jpg (Anubis is stored as .jpg).
    3. Name form for multi-word gods: underscored vs concatenated. The
       *icon* path is inconsistent — sometimes underscored
       (T_Baron_Samedi(S2)_..., T_Hou_Yi(S2)_...) and sometimes
       concatenated (T_DaJi(S2)_..., T_JingWei(S2)_...). The *card*
       path is independently inconsistent — Baron Samedi's card uses
       'BaronSamedi' (concat), Da Ji's card uses 'Da_Ji' (underscore),
       Hou Yi's card uses 'Hou_Yi' (underscore). Always try both forms.

    The `display_name` argument lets us derive the underscored form
    even when the icon's wiki_name is concatenated (so 'Da Ji' display
    + 'DaJi' wiki_name still produces 'Da_Ji' as a candidate form).
    """
    if not wiki_name:
        return []

    # Build the set of name-form variants. We use a set then sort by
    # insertion to dedupe but keep ordering stable.
    forms_seen = set()
    name_forms = []

    def add(form):
        if form and form not in forms_seen:
            forms_seen.add(form)
            name_forms.append(form)

    add(wiki_name)
    if "_" in wiki_name:
        add(wiki_name.replace("_", ""))           # underscored -> concat
    if display_name:
        add(display_name.replace(" ", "_"))       # 'Da Ji'      -> 'Da_Ji'
        add(display_name.replace(" ", ""))        # 'Da Ji'      -> 'DaJi'

    stem_templates = [
        "T_{n}(S2)_Default",
        "T_{n}S2_Default",
        "SkinArt_{n}S2_Default",
        "SkinArt_{n}(S2)_Default",
        "GodCard_{n}",
    ]

    candidates = []
    for n in name_forms:
        for tmpl in stem_templates:
            stem = tmpl.format(n=n)
            for ext in (".png", ".jpg"):
                candidates.append(f"{WIKI_BASE}/images/{stem}{ext}")
    return [urllib.parse.quote(u, safe=":/()_") for u in candidates]


# ============================================================
# OG:IMAGE FALLBACK (off by default)
# ============================================================

OG_IMAGE_RE = re.compile(
    r'<meta\s+property="og:image"\s+content="([^"]+)"',
    re.IGNORECASE,
)


def _wiki_url_path_for(name):
    """'Ra' -> 'Ra', 'Hou Yi' -> 'Hou_Yi', 'Morgan Le Fay' -> 'Morgan_Le_Fay'."""
    return urllib.parse.quote(name.replace(" ", "_"), safe="()_")


def discover_card_url_via_og(name, *, verbose=False):
    """Last-ditch fallback: scrape og:image from the god's wiki page."""
    page_url = f"{WIKI_BASE}/w/{_wiki_url_path_for(name)}"
    status, body = _http_get(page_url, verbose=verbose)
    if status != 200 or not body:
        return None
    try:
        html = body.decode("utf-8", errors="replace")
    except Exception:
        return None
    match = OG_IMAGE_RE.search(html)
    if not match:
        return None
    url = match.group(1).split("?", 1)[0]
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = WIKI_BASE + url
    return url


# ============================================================
# DOWNLOAD
# ============================================================

def download_card(god, *, force=False, use_og_scrape=False, verbose=False):
    """
    Try to download one god's card to data/god_cards/<slug>.png.
    `god` is a dict with keys: name, slug, wiki_filename.
    Returns one of: 'direct', 'og', 'exists', None.
    """
    slug = god["slug"]
    output_path = OUTPUT_DIR / f"{slug}.png"
    if not force and output_path.exists():
        return "exists"

    wiki_name = extract_wiki_name(god.get("wiki_filename", ""))
    display_name = god.get("name", "")
    if verbose:
        print(f"    wiki_name='{wiki_name}', display_name='{display_name}'")
    for url in construct_card_urls(wiki_name, display_name):
        status, body = _http_get(url, verbose=verbose)
        if status == 200 and body and len(body) >= MIN_VALID_BYTES:
            _save(output_path, body)
            return "direct"

    if use_og_scrape:
        url = discover_card_url_via_og(god["name"], verbose=verbose)
        if url:
            status, body = _http_get(url, verbose=verbose)
            if status == 200 and body and len(body) >= MIN_VALID_BYTES:
                _save(output_path, body)
                return "og"
    return None


def _is_jpeg(data):
    return len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF


def _is_png(data):
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def _save(path, data):
    """Save bytes to disk; transcode JPEG to PNG so on-disk is always <slug>.png."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_png(data):
        with open(path, "wb") as f:
            f.write(data)
        return
    if _is_jpeg(data):
        try:
            from PIL import Image
            from io import BytesIO
            Image.open(BytesIO(data)).convert("RGB").save(path, "PNG", optimize=True)
            return
        except Exception:
            pass
    with open(path, "wb") as f:
        f.write(data)


def _resolve_god_list(gods, only_arg):
    wanted = {g.strip().lower() for g in only_arg.split(",") if g.strip()}
    if not wanted:
        return gods
    return [g for g in gods if g["name"].lower() in wanted]


def _build_god_dict_for_add(name):
    title = name.title()
    wiki_filename = f"T_{title.replace(' ', '_')}(S2)_Default_Icon.png"
    return {
        "name": title,
        "slug": _name_to_slug(name),
        "wiki_filename": wiki_filename,
    }


def main():
    parser = argparse.ArgumentParser(description="Download Smite 2 god card art")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--add", type=str)
    parser.add_argument("--only", type=str)
    parser.add_argument("--throttle", type=float, default=0.4)
    parser.add_argument("--use-og-scrape", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    print(f"[i] CWD: {os.getcwd()}")
    print(f"[i] REPO_ROOT: {REPO_ROOT}")
    print(f"[i] OUTPUT_DIR: {OUTPUT_DIR}")
    if _USE_CURL_CFFI:
        print(f"[i] HTTP client: curl_cffi (Chrome TLS fingerprint)")
    else:
        print(f"[!] HTTP client: urllib.request - wiki.smite2.com will 403.")
        print(f"[!] Install curl_cffi: pip install curl_cffi")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.add:
        try:
            gods = parse_wiki_html()
        except Exception as exc:
            print(f"[!] parse_wiki_html() raised: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            traceback.print_exc()
            return
        target = args.add.strip()
        god = next((g for g in gods if g["name"].lower() == target.lower()), None)
        if not god:
            god = _build_god_dict_for_add(target)
            print(f"Not in parsed wiki HTML; constructed wiki_filename: "
                  f"{god['wiki_filename']}")
        print(f"Downloading card for: {god['name']} (slug={god['slug']})")
        source = download_card(god, force=True,
                               use_og_scrape=args.use_og_scrape,
                               verbose=args.verbose)
        if source == "exists":
            print(f"  Already present at {OUTPUT_DIR / (god['slug'] + '.png')}")
        elif source:
            print(f"  Saved (source: {source})")
        else:
            print(f"  Not found. Try -v to see URLs tried.")
        return

    try:
        gods = parse_wiki_html()
    except Exception as exc:
        print(f"[!] parse_wiki_html() raised: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        traceback.print_exc()
        return

    if not gods:
        print("No gods found. Make sure 'Gods - SMITE 2 Wiki.html' is current.")
        return

    print(f"[i] Parsed {len(gods)} gods from saved wiki HTML.")

    if args.only:
        gods = _resolve_god_list(gods, args.only)
        if not gods:
            print(f"No gods matched --only '{args.only}'.")
            return
        print(f"[i] Filtered to {len(gods)} via --only")

    if args.check:
        missing = [g for g in gods
                   if not (OUTPUT_DIR / f"{g['slug']}.png").exists()]
        if missing:
            print(f"{len(missing)} of {len(gods)} cards missing:")
            for g in missing:
                print(f"  - {g['name']}")
        else:
            print(f"All {len(gods)} cards present in {OUTPUT_DIR}/")
        return

    print(f"Gods to process: {len(gods)}")
    print(f"Output: {OUTPUT_DIR}/\n")

    stats = {"direct": 0, "og": 0, "exists": 0, "missing": 0}
    for god in gods:
        if args.verbose:
            print(f"  [{god['name']}]")
        source = download_card(god, force=args.force,
                               use_og_scrape=args.use_og_scrape,
                               verbose=args.verbose)
        if source == "exists":
            stats["exists"] += 1
        elif source:
            stats[source] += 1
            label = {"direct": "[Direct]", "og": "[OG]    "}.get(
                source, f"[{source}]")
            print(f"  {label} {god['name']}")
        else:
            stats["missing"] += 1
            print(f"  [--]     {god['name']} (not found)")
        time.sleep(args.throttle)

    total = len(list(OUTPUT_DIR.glob("*.png")))
    print(f"\nDone!")
    if stats["direct"]:
        print(f"  Direct image URL: {stats['direct']}")
    if stats["og"]:
        print(f"  Wiki og:image:    {stats['og']}")
    print(f"  Already had:      {stats['exists']}")
    if stats["missing"]:
        print(f"  Not found:        {stats['missing']}")
    print(f"  Total card files: {total}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[!] Unhandled error in main(): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
