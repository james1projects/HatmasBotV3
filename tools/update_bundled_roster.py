"""
tools/update_bundled_roster.py — regenerate core/god_roster_data.py.
=====================================================================
The bundled snapshot is the roster's last-resort fallback (fresh
checkout, no cache, wiki down). It doesn't need to be current for the
bot to learn about new gods — refresh() handles that at runtime — but
re-baking it occasionally keeps the no-network path honest.

Fetches the live SMITE 2 roster from wiki.smite2.com and the frozen
SMITE 1 catalog from smite.fandom.com, then rewrites
core/god_roster_data.py in place. first_seen dates for gods already
in the current roster (cache or bundled) are preserved.

Usage:
    python tools/update_bundled_roster.py
"""

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import god_roster

OUT = REPO_ROOT / "core" / "god_roster_data.py"
S1_LIST_API = (
    "https://smite.fandom.com/api.php"
    "?action=parse&page=List_of_gods&prop=text&format=json&formatversion=2"
)
# Icon cell (data-image-key) followed by the god-name link cell.
S1_ROW_RE = re.compile(
    r'data-image-key="(T_[^"]+_Default_Icon\.png)"[^>]*/>.*?'
    r'<td><a href="/wiki/[^"]+" title="([^"]+)">',
    re.DOTALL,
)


def fetch_s1_catalog():
    """Scrape the frozen 130-god SMITE 1 list. Fandom allows urllib."""
    req = urllib.request.Request(S1_LIST_API, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
    })
    body = urllib.request.urlopen(req, timeout=20).read()
    html_text = json.loads(body)["parse"]["text"]

    import html as html_mod
    seen, catalog = set(), []
    for icon, name in S1_ROW_RE.findall(html_text):
        name = html_mod.unescape(name)
        if name in seen:
            continue
        seen.add(name)
        catalog.append({
            "name": name,
            "slug": god_roster.name_to_slug(name),
            "s1_icon": icon,
        })
    return sorted(catalog, key=lambda g: g["slug"])


def main():
    print("Fetching live SMITE 2 roster...")
    live = god_roster.fetch_live_gods()
    if not live:
        print("[!] Live fetch failed — not touching the bundled snapshot.")
        sys.exit(1)

    # Preserve first_seen from whatever roster we currently know.
    current = {g["slug"]: g.get("first_seen") for g in god_roster.gods()}
    today = date.today().isoformat()
    s2 = []
    for g in sorted(live, key=lambda g: g["slug"]):
        s2.append((g["name"], g["slug"], g["wiki_filename"],
                   current.get(g["slug"], today)))
    print(f"  {len(s2)} SMITE 2 gods")

    print("Fetching SMITE 1 catalog...")
    s1 = fetch_s1_catalog()
    if len(s1) < 120:
        print(f"[!] Only {len(s1)} SMITE 1 gods parsed (expected 130) — "
              f"aborting rather than shrinking the catalog.")
        sys.exit(1)
    print(f"  {len(s1)} SMITE 1 gods")

    lines = [
        '"""',
        "core/god_roster_data.py — bundled god-roster snapshot (GENERATED).",
        "",
        "Fallback data for core/god_roster.py when data/god_roster.json does",
        "not exist yet and the live wiki fetch fails. Regenerate with:",
        "",
        "    python tools/update_bundled_roster.py",
        "",
        f"Snapshot taken {today} from wiki.smite2.com (SMITE 2, {len(s2)} gods)",
        f"and smite.fandom.com (SMITE 1 catalog, {len(s1)} gods).",
        '"""',
        "",
        f'BUNDLED_SNAPSHOT_DATE = "{today}"',
        "",
        "# SMITE 2 released roster: [name, slug, wiki icon filename, first_seen]",
        "# first_seen is None for gods that predate roster tracking.",
        "BUNDLED_S2_GODS = [",
    ]
    for name, slug, wiki_filename, fs in s2:
        fs_repr = f'"{fs}"' if fs else "None"
        lines.append(f'    ("{name}", "{slug}", "{wiki_filename}", {fs_repr}),')
    lines += [
        "]",
        "",
        "# Full SMITE 1 catalog: [name, slug, fandom-wiki icon filename].",
        "# SMITE 1 is frozen (no new gods), so this list is static. Used to",
        "# recognize not-yet-ported god names in chat and to fetch fallback",
        "# display art for gods that join SMITE 2 later.",
        "SMITE1_CATALOG = [",
    ]
    for g in s1:
        lines.append(f'    ("{g["name"]}", "{g["slug"]}", "{g["s1_icon"]}"),')
    lines += ["]", ""]

    OUT.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
