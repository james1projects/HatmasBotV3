"""
Thumbnail Studio - web UI for tools/build_thumbnail.py
=======================================================
Local aiohttp server on 127.0.0.1:8071 (or first free port up to 8089).
Single-page form for building YouTube thumbnails: pick a preset, pick
gods from an icon dropdown, pick items from a category-grouped icon
dropdown, set headline / result / KDA, click Render -> see the result
inline -> click "Save & Open in Paint.NET" to launch layered editing.

Imports tools/build_thumbnail.py as a module - same code path the CLI
uses, no subprocesses.

Architecture
------------
- aiohttp app bound to 127.0.0.1 (never 0.0.0.0).
- GET /                  Single-page UI (HTML + inline CSS/JS, no build step).
- GET /api/options       Presets, gods (with skins), items grouped by category.
- GET /icon/god/<slug>   Serves the Custom God Icons primary if present,
                         else data/god_icons/<slug>.png.
- GET /icon/item/<slug>  Serves Custom Item Icons override if present,
                         else data/item_icons/<slug>.png.
- POST /api/render       Body = JSON form values. Calls build_thumbnail.compose()
                         and writes the flat PNG. Returns {png_url, png_path,
                         stem, warnings}. Layered PNGs / PSD are written
                         only when the user clicks "Save & Open in Paint.NET"
                         (avoids ~50 file writes per preview render).
- POST /api/open_in_paint Body = {png_path, stem}. Looks up the cached
                         layer_outputs for that stem, writes the layered
                         PNGs + optional PSD, launches Paint.NET.
- GET /render/<file>     Serves a freshly rendered PNG from thumbnails/.

Why a custom dropdown instead of <select>
-----------------------------------------
Native <option> elements can't render images. The icon-inline dropdown
is a small custom widget (button trigger + filterable panel) shared by
god slots and item slots; item slots use category section headers.

Usage
-----
    python tools/thumbnail_studio.py            # bind :8071 (or next free)
    python tools/thumbnail_studio.py --no-open  # don't open the browser
    python tools/thumbnail_studio.py --port 9000

Stream Deck pair: thumbnail_studio.bat at the repo root.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import traceback
import types
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

# Resolve repo root and add tools/ to sys.path so we can import build_thumbnail
TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
import build_thumbnail as bt  # noqa: E402

PRESETS_DIR = REPO_ROOT / "thumbnail_presets"
THUMBNAILS_DIR = REPO_ROOT / "thumbnails"
GOD_CARDS_DIR = REPO_ROOT / "data" / "god_cards"
GOD_ICONS_DIR = REPO_ROOT / "data" / "god_icons"
CUSTOM_ICONS_DIR = REPO_ROOT / "Custom God Icons"
CUSTOM_CARDS_DIR = REPO_ROOT / "Custom God Cards"
ITEM_ICONS_DIR = REPO_ROOT / "data" / "item_icons"
CUSTOM_ITEM_ICONS_DIR = REPO_ROOT / "Custom Item Icons"
ITEM_MANIFEST = ITEM_ICONS_DIR / "_manifest.json"

DEFAULT_PORT = 8071
MAX_PORT_PROBE = 8089
BIND_HOST = "127.0.0.1"

# Presets to expose in the dropdown. Filters out experimental "_*" presets.
ALLOWED_PRESETS = ("build_guide", "1v1", "1v2", "2matches", "2gods", "single")

# Gods that have a card on disk but no Custom God Icons primary file.
# Used so the dropdown shows the proper display name (and the icon
# server can still serve the wiki-downloaded data/god_icons/<slug>.png).
GOD_DISPLAY_NAME_OVERRIDES: Dict[str, str] = {
    "hecate": "Hecate",
    "nut": "Nut",
    "princess-bari": "Princess Bari",
}

# Which form fields each preset uses. Drives show/hide in the front-end.
# Mirrors what build_placeholders / the layer set in each preset.json
# actually consume - sending an unused field is harmless but the UI hides
# it to keep the form focused.
PRESET_FIELDS: Dict[str, List[str]] = {
    "build_guide": ["god", "skin", "item1", "item2", "item3",
                    "text", "result", "flip_god"],
    "1v1":        ["god", "skin", "vs",
                   "text", "subtext", "kda", "result",
                   "flip_god", "flip_vs"],
    "1v2":        ["god", "skin", "vs", "vs2",
                   "text", "subtext", "kda", "result", "result2",
                   "flip_god", "flip_vs", "flip_vs2"],
    "2matches":   ["god", "skin", "vs", "god2", "skin2", "vs2",
                   "text", "subtext", "kda", "result", "result2",
                   "flip_god", "flip_vs", "flip_god2", "flip_vs2"],
    "2gods":      ["god", "skin", "god2", "skin2",
                   "text", "subtext",
                   "flip_god", "flip_god2"],
    "single":     ["god", "skin",
                   "text", "subtext", "kda", "result",
                   "flip_god"],
}

# In-memory cache: stem -> (canvas_size, layer_outputs).
# Populated by /api/render, consumed by /api/open_in_paint so we don't
# have to recompose to write the layered PNGs + PSD. Bounded loosely;
# old entries get pushed out as new renders happen.
_LAYER_CACHE: Dict[str, Tuple[Tuple[int, int], List[Tuple[str, Any]]]] = {}
_LAYER_CACHE_MAX = 16


# ============================================================
# OPTIONS / METADATA
# ============================================================

def _slugify_god(name: str) -> str:
    """Match build_thumbnail.slugify exactly."""
    return name.lower().replace("'", "").replace(" ", "-")


def list_presets() -> List[str]:
    """All allowed presets that exist on disk, in our preferred display order."""
    if not PRESETS_DIR.exists():
        return []
    on_disk = {p.stem for p in PRESETS_DIR.glob("*.json")
               if not p.stem.startswith("_")}
    return [p for p in ALLOWED_PRESETS if p in on_disk]


def _custom_icon_primary_stems() -> List[str]:
    """Walk Custom God Icons/ for "primary" stems - files with no '-' in the
    name. These preserve the canonical display name with spaces (e.g.
    "Hou Yi.png"), which is the source of truth we need to reverse the
    lossy slugify."""
    if not CUSTOM_ICONS_DIR.exists():
        return []
    out = []
    for f in CUSTOM_ICONS_DIR.glob("*.png"):
        if "-" in f.stem:
            continue
        out.append(f.stem)
    return out


def _build_slug_to_display() -> Dict[str, str]:
    """slug -> display name from Custom God Icons + manual overrides."""
    table: Dict[str, str] = {}
    for stem in _custom_icon_primary_stems():
        table[_slugify_god(stem)] = stem
    for slug, name in GOD_DISPLAY_NAME_OVERRIDES.items():
        table.setdefault(slug, name)
    return table


def _titlecase_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-"))


def _list_skins_for(god_display: str) -> List[str]:
    """Scan Custom God Cards/ for <God>-<Skin>.<ext> and <God><Skin>.<ext>.
    Returns sorted unique skin names. Mirrors the matching rules in
    build_thumbnail.resolve_god_card."""
    if not CUSTOM_CARDS_DIR.exists() or not god_display:
        return []
    EXTS = {".png", ".webp", ".jpg", ".jpeg"}
    god_variants = {god_display, god_display.title(), god_display.lower(),
                    god_display.replace(" ", "")}
    skins: set = set()
    for f in CUSTOM_CARDS_DIR.iterdir():
        if not f.is_file() or f.suffix.lower() not in EXTS:
            continue
        stem = f.stem
        for gv in god_variants:
            if not gv:
                continue
            # <God>-<Skin>
            if stem.startswith(gv + "-"):
                tail = stem[len(gv) + 1:].strip()
                if tail:
                    skins.add(tail)
                break
            # <God><Skin> (no separator) - only valid when the first
            # character of the tail is uppercase, otherwise we'd false-
            # match e.g. "Apollo" vs "Apollon" if such a god existed.
            if stem != gv and stem.startswith(gv) and len(stem) > len(gv):
                tail = stem[len(gv):]
                if tail and tail[0].isupper():
                    skins.add(tail)
                    break
    return sorted(skins)


def list_gods() -> List[Dict[str, Any]]:
    """One entry per god that has either a card or an icon on disk."""
    slug_to_display = _build_slug_to_display()
    slugs: set = set()
    if GOD_CARDS_DIR.exists():
        for f in GOD_CARDS_DIR.glob("*.png"):
            slugs.add(f.stem)
    if GOD_ICONS_DIR.exists():
        for f in GOD_ICONS_DIR.glob("*.png"):
            slugs.add(f.stem)
    out = []
    for slug in sorted(slugs):
        display = slug_to_display.get(slug) or _titlecase_slug(slug)
        out.append({
            "slug": slug,
            "display_name": display,
            "icon_url": f"/icon/god/{slug}",
            "skins": _list_skins_for(display),
        })
    out.sort(key=lambda g: g["display_name"].lower())
    return out


def load_items() -> Dict[str, List[Dict[str, Any]]]:
    """Items grouped by category. Reads data/item_icons/_manifest.json."""
    if not ITEM_MANIFEST.exists():
        return {}
    try:
        manifest = json.loads(ITEM_MANIFEST.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[studio] WARN: could not parse item manifest: {exc}")
        return {}
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for slug, meta in manifest.items():
        cat = str(meta.get("category") or "Item")
        by_cat.setdefault(cat, []).append({
            "slug": slug,
            "display_name": str(meta.get("display_name") or slug),
            "icon_url": f"/icon/item/{slug}",
            "tier": meta.get("tier"),
            "god": meta.get("god"),
        })
    # Within each category: tier first (T1 < T2 < T3 < ""), then display name
    def _key(e: Dict[str, Any]) -> Tuple[str, str]:
        return (str(e.get("tier") or "~"),
                str(e.get("display_name") or "").lower())
    for cat in by_cat:
        by_cat[cat].sort(key=_key)
    return by_cat


# Module-level caches so /icon/* lookups don't re-scan on every hit.
_GOD_INDEX_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_ITEM_INDEX_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def _god_index() -> Dict[str, Dict[str, Any]]:
    global _GOD_INDEX_CACHE
    if _GOD_INDEX_CACHE is None:
        _GOD_INDEX_CACHE = {g["slug"]: g for g in list_gods()}
    return _GOD_INDEX_CACHE


def _item_index() -> Dict[str, Dict[str, Any]]:
    global _ITEM_INDEX_CACHE
    if _ITEM_INDEX_CACHE is None:
        flat: Dict[str, Dict[str, Any]] = {}
        for cat, items in load_items().items():
            for it in items:
                flat[it["slug"]] = it
        _ITEM_INDEX_CACHE = flat
    return _ITEM_INDEX_CACHE


# ============================================================
# ROUTE HANDLERS
# ============================================================

def _file_response(path: Path, cache_seconds: int = 86400) -> web.Response:
    return web.FileResponse(
        path,
        headers={"Cache-Control": f"public, max-age={cache_seconds}"},
    )


def _no_store(path: Path) -> web.Response:
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def api_options(request: web.Request) -> web.Response:
    return web.json_response({
        "presets": list_presets(),
        "preset_fields": PRESET_FIELDS,
        "gods": list_gods(),
        "items_by_category": load_items(),
    })


async def serve_god_icon(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    # 1. Custom God Icons primary by display name
    god = _god_index().get(slug)
    if god and CUSTOM_ICONS_DIR.exists():
        for variant in (god["display_name"], god["display_name"].title()):
            primary = CUSTOM_ICONS_DIR / f"{variant}.png"
            if primary.exists():
                return _file_response(primary)
    # 2. data/god_icons/<slug>.png
    p = GOD_ICONS_DIR / f"{slug}.png"
    if p.exists():
        return _file_response(p)
    return web.Response(status=404, text="Not found")


async def serve_item_icon(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    meta = _item_index().get(slug)
    # 1. Custom Item Icons override by display name
    if meta and CUSTOM_ITEM_ICONS_DIR.exists():
        display = meta.get("display_name", "")
        for variant in (display, display.title(), display.lower()):
            if not variant:
                continue
            p = CUSTOM_ITEM_ICONS_DIR / f"{variant}.png"
            if p.exists():
                return _file_response(p)
    # 2. data/item_icons/<slug>.png
    p = ITEM_ICONS_DIR / f"{slug}.png"
    if p.exists():
        return _file_response(p)
    return web.Response(status=404, text="Not found")


async def serve_render(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    # Path-traversal guard: only allow plain filenames.
    if "/" in filename or "\\" in filename or ".." in filename:
        return web.Response(status=400, text="Bad filename")
    p = THUMBNAILS_DIR / filename
    if not p.exists():
        return web.Response(status=404, text="Not rendered")
    return _no_store(p)


def _normalize_optional(value: Any) -> Optional[str]:
    """Empty string -> None so build_placeholders falls back to its default
    (which for text is "my god's name", for subtext is "vs god's name").
    The dropdowns post the bare display name; missing field -> empty string."""
    if value is None:
        return None
    s = str(value)
    return s if s.strip() != "" else None


async def api_render(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"bad JSON: {exc}"},
                                 status=400)

    preset_name = (data.get("preset") or "").strip()
    if preset_name not in ALLOWED_PRESETS:
        return web.json_response(
            {"ok": False, "error": f"unknown preset {preset_name!r}"},
            status=400,
        )
    try:
        preset = bt.load_preset(preset_name)
    except SystemExit as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)

    god = (data.get("god") or "").strip()
    if not god:
        return web.json_response(
            {"ok": False, "error": "god is required"}, status=400,
        )

    # Build the Namespace build_placeholders expects. Treat empty strings
    # for text/subtext as "None" so the CLI default (god name) fills in.
    ns = types.SimpleNamespace(
        god=god,
        vs=(data.get("vs") or "").strip(),
        vs2=(data.get("vs2") or "").strip(),
        god2=(data.get("god2") or "").strip(),
        skin=(data.get("skin") or "").strip(),
        skin2=(data.get("skin2") or "").strip(),
        item1=(data.get("item1") or "").strip(),
        item2=(data.get("item2") or "").strip(),
        item3=(data.get("item3") or "").strip(),
        text=_normalize_optional(data.get("text")),
        subtext=_normalize_optional(data.get("subtext")),
        kda=(data.get("kda") or "").strip(),
        result=(data.get("result") or "").strip(),
        result2=(data.get("result2") or "").strip(),
        flip_god=bool(data.get("flip_god")),
        flip_vs=bool(data.get("flip_vs")),
        flip_god2=bool(data.get("flip_god2")),
        flip_vs2=bool(data.get("flip_vs2")),
        no_text=bool(data.get("no_text")),
        no_subtext=bool(data.get("no_subtext")),
        preset=preset_name,
    )

    bt.apply_flip_overrides(preset, ns)
    placeholders = bt.build_placeholders(ns)

    # Surface the same "no card found" warnings the CLI prints.
    warnings: List[str] = []
    if not placeholders["my_god_card"]:
        warnings.append(f"No card found for '{god}'.")
    if ns.vs and not placeholders["vs_god_card"]:
        warnings.append(f"No card found for '{ns.vs}'.")
    if ns.vs2 and not placeholders["vs2_god_card"]:
        warnings.append(f"No card found for '{ns.vs2}'.")
    if ns.god2 and not placeholders["my_god2_card"]:
        warnings.append(f"No card found for '{ns.god2}'.")
    # Item icon warnings (build_guide-only fields)
    for label, name, key in (
        ("item1", ns.item1, "item1_icon"),
        ("item2", ns.item2, "item2_icon"),
        ("item3", ns.item3, "item3_icon"),
    ):
        if name and not placeholders[key]:
            warnings.append(f"No icon found for {label}: '{name}'.")

    try:
        composite, layer_outputs = bt.compose(preset, placeholders)
    except Exception as exc:
        traceback.print_exc()
        return web.json_response(
            {"ok": False, "error": f"render failed: {exc}"}, status=500,
        )

    THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
    stem = bt._default_stem(ns)
    out_png = THUMBNAILS_DIR / f"{stem}.png"
    composite.convert("RGB").save(out_png, "PNG", optimize=True)

    canvas_size = tuple(preset.get("size", [1280, 720]))
    _LAYER_CACHE[stem] = (canvas_size, layer_outputs)
    # Trim cache
    while len(_LAYER_CACHE) > _LAYER_CACHE_MAX:
        oldest = next(iter(_LAYER_CACHE))
        _LAYER_CACHE.pop(oldest, None)

    return web.json_response({
        "ok": True,
        "png_path": str(out_png),
        "png_url": f"/render/{out_png.name}",
        "stem": stem,
        "warnings": warnings,
    })


async def api_open_in_paint(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"bad JSON: {exc}"},
                                 status=400)
    png_path = data.get("png_path")
    stem = data.get("stem")
    if not png_path or not stem:
        return web.json_response(
            {"ok": False, "error": "png_path and stem required"},
            status=400,
        )
    cached = _LAYER_CACHE.get(stem)
    if not cached:
        return web.json_response(
            {"ok": False,
             "error": "no cached layers for that render (re-render first)"},
            status=400,
        )
    canvas_size, layer_outputs = cached
    out_png = Path(png_path)
    layers_dir = out_png.with_name(out_png.stem + "_layers")
    try:
        bt.write_layered_pngs(layers_dir, layer_outputs)
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": f"failed to write layered PNGs: {exc}"},
            status=500,
        )
    psd_path = out_png.with_suffix(".psd")
    psd_written = bt.try_write_psd(psd_path, tuple(canvas_size), layer_outputs)
    target = psd_path if psd_written and psd_path.exists() else out_png
    opened = bt.open_in_paint_net(target)
    return web.json_response({
        "ok": bool(opened),
        "opened_path": str(target),
        "psd_written": bool(psd_written),
        "layers_dir": str(layers_dir),
    })


# ============================================================
# HTML / CSS / JS (single page, no build step)
# ============================================================

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Thumbnail Studio</title>
<style>
  :root {
    --bg: #0a0e14;
    --panel: #141923;
    --panel-2: #1a1f29;
    --border: #2a3140;
    --border-strong: #3a4150;
    --text: #e6e6e6;
    --muted: #8d96a8;
    --accent: #ffd24d;
    --accent-soft: #ffe28a;
    --warn: #f5a623;
    --error: #ff6b6b;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
  }
  .layout {
    display: grid;
    grid-template-columns: 420px 1fr;
    gap: 16px;
    padding: 16px;
    height: 100vh;
  }
  .col-form {
    overflow-y: auto;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px;
  }
  .col-preview {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  h1 {
    font-size: 16px;
    margin: 0 0 12px;
    color: var(--accent);
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  label {
    display: block;
    font-size: 11px;
    color: var(--muted);
    margin: 12px 0 4px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 600;
  }
  label .inline-clear {
    float: right;
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 1px 6px;
    font-size: 10px;
    cursor: pointer;
    text-transform: none;
    letter-spacing: 0;
    font-weight: normal;
  }
  label .inline-clear:hover { color: var(--text); border-color: var(--border-strong); }
  input[type=text], select {
    width: 100%;
    padding: 8px 10px;
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    font-size: 14px;
    font-family: inherit;
  }
  input[type=text]:focus, select:focus {
    outline: none;
    border-color: var(--accent);
  }
  button {
    padding: 8px 16px;
    background: var(--accent);
    color: var(--bg);
    border: 0;
    border-radius: 4px;
    font-weight: 600;
    cursor: pointer;
    font-family: inherit;
    font-size: 14px;
  }
  button:hover { background: var(--accent-soft); }
  button.secondary {
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border-strong);
  }
  button.secondary:hover { background: var(--border); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  .hidden { display: none !important; }

  /* Custom icon-grid dropdown */
  .icondrop { position: relative; }
  .icondrop .trigger {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 6px 10px;
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
    min-height: 44px;
    text-align: left;
    font-weight: normal;
  }
  .icondrop .trigger:hover { border-color: var(--border-strong); }
  .icondrop .trigger img {
    width: 32px; height: 32px;
    border-radius: 3px;
    background: var(--bg);
    object-fit: contain;
  }
  .icondrop .trigger .label { flex: 1; }
  .icondrop .trigger .caret { color: var(--muted); }
  .icondrop .panel {
    position: absolute;
    z-index: 50;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    max-height: 360px;
    overflow-y: auto;
    background: var(--panel-2);
    border: 1px solid var(--border-strong);
    border-radius: 4px;
    padding: 4px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.45);
  }
  .icondrop .panel.hidden { display: none; }
  .icondrop .panel .search-row {
    position: sticky;
    top: 0;
    background: var(--panel-2);
    padding: 4px 4px 6px;
    z-index: 1;
  }
  .icondrop .panel input.search {
    width: 100%;
    padding: 6px 8px;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 3px;
    font-size: 13px;
    font-family: inherit;
  }
  .icondrop .panel .cat-header {
    font-size: 10px;
    color: var(--muted);
    text-transform: uppercase;
    padding: 8px 8px 2px;
    letter-spacing: 0.08em;
    font-weight: 600;
  }
  .icondrop .panel button.opt {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    background: transparent;
    color: var(--text);
    border: 0;
    padding: 4px 6px;
    border-radius: 3px;
    cursor: pointer;
    text-align: left;
    font-weight: normal;
  }
  .icondrop .panel button.opt:hover,
  .icondrop .panel button.opt.active { background: var(--border); }
  .icondrop .panel button.opt img {
    width: 32px; height: 32px;
    border-radius: 3px;
    background: var(--bg);
    object-fit: contain;
  }
  .icondrop .panel button.opt .tier {
    color: var(--muted);
    font-size: 12px;
    margin-left: auto;
  }
  .icondrop .panel .empty {
    padding: 12px;
    text-align: center;
    color: var(--muted);
    font-size: 13px;
  }

  /* Radios */
  .radios { display: flex; gap: 14px; flex-wrap: wrap; }
  .radios label {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin: 0;
    font-size: 13px;
    color: var(--text);
    text-transform: none;
    letter-spacing: 0;
    font-weight: normal;
  }
  .radios input { accent-color: var(--accent); }

  /* Preview */
  .preview-wrap {
    background: #000;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px;
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }
  .preview-wrap img {
    max-width: 100%;
    max-height: 100%;
    display: block;
    border-radius: 3px;
  }
  .preview-wrap .placeholder {
    color: var(--muted);
    font-style: italic;
    font-size: 14px;
  }
  .status { font-size: 13px; color: var(--muted); min-height: 18px; }
  .status.err { color: var(--error); }
  .warnings {
    color: var(--warn);
    font-size: 12px;
    min-height: 16px;
    line-height: 1.4;
  }
  .actions { display: flex; gap: 8px; padding-top: 12px; }

  /* Small group layout */
  .field-group {
    padding: 8px 0;
    border-top: 1px solid var(--border);
    margin-top: 8px;
  }
  .field-group:first-of-type { border-top: 0; margin-top: 0; padding-top: 0; }
</style>
</head>
<body>
<div class="layout">
  <div class="col-form">
    <h1>Thumbnail Studio</h1>

    <div class="field-group">
      <label>Preset</label>
      <select id="preset"></select>
    </div>

    <div class="field-group">
      <div data-field="god">
        <label>My God <button class="inline-clear" data-clear="god" type="button">clear</button></label>
        <div class="icondrop" data-name="god" data-kind="god"></div>
      </div>
      <div data-field="skin">
        <label>Skin (optional)</label>
        <input type="text" id="skin" placeholder="e.g. Forest Lord">
      </div>
      <div data-field="flip_god" style="margin-top:8px">
        <label class="radios"><input type="checkbox" id="flip_god"> Flip my god</label>
      </div>
    </div>

    <div class="field-group" data-group="vs">
      <div data-field="vs">
        <label>Versus God <button class="inline-clear" data-clear="vs" type="button">clear</button></label>
        <div class="icondrop" data-name="vs" data-kind="god"></div>
      </div>
      <div data-field="flip_vs" style="margin-top:8px">
        <label class="radios"><input type="checkbox" id="flip_vs"> Flip vs god</label>
      </div>
    </div>

    <div class="field-group" data-group="vs2">
      <div data-field="vs2">
        <label>Versus God 2 <button class="inline-clear" data-clear="vs2" type="button">clear</button></label>
        <div class="icondrop" data-name="vs2" data-kind="god"></div>
      </div>
      <div data-field="flip_vs2" style="margin-top:8px">
        <label class="radios"><input type="checkbox" id="flip_vs2"> Flip vs god 2</label>
      </div>
    </div>

    <div class="field-group" data-group="god2">
      <div data-field="god2">
        <label>My God 2 <button class="inline-clear" data-clear="god2" type="button">clear</button></label>
        <div class="icondrop" data-name="god2" data-kind="god"></div>
      </div>
      <div data-field="skin2">
        <label>Skin 2 (optional)</label>
        <input type="text" id="skin2" placeholder="e.g. Heart of Gold">
      </div>
      <div data-field="flip_god2" style="margin-top:8px">
        <label class="radios"><input type="checkbox" id="flip_god2"> Flip my god 2</label>
      </div>
    </div>

    <div class="field-group" data-group="items">
      <div data-field="item1">
        <label>Item 1 <button class="inline-clear" data-clear="item1" type="button">clear</button></label>
        <div class="icondrop" data-name="item1" data-kind="item"></div>
      </div>
      <div data-field="item2">
        <label>Item 2 <button class="inline-clear" data-clear="item2" type="button">clear</button></label>
        <div class="icondrop" data-name="item2" data-kind="item"></div>
      </div>
      <div data-field="item3">
        <label>Item 3 <button class="inline-clear" data-clear="item3" type="button">clear</button></label>
        <div class="icondrop" data-name="item3" data-kind="item"></div>
      </div>
    </div>

    <div class="field-group">
      <div data-field="text">
        <label>Headline</label>
        <input type="text" id="text" placeholder="(defaults to my god's name)">
      </div>
      <div data-field="subtext">
        <label>Subtext</label>
        <input type="text" id="subtext" placeholder="(defaults to vs god's name)">
      </div>
      <div data-field="kda">
        <label>KDA</label>
        <input type="text" id="kda" placeholder="e.g. 12/3/8">
      </div>
      <div data-field="result">
        <label>Result</label>
        <div class="radios">
          <label><input type="radio" name="result" value="" checked> none</label>
          <label><input type="radio" name="result" value="win"> Win</label>
          <label><input type="radio" name="result" value="loss"> Loss</label>
        </div>
      </div>
      <div data-field="result2">
        <label>Result 2</label>
        <div class="radios">
          <label><input type="radio" name="result2" value="" checked> none</label>
          <label><input type="radio" name="result2" value="win"> Win</label>
          <label><input type="radio" name="result2" value="loss"> Loss</label>
        </div>
      </div>
    </div>

    <div class="actions">
      <button id="render-btn">Render</button>
      <button id="open-btn" class="secondary" disabled>Save &amp; Open in Paint.NET</button>
    </div>
  </div>

  <div class="col-preview">
    <div class="status" id="status">Loading options...</div>
    <div class="warnings" id="warnings"></div>
    <div class="preview-wrap">
      <span class="placeholder" id="preview-placeholder">Click Render to build a thumbnail.</span>
      <img id="preview" alt="" style="display:none">
    </div>
  </div>
</div>

<script>
(async function () {
  const $  = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c]));

  // ---- Load options ------------------------------------------------------
  let opts;
  try {
    const r = await fetch("/api/options");
    if (!r.ok) throw new Error("HTTP " + r.status);
    opts = await r.json();
  } catch (e) {
    $("#status").textContent = "Failed to load options: " + e.message;
    $("#status").classList.add("err");
    return;
  }
  const PRESET_FIELDS = opts.preset_fields;

  // ---- Preset dropdown ---------------------------------------------------
  const presetSel = $("#preset");
  for (const p of opts.presets) {
    const o = document.createElement("option");
    o.value = p; o.textContent = p;
    presetSel.appendChild(o);
  }
  presetSel.value = opts.presets.includes("build_guide") ? "build_guide" : opts.presets[0];

  // ---- State -------------------------------------------------------------
  const state = {
    god: "", vs: "", vs2: "", god2: "",
    item1: "", item2: "", item3: "",
    skin: "", skin2: "",
    text: "", subtext: "", kda: "",
    result: "", result2: "",
    flip_god: false, flip_vs: false, flip_god2: false, flip_vs2: false,
    preset: presetSel.value,
  };
  let lastRender = null; // { png_path, stem }

  // ---- Icon dropdown widget ---------------------------------------------
  // options: [{slug, display_name, icon_url, _cat?, _tier?}]
  // onSelect(slug, display_name, icon_url) -> void
  function buildIconDrop(host, options, onSelect) {
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "trigger";
    trigger.innerHTML = '<img alt="" style="display:none"><span class="label">- pick -</span><span class="caret">v</span>';
    host.appendChild(trigger);

    const panel = document.createElement("div");
    panel.className = "panel hidden";
    const searchRow = document.createElement("div");
    searchRow.className = "search-row";
    const search = document.createElement("input");
    search.type = "text";
    search.className = "search";
    search.placeholder = "Type to filter...";
    searchRow.appendChild(search);
    panel.appendChild(searchRow);
    const listHost = document.createElement("div");
    panel.appendChild(listHost);
    host.appendChild(panel);

    let activeIdx = -1;
    let visibleBtns = [];

    function renderList(filterText) {
      const filt = (filterText || "").toLowerCase().trim();
      listHost.innerHTML = "";
      let lastCat = null;
      const btns = [];
      let anyShown = false;
      for (const o of options) {
        const match = !filt || o.display_name.toLowerCase().includes(filt);
        if (!match) continue;
        if (o._cat && o._cat !== lastCat) {
          const h = document.createElement("div");
          h.className = "cat-header";
          h.textContent = o._cat;
          listHost.appendChild(h);
          lastCat = o._cat;
        } else if (!o._cat) {
          lastCat = null;
        }
        const b = document.createElement("button");
        b.type = "button";
        b.className = "opt";
        const tierMarkup = o._tier ? '<span class="tier">' + esc(o._tier) + "</span>" : "";
        b.innerHTML = '<img src="' + esc(o.icon_url) + '" alt=""><span>' + esc(o.display_name) + "</span>" + tierMarkup;
        b.addEventListener("click", () => {
          onSelect(o.slug, o.display_name, o.icon_url);
          panel.classList.add("hidden");
        });
        listHost.appendChild(b);
        btns.push(b);
        anyShown = true;
      }
      if (!anyShown) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No matches.";
        listHost.appendChild(empty);
      }
      visibleBtns = btns;
      activeIdx = -1;
    }

    function refreshActive() {
      visibleBtns.forEach((el, i) => el.classList.toggle("active", i === activeIdx));
      if (activeIdx >= 0 && visibleBtns[activeIdx]) {
        visibleBtns[activeIdx].scrollIntoView({ block: "nearest" });
      }
    }

    search.addEventListener("input", () => renderList(search.value));
    search.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        activeIdx = Math.min(visibleBtns.length - 1, activeIdx + 1);
        refreshActive();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        activeIdx = Math.max(0, activeIdx - 1);
        refreshActive();
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (activeIdx >= 0 && visibleBtns[activeIdx]) visibleBtns[activeIdx].click();
      } else if (e.key === "Escape") {
        panel.classList.add("hidden");
      }
    });

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasHidden = panel.classList.contains("hidden");
      $$(".icondrop .panel").forEach(p => p.classList.add("hidden"));
      if (wasHidden) {
        panel.classList.remove("hidden");
        search.value = "";
        renderList("");
        setTimeout(() => { search.focus(); search.select(); }, 0);
      }
    });
    document.addEventListener("click", (e) => {
      if (!host.contains(e.target)) panel.classList.add("hidden");
    });

    return {
      setSelected(slug, display_name, icon_url) {
        const img = trigger.querySelector("img");
        const label = trigger.querySelector(".label");
        if (slug) {
          img.src = icon_url;
          img.style.display = "";
          label.textContent = display_name;
        } else {
          img.removeAttribute("src");
          img.style.display = "none";
          label.textContent = "- pick -";
        }
      },
    };
  }

  // ---- Build the dropdown option lists -----------------------------------
  const godOptions = opts.gods.map(g => ({
    slug: g.slug,
    display_name: g.display_name,
    icon_url: g.icon_url,
  }));

  // Category display order for items
  const CAT_ORDER = ["Starter", "Item", "Consumable", "Curio", "Relic", "GodSpecific"];
  const itemOptions = [];
  const itemDisplayBySlug = {};
  const seenCats = new Set();
  for (const cat of CAT_ORDER) {
    const list = opts.items_by_category[cat] || [];
    if (!list.length) continue;
    seenCats.add(cat);
    for (const it of list) {
      itemOptions.push({
        slug: it.slug,
        display_name: it.display_name,
        icon_url: it.icon_url,
        _cat: cat,
        _tier: it.tier || "",
      });
      itemDisplayBySlug[it.slug] = it.display_name;
    }
  }
  // Catch any unexpected categories the manifest might add later
  for (const cat of Object.keys(opts.items_by_category)) {
    if (seenCats.has(cat)) continue;
    for (const it of opts.items_by_category[cat]) {
      itemOptions.push({
        slug: it.slug,
        display_name: it.display_name,
        icon_url: it.icon_url,
        _cat: cat,
        _tier: it.tier || "",
      });
      itemDisplayBySlug[it.slug] = it.display_name;
    }
  }

  // ---- Instantiate dropdowns --------------------------------------------
  const drops = {};
  for (const name of ["god", "vs", "vs2", "god2"]) {
    const host = $(`.icondrop[data-name="${name}"]`);
    drops[name] = buildIconDrop(host, godOptions, (slug, display, url) => {
      state[name] = display;
      drops[name].setSelected(slug, display, url);
    });
  }
  for (const name of ["item1", "item2", "item3"]) {
    const host = $(`.icondrop[data-name="${name}"]`);
    drops[name] = buildIconDrop(host, itemOptions, (slug, display, url) => {
      state[name] = itemDisplayBySlug[slug] || display;
      drops[name].setSelected(slug, display, url);
    });
  }

  // ---- Clear buttons -----------------------------------------------------
  $$("button[data-clear]").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const f = btn.dataset.clear;
      state[f] = "";
      if (drops[f]) drops[f].setSelected("", "", "");
    });
  });

  // ---- Text inputs -------------------------------------------------------
  for (const f of ["skin", "skin2", "text", "subtext", "kda"]) {
    $("#" + f).addEventListener("input", () => { state[f] = $("#" + f).value; });
  }
  // ---- Flip toggles ------------------------------------------------------
  for (const f of ["flip_god", "flip_vs", "flip_god2", "flip_vs2"]) {
    $("#" + f).addEventListener("change", () => { state[f] = $("#" + f).checked; });
  }
  // ---- Result radios -----------------------------------------------------
  $$('input[name="result"]').forEach(r => r.addEventListener("change", () => {
    if (r.checked) state.result = r.value;
  }));
  $$('input[name="result2"]').forEach(r => r.addEventListener("change", () => {
    if (r.checked) state.result2 = r.value;
  }));

  // ---- Preset-driven field visibility -----------------------------------
  function applyPresetVisibility() {
    state.preset = presetSel.value;
    const enabled = PRESET_FIELDS[state.preset] || [];
    $$("[data-field]").forEach(div => {
      const f = div.dataset.field;
      div.classList.toggle("hidden", !enabled.includes(f));
    });
    // Group containers - hide whole group if all children hidden
    $$(".field-group[data-group]").forEach(g => {
      const anyVisible = $$("[data-field]", g).some(d => !d.classList.contains("hidden"));
      g.classList.toggle("hidden", !anyVisible);
    });
  }
  presetSel.addEventListener("change", applyPresetVisibility);
  applyPresetVisibility();

  // ---- Render ------------------------------------------------------------
  $("#render-btn").addEventListener("click", async () => {
    $("#render-btn").disabled = true;
    $("#open-btn").disabled = true;
    $("#status").textContent = "Rendering...";
    $("#status").classList.remove("err");
    $("#warnings").textContent = "";
    try {
      const r = await fetch("/api/render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state),
      });
      const j = await r.json();
      if (!j.ok) {
        $("#status").textContent = "Error: " + (j.error || "unknown");
        $("#status").classList.add("err");
        return;
      }
      const img = $("#preview");
      const ph = $("#preview-placeholder");
      img.src = j.png_url + "?t=" + Date.now();
      img.style.display = "";
      if (ph) ph.style.display = "none";
      $("#status").textContent = "Rendered: " + j.png_path;
      if (j.warnings && j.warnings.length) {
        $("#warnings").textContent = j.warnings.join("  |  ");
      }
      lastRender = { png_path: j.png_path, stem: j.stem };
      $("#open-btn").disabled = false;
    } catch (e) {
      $("#status").textContent = "Render failed: " + e.message;
      $("#status").classList.add("err");
    } finally {
      $("#render-btn").disabled = false;
    }
  });

  // ---- Save & Open in Paint.NET ------------------------------------------
  $("#open-btn").addEventListener("click", async () => {
    if (!lastRender) return;
    $("#open-btn").disabled = true;
    $("#status").textContent = "Writing layered PNGs and launching Paint.NET...";
    $("#status").classList.remove("err");
    try {
      const r = await fetch("/api/open_in_paint", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRender),
      });
      const j = await r.json();
      if (j.ok) {
        $("#status").textContent = "Opened in Paint.NET: " + j.opened_path
          + (j.psd_written ? " (PSD)" : " (flat PNG, install psd-tools for layered PSD)");
      } else {
        $("#status").textContent = "Open failed: " + (j.error || "unknown");
        $("#status").classList.add("err");
      }
    } catch (e) {
      $("#status").textContent = "Open failed: " + e.message;
      $("#status").classList.add("err");
    } finally {
      $("#open-btn").disabled = false;
    }
  });

  $("#status").textContent = "Ready. " + opts.gods.length + " gods, "
    + Object.values(opts.items_by_category).reduce((n, l) => n + l.length, 0)
    + " items.";
})();
</script>
</body>
</html>
"""


# ============================================================
# SERVER BOOTSTRAP
# ============================================================

def _find_free_port(start: int, end: int) -> int:
    """Try ports in [start, end]. Returns the first one we can bind on
    127.0.0.1. Raises SystemExit if none are free."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((BIND_HOST, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port in range {start}-{end} on {BIND_HOST}")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/options", api_options)
    app.router.add_get("/icon/god/{slug}", serve_god_icon)
    app.router.add_get("/icon/item/{slug}", serve_item_icon)
    app.router.add_get("/render/{filename}", serve_render)
    app.router.add_post("/api/render", api_render)
    app.router.add_post("/api/open_in_paint", api_open_in_paint)
    return app


def _open_browser_soon(url: str) -> None:
    """Open the browser after a short delay so the server has a chance
    to start listening first. Runs in a daemon thread so it can't hold
    the process open after Ctrl+C."""
    def _go():
        time.sleep(0.4)
        try:
            webbrowser.open(url)
        except Exception as exc:
            print(f"[studio] WARN: couldn't open browser: {exc}")
    threading.Thread(target=_go, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"Preferred port (default {DEFAULT_PORT}). Auto-falls "
                         f"through to the next free port up to {MAX_PORT_PROBE}.")
    ap.add_argument("--no-open", action="store_true",
                    help="Don't auto-open the browser.")
    args = ap.parse_args()

    # Pre-warm caches so the first request is snappy.
    presets = list_presets()
    gods = list_gods()
    items = load_items()
    item_count = sum(len(v) for v in items.values())
    print(f"[studio] presets: {len(presets)} ({', '.join(presets)})")
    print(f"[studio] gods:    {len(gods)}")
    print(f"[studio] items:   {item_count} across {len(items)} categories")

    if not presets:
        print("[studio] WARN: no presets found in thumbnail_presets/. "
              "Render endpoint will reject every request.")
    if not gods:
        print("[studio] WARN: no gods found. Make sure data/god_cards/ or "
              "data/god_icons/ has PNGs.")
    if item_count == 0:
        print("[studio] WARN: no items found. Run "
              "`python tools/download_item_icons.py` to populate the "
              "manifest. Item dropdowns will be empty.")

    port = _find_free_port(args.port, max(args.port, MAX_PORT_PROBE))
    url = f"http://{BIND_HOST}:{port}/"
    print(f"[studio] serving at {url}  (Ctrl+C to stop)")

    if not args.no_open:
        _open_browser_soon(url)

    try:
        web.run_app(build_app(), host=BIND_HOST, port=port,
                    print=None, access_log=None)
    except KeyboardInterrupt:
        print("\n[studio] shutdown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
