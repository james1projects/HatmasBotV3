"""
Build YouTube Thumbnail (preset-driven Pillow compositor)
==========================================================
Generates a 1280x720 (or any size) YouTube thumbnail from a JSON preset
plus a small set of CLI inputs, then opens the result in Paint.NET so
you can do final touch-ups before uploading.

Layered editing in Paint.NET
----------------------------
Pillow can't write .pdn (Paint.NET's proprietary format) and there is
no reliable open-source .pdn writer. We work around this in two ways:

1. Every render also writes a sidecar folder
       thumbnails/<stem>_layers/
   containing one PNG per layer, named in render order
   (00_background.png, 01_left_god_card.png, ...). To get a layered
   editing session in Paint.NET, multi-select all the PNGs in that
   folder and drag them into a Paint.NET window — they import as
   separate layers in a single operation. (Or use "Layers > Import
   from File" with multi-select.)

2. If the optional `psd-tools` package is installed (`pip install
   psd-tools`), the renderer also emits thumbnails/<stem>.psd. The
   PSD plugin for Paint.NET (https://github.com/0xC0000054/psd-plugin)
   opens it as a fully layered PSD with each layer named the same way
   it appears in the preset. When the .psd is present we open it
   instead of the flat .png.

Preset schema (thumbnail_presets/*.json)
----------------------------------------
- schema_version: 1
- name, description
- size: [width, height]
- layers: array of layer objects, rendered bottom-to-top.

Layer types
-----------
solid     : pos, size (default full canvas), color, opacity
gradient  : pos, size (default full canvas), direction (horizontal|vertical|diagonal),
            stops: [{at, color, opacity?}, ...]
image     : src (path or {placeholder}), pos, size, fit (cover|contain|stretch),
            anchor, flip_h, opacity, fallback_text
icon      : god ({my_god}|{vs_god}|literal name), pos, size (square px),
            border {color, width}, shadow {color, offset, blur, opacity}
text      : value (string with placeholders), pos, anchor, font, size, fill,
            stroke {color, width}, shadow {color, offset, blur, opacity},
            max_width, skip_if_empty

Placeholders resolved at render time
------------------------------------
{my_god}         Display name passed via --god
{my_god2}        Display name passed via --god2 (or empty) — used by 2matches / 2gods presets
{vs_god}         Display name passed via --vs (or empty)
{vs2_god}        Display name passed via --vs2 (or empty) — used by 1v2 / 2matches presets
{my_god_card}    Resolved card path (Custom God Cards override > auto-downloaded base)
{my_god2_card}   Same, for --god2 — used by 2matches / 2gods
{vs_god_card}    Resolved card path (auto-downloaded base art)
{vs2_god_card}   Same — used by 1v2 / 2matches
{my_god_icon}    Resolved file path of best available icon (custom > data/god_icons)
{my_god2_icon}   Same, for --god2 — used by 2matches / 2gods
{vs_god_icon}    Same for the vs god
{vs2_god_icon}   Same for the second vs god (1v2 / 2matches presets)
{text}           Free text from --text
{subtext}        Free text from --subtext
{result}         --result win|loss → "WIN"/"LOSS"; empty if not given
{result2}        --result2 win|loss → "WIN"/"LOSS"; empty if not given (1v2 / 2matches)
{kda}            --kda K/D/A → "K/D/A"; empty if not given

Custom god cards (skin art override)
------------------------------------
The card resolver checks these locations in order before falling back to
the auto-downloaded base art under data/god_cards/:

    1. Custom God Cards/<God>-<Skin>.png   (when --skin/--skin2 is given)
    2. Custom God Cards/<God>.png          (default override for that god)
    3. data/god_cards/<slug>.png           (auto-downloaded base art)

Drop manually-downloaded skin art (e.g. wiki-missing skins) into
"Custom God Cards/" at the repo root. Filename must be exactly the god
display name, optionally suffixed with "-<SkinName>". Spaces in the
skin name are tolerated — "Sylvanus-Forest Lord.png" and
"Sylvanus-ForestLord.png" both match `--skin "Forest Lord"`.

Card flip overrides
-------------------
Each god slot has a flip toggle that *inverts* whatever the preset
already does, so you don't have to remember which way each preset
flips. Use these when the splash art happens to face away from the
opponent and you want them looking at each other:

    --flip-god     mirror --god's card
    --flip-vs      mirror --vs's card
    --flip-god2    mirror --god2's card  (2matches / 2gods)
    --flip-vs2     mirror --vs2's card   (1v2 / 2matches)

Usage
-----
    python tools/build_thumbnail.py --god Ymir --vs Loki --text "Pentakill" --result win --kda 12/3/8
    python tools/build_thumbnail.py --god "Hou Yi" --preset single --text "Solo Lane Domination"
    python tools/build_thumbnail.py --god Ymir --preset 1v1 --vs Loki --no-open
    python tools/build_thumbnail.py --god Awilix --preset 1v2 --vs Eset --vs2 Chiron --result win --result2 loss
    python tools/build_thumbnail.py --god Thanatos --vs "Baron Samedi" --god2 "Baron Samedi" --vs2 Awilix --preset 2matches
    python tools/build_thumbnail.py --god Sylvanus --god2 Atlas --preset 2gods --skin "Forest Lord"
    python tools/build_thumbnail.py --god Ymir --vs Loki --preset 1v1 --flip-god --flip-vs   # both face inward
"""

import argparse
import datetime as _dt
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageColor, ImageChops

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = REPO_ROOT / "thumbnail_presets"
GOD_CARDS_DIR = REPO_ROOT / "data" / "god_cards"
GOD_ICONS_DIR = REPO_ROOT / "data" / "god_icons"
CUSTOM_ICONS_DIR = REPO_ROOT / "Custom God Icons"
CUSTOM_CARDS_DIR = REPO_ROOT / "Custom God Cards"
OUT_DIR = REPO_ROOT / "thumbnails"

# Common Windows Paint.NET install locations. We try these in order;
# fall back to `os.startfile` if none exist.
PAINT_NET_CANDIDATES = [
    Path(r"C:\Program Files\paint.net\paintdotnet.exe"),
    Path(r"C:\Program Files\paint.net\PaintDotNet.exe"),
    Path(r"C:\Program Files (x86)\paint.net\paintdotnet.exe"),
    Path(r"C:\Program Files (x86)\paint.net\PaintDotNet.exe"),
]


# ============================================================
# UTILITIES
# ============================================================

def slugify(name):
    """'Hou Yi' -> 'hou-yi', 'Da'Ji' -> 'daji'. Matches download_god_icons.py."""
    return name.lower().replace("'", "").replace(" ", "-")


def resolve_god_card(name, skin=None):
    """
    Resolve the splash card art for a god, with optional skin variant.

    Lookup order:

      1. Custom God Cards/<God>-<Skin>.{png,webp,jpg,jpeg}  — if `skin`
         is provided. Also matches the no-separator form
         Custom God Cards/<God><Skin>.{png,webp,jpg,jpeg} (e.g.
         "SylvanusHighNoon.webp"), which is what you usually get when
         you save promo art directly off the Smite 2 site.
      2. Custom God Cards/<God>.{png,webp,jpg,jpeg}         — manual
         override of the default card for this god, no flag needed.
      3. data/god_cards/<slug>.png                          — auto-
         downloaded base art from download_god_cards.py.
      4. None — caller falls back to the layer's fallback_text.

    Custom God Cards/ is the manual-drop folder (parallel to the
    existing Custom God Icons/ convention). Drop skin splash art there.
    File matching is case-insensitive across a few common spellings of
    both the god name and the skin name (with/without spaces, Title Case
    vs lowercase) so you don't have to remember exact capitalization,
    and supports both hyphenated (`Sylvanus-High Noon.png`) and concat'd
    (`SylvanusHighNoon.webp`) filenames.
    """
    if not name:
        return None

    # Image formats Pillow opens out of the box. Order matters: PNG
    # wins on ties because it preserves transparency/quality, then
    # WEBP (which is what Smite 2's site usually serves), then JPEG.
    EXTS = (".png", ".webp", ".jpg", ".jpeg")

    def _first_match(base_names):
        seen = set()
        for stem in base_names:
            for ext in EXTS:
                candidate = CUSTOM_CARDS_DIR / f"{stem}{ext}"
                if candidate in seen:
                    continue
                seen.add(candidate)
                if candidate.exists():
                    return candidate
        return None

    # ── 1: Custom God Cards with explicit skin variant ─────────────
    if skin and CUSTOM_CARDS_DIR.exists():
        god_variants = (name, name.title(), name.lower(),
                        name.replace(" ", ""))
        skin_variants = (skin, skin.title(), skin.lower(),
                         skin.replace(" ", ""), skin.replace(" ", "_"))
        # Try hyphenated form first ("Sylvanus-High Noon"), then the
        # concat form ("SylvanusHighNoon" — what you get when you save
        # promo art directly off the wiki/site).
        stems = []
        for gv in god_variants:
            for sv in skin_variants:
                stems.append(f"{gv}-{sv}")
        for gv in god_variants:
            for sv in skin_variants:
                stems.append(f"{gv}{sv}")
        hit = _first_match(stems)
        if hit:
            return hit

    # ── 2: Custom God Cards default (no skin suffix) ───────────────
    if CUSTOM_CARDS_DIR.exists():
        hit = _first_match((name, name.title(), name.lower()))
        if hit:
            return hit

    # ── 3: Auto-downloaded base art (the existing path) ────────────
    p = GOD_CARDS_DIR / f"{slugify(name)}.png"
    return p if p.exists() else None


# Module-level toggle. When True, resolve_god_icon randomly picks
# from all available Custom God Icons variants. Set to False via
# --no-random-icons to force the primary every time. Reproducible
# random sequences come from setting a global seed via --seed (which
# calls random.seed() in main()).
_RANDOMIZE_ICONS = True


def resolve_god_icon_candidates(name):
    """
    Return Custom God Icons files that belong to `name`:
      <Display Name>.png                    (primary)
      <Display Name>-1.png, -2.png, ...     (numbered variants from import_god_icons.py)

    Skin-named files like '<Name>-Battleworn.png' are intentionally
    NOT pooled — they're legacy from an older bot version and
    shouldn't show up in the random rotation. Only files whose suffix
    after the final '-' is purely digits count as variants.

    Returns a list of Path objects, primary first, variants sorted by
    numeric suffix. Empty list if Custom God Icons/ has nothing matching.
    """
    if not name or not CUSTOM_ICONS_DIR.exists():
        return []

    paths = []
    seen = set()

    # Primary first (try exact display name, then Title Case as a fallback)
    for variant_name in (name, name.title()):
        primary = CUSTOM_ICONS_DIR / f"{variant_name}.png"
        if primary.exists() and primary not in seen:
            paths.append(primary)
            seen.add(primary)
            break

    # Numbered variants only — <Name>-<digits>.png
    numbered = []
    for variant_name in (name, name.title()):
        for f in CUSTOM_ICONS_DIR.glob(f"{variant_name}-*.png"):
            if f in seen:
                continue
            stem = f.stem
            tail = stem.rsplit("-", 1)[-1] if "-" in stem else ""
            if tail.isdigit():
                numbered.append((int(tail), f))
                seen.add(f)
    # Sort by numeric suffix so -1, -2, -10 stays in natural order.
    numbered.sort(key=lambda t: t[0])
    paths.extend(f for _, f in numbered)

    return paths


def resolve_god_icon(name):
    """
    Pick an icon for `name`. When `_RANDOMIZE_ICONS` is True (default)
    and multiple Custom God Icons variants exist, randomly choose one.
    Falls back to data/god_icons/<slug>.png if no custom icons exist.
    Returns Path or None.
    """
    if not name:
        return None

    candidates = resolve_god_icon_candidates(name)
    if candidates:
        if _RANDOMIZE_ICONS and len(candidates) > 1:
            return random.choice(candidates)
        return candidates[0]

    # Fallback to the canonical wiki icon library.
    p = GOD_ICONS_DIR / f"{slugify(name)}.png"
    return p if p.exists() else None


def resolve_color(value, default=(0, 0, 0, 255)):
    """
    Accept hex strings (#rgb, #rrggbb, #rrggbbaa), CSS color names, or
    [r,g,b]/[r,g,b,a] arrays. Returns a 4-tuple.
    """
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        if len(value) == 3:
            return (int(value[0]), int(value[1]), int(value[2]), 255)
        if len(value) == 4:
            return tuple(int(v) for v in value)
    if isinstance(value, str):
        try:
            rgba = ImageColor.getcolor(value, "RGBA")
            return rgba
        except Exception:
            return default
    return default


def apply_opacity(rgba, opacity):
    """Multiply alpha by opacity ∈ [0,1]."""
    if opacity is None:
        return rgba
    r, g, b, a = rgba
    return (r, g, b, max(0, min(255, int(a * float(opacity)))))


def safe_filename(text, max_len=80):
    """Sanitize a string for use in a filename."""
    out = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", " "):
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out).strip().replace(" ", "_")
    return s[:max_len] or "thumbnail"


# ============================================================
# PLACEHOLDER RESOLUTION
# ============================================================

def _normalize_result(value):
    """Convert a CLI --result value into a display string.

    Accepts 'w'/'win'/'W'/'WIN' → 'WIN', 'l'/'loss'/'lose' → 'LOSS',
    anything else → uppercased input. Empty input → empty string.
    """
    if not value:
        return ""
    r = value.strip().lower()
    if r in ("w", "win"):
        return "WIN"
    if r in ("l", "loss", "lose"):
        return "LOSS"
    return value.upper()


def build_placeholders(args):
    """
    Build the {placeholder: value} map used for both text substitution
    and src-path substitution.
    """
    # Skin selectors. Empty string when not given; passed to
    # resolve_god_card() which auto-falls-back through the lookup chain
    # (skin variant → default custom override → auto-downloaded base).
    skin1 = getattr(args, "skin", "") or ""
    skin2 = getattr(args, "skin2", "") or ""

    my_card = resolve_god_card(args.god, skin=skin1)
    vs_card = resolve_god_card(args.vs) if args.vs else None
    my_icon = resolve_god_icon(args.god)
    vs_icon = resolve_god_icon(args.vs) if args.vs else None

    # Second opponent (1v2 / 2matches presets). Empty / not provided → all the
    # vs2_* placeholders resolve to empty strings, so layers that
    # reference them no-op via skip_if_empty / fallback_text in the
    # preset.
    vs2_name = getattr(args, "vs2", "") or ""
    vs2_card = resolve_god_card(vs2_name) if vs2_name else None
    vs2_icon = resolve_god_icon(vs2_name) if vs2_name else None

    # User's second god (2matches / 2gods preset — when the player
    # played different gods in the same video). Skin selector --skin2
    # applies to this god, just like --skin applies to --god.
    my_god2_name = getattr(args, "god2", "") or ""
    my_god2_card = resolve_god_card(my_god2_name, skin=skin2) if my_god2_name else None
    my_god2_icon = resolve_god_icon(my_god2_name) if my_god2_name else None

    result = _normalize_result(args.result)
    result2 = _normalize_result(getattr(args, "result2", ""))

    # Headline defaults to my god's name; subtext defaults to vs god's
    # name. Explicit --text / --subtext overrides; --no-text / --no-subtext
    # disables the corresponding label.
    if getattr(args, "no_text", False):
        text_value = ""
    elif args.text is not None:
        text_value = args.text
    else:
        text_value = args.god or ""

    if getattr(args, "no_subtext", False):
        subtext_value = ""
    elif args.subtext is not None:
        subtext_value = args.subtext
    else:
        subtext_value = args.vs or ""

    return {
        "my_god": args.god or "",
        "my_god2": my_god2_name,
        "vs_god": args.vs or "",
        "vs2_god": vs2_name,
        "my_god_card": str(my_card) if my_card else "",
        "my_god2_card": str(my_god2_card) if my_god2_card else "",
        "vs_god_card": str(vs_card) if vs_card else "",
        "vs2_god_card": str(vs2_card) if vs2_card else "",
        "my_god_icon": str(my_icon) if my_icon else "",
        "my_god2_icon": str(my_god2_icon) if my_god2_icon else "",
        "vs_god_icon": str(vs_icon) if vs_icon else "",
        "vs2_god_icon": str(vs2_icon) if vs2_icon else "",
        "text": text_value,
        "subtext": subtext_value,
        "result": result,
        "result2": result2,
        "kda": args.kda or "",
    }


def substitute(value, placeholders):
    """Replace {placeholder} tokens in a string. Non-strings pass through."""
    if not isinstance(value, str):
        return value
    out = value
    for key, repl in placeholders.items():
        out = out.replace("{" + key + "}", repl)
    return out


# Map CLI flag names to the card placeholder string they target. When
# the flag is True, we toggle the layer's `flip_h` value rather than
# forcing it to True/False — that way the user just thinks "this god is
# facing the wrong way, add the flag" without having to remember which
# direction the preset chose by default.
_FLIP_FLAG_TO_CARD_TOKEN = {
    "flip_god":  "{my_god_card}",
    "flip_vs":   "{vs_god_card}",
    "flip_god2": "{my_god2_card}",
    "flip_vs2":  "{vs2_god_card}",
}


def apply_flip_overrides(preset, args):
    """
    Walk the preset's layers and toggle `flip_h` on image layers whose
    `src` references one of the card placeholders the user asked to
    flip via --flip-god / --flip-vs / --flip-god2 / --flip-vs2.

    Mutates `preset` in place. Safe to call even when no flip flags are
    set — it's a no-op in that case.
    """
    flagged_tokens = []
    for flag, token in _FLIP_FLAG_TO_CARD_TOKEN.items():
        if getattr(args, flag, False):
            flagged_tokens.append(token)
    if not flagged_tokens:
        return preset

    for layer in preset.get("layers", []):
        if layer.get("type") != "image":
            continue
        src = layer.get("src", "")
        if not isinstance(src, str):
            continue
        if any(token in src for token in flagged_tokens):
            layer["flip_h"] = not layer.get("flip_h", False)
    return preset


# ============================================================
# FONT LOADING
# ============================================================

# Common Windows TTF locations to probe. We try these as direct paths
# before relying on PIL's font resolver. Add to this list as needed.
WIN_FONTS = Path("C:/Windows/Fonts")
FONT_FILE_HINTS = {
    "impact": ["impact.ttf"],
    "arial": ["arial.ttf"],
    "arial bold": ["arialbd.ttf"],
    "arial black": ["ariblk.ttf"],
    "anton": ["Anton-Regular.ttf"],
    "bebas neue": ["BebasNeue-Regular.ttf"],
    "b612": ["B612-Regular.ttf"],
    "b612 bold": ["B612-Bold.ttf"],
    "comic sans": ["comic.ttf"],
    "verdana": ["verdana.ttf"],
    # Big Noodle Titling is the same condensed display face used by the
    # Hatmas Market overlays. Probe every reasonable filename variant
    # because different distributions ship with different conventions.
    # Both "Big Noodle Titling" and "BigNoodleTitling" in a preset
    # resolve to the same hint list.
    "big noodle titling": [
        "BigNoodleTitling.ttf",
        "big_noodle_titling.ttf",
        "Big_Noodle_Titling.ttf",
        "big noodle titling.ttf",
        "BigNoodleTitling-Regular.ttf",
        "bignoodletitling.ttf",
    ],
    "bignoodletitling": [
        "BigNoodleTitling.ttf",
        "BigNoodleTitling-Regular.ttf",
        "bignoodletitling.ttf",
    ],
}


# Cross-platform fallback chain for when the requested font isn't available
# (e.g. running the script on Linux where 'Impact' doesn't exist). On the
# user's Windows machine the first candidate (Impact) wins; the rest are
# safety nets so the script still produces a usable image elsewhere.
FONT_FALLBACK_PATHS = [
    # Prefer Big Noodle Titling when present (used by Hatmas Market overlays)
    Path(r"C:\Windows\Fonts\BigNoodleTitling.ttf"),
    Path(r"C:\Windows\Fonts\big_noodle_titling.ttf"),
    # Then plain Windows display fonts
    Path(r"C:\Windows\Fonts\impact.ttf"),
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path(r"C:\Windows\Fonts\ariblk.ttf"),
    # Last-ditch Linux fallbacks (only relevant when running outside Windows)
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"),
]


def _font_search_dirs():
    """Directories to scan for installed fonts. Includes both system-wide
    and per-user font locations on Windows — Paint.NET reads both, but
    Pillow only checks the system location by default."""
    dirs = [Path("C:/Windows/Fonts")]
    # %LOCALAPPDATA%\Microsoft\Windows\Fonts is where fonts installed
    # without admin rights end up (right-click font -> 'Install for me only').
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        dirs.append(Path(local_appdata) / "Microsoft" / "Windows" / "Fonts")
    return [d for d in dirs if d.exists()]


def _norm_font_name(s):
    """Strip everything but alphanumerics and lowercase. 'Big Noodle Titling'
    and 'BigNoodleTitling' and 'big_noodle_titling' all collapse to the
    same key, so a fuzzy match against actual filenames works."""
    return "".join(c for c in (s or "").lower() if c.isalnum())


# Cache of (normalized stem -> Path) built lazily on first use, so we
# don't re-scan font dirs for every text layer in a render.
_FONT_INDEX_CACHE = None


def _build_font_index():
    """Walk every font search dir and index files by normalized stem."""
    global _FONT_INDEX_CACHE
    if _FONT_INDEX_CACHE is not None:
        return _FONT_INDEX_CACHE
    index = {}
    for d in _font_search_dirs():
        try:
            for ext in ("*.ttf", "*.otf", "*.ttc", "*.TTF", "*.OTF"):
                for f in d.glob(ext):
                    key = _norm_font_name(f.stem)
                    if key and key not in index:
                        index[key] = f
        except Exception:
            pass
    _FONT_INDEX_CACHE = index
    return index


def load_font(name, size):
    """
    Best-effort font loader.

    Lookup order:
      1. Hint table by friendly name (fast path for common fonts).
      2. Fuzzy match against the actual filenames in C:\\Windows\\Fonts
         and %LOCALAPPDATA%\\Microsoft\\Windows\\Fonts. This catches the
         common case where a font was installed for the current user
         only (no admin rights), which Paint.NET handles transparently
         but Pillow does not by default.
      3. Pillow's own resolver (handles a small set of system fonts).
      4. Cross-platform fallback chain.
      5. PIL's bitmap default (last resort).
    """
    size = int(size)
    if not name:
        name = "Impact"

    # 1. Hint table — search every font dir, not just C:\Windows\Fonts.
    key = name.strip().lower()
    search_dirs = _font_search_dirs()
    for hint in FONT_FILE_HINTS.get(key, []):
        for d in search_dirs:
            candidate = d / hint
            if candidate.exists():
                try:
                    return ImageFont.truetype(str(candidate), size)
                except Exception:
                    pass

    # 2. Fuzzy match against the actual filename index.
    requested_norm = _norm_font_name(name)
    if requested_norm:
        index = _build_font_index()
        if requested_norm in index:
            try:
                return ImageFont.truetype(str(index[requested_norm]), size)
            except Exception:
                pass

    # 3. Pillow's resolver (bare name, then with .ttf suffix).
    for attempt in (name, f"{name}.ttf"):
        try:
            return ImageFont.truetype(attempt, size)
        except Exception:
            pass

    # 4. Walk the cross-platform fallback chain.
    for candidate in FONT_FALLBACK_PATHS:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size)
            except Exception:
                pass

    # Last resort: PIL default (low quality, but always works)
    return ImageFont.load_default()


# ============================================================
# LAYER RENDERERS
# Each renderer returns an RGBA PIL.Image of canvas size, fully
# composed for that layer (with all transparency in place).
# ============================================================

def _new_canvas(size):
    return Image.new("RGBA", size, (0, 0, 0, 0))


def _resolve_pos_size(layer, canvas_size):
    """
    Return (x, y, w, h) for the layer's bounding rect.
    pos defaults to [0,0]. size defaults to full canvas.
    """
    pos = layer.get("pos") or [0, 0]
    size = layer.get("size") or list(canvas_size)
    return int(pos[0]), int(pos[1]), int(size[0]), int(size[1])


def render_solid(layer, canvas_size):
    img = _new_canvas(canvas_size)
    x, y, w, h = _resolve_pos_size(layer, canvas_size)
    color = resolve_color(layer.get("color"), (0, 0, 0, 255))
    color = apply_opacity(color, layer.get("opacity", 1.0))
    rect = Image.new("RGBA", (w, h), color)
    img.paste(rect, (x, y), rect)
    return img


def render_gradient(layer, canvas_size):
    img = _new_canvas(canvas_size)
    x, y, w, h = _resolve_pos_size(layer, canvas_size)
    direction = (layer.get("direction") or "horizontal").lower()
    stops = layer.get("stops") or []
    if not stops:
        return img

    # Normalize stops: list of (at, rgba)
    norm = []
    for stop in stops:
        at = float(stop.get("at", 0.0))
        col = resolve_color(stop.get("color"), (0, 0, 0, 255))
        col = apply_opacity(col, stop.get("opacity", 1.0))
        norm.append((at, col))
    norm.sort(key=lambda s: s[0])

    grad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = grad.load()

    def lerp(a, b, t):
        return int(a + (b - a) * t)

    def color_at(t):
        # Clamp + find bracketing stops
        if t <= norm[0][0]:
            return norm[0][1]
        if t >= norm[-1][0]:
            return norm[-1][1]
        for i in range(len(norm) - 1):
            t0, c0 = norm[i]
            t1, c1 = norm[i + 1]
            if t0 <= t <= t1:
                frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return tuple(lerp(c0[k], c1[k], frac) for k in range(4))
        return norm[-1][1]

    if direction == "horizontal":
        for col in range(w):
            c = color_at(col / max(1, w - 1))
            for row in range(h):
                px[col, row] = c
    elif direction == "vertical":
        for row in range(h):
            c = color_at(row / max(1, h - 1))
            for col in range(w):
                px[col, row] = c
    elif direction == "diagonal":
        denom = max(1, (w - 1) + (h - 1))
        for row in range(h):
            for col in range(w):
                c = color_at((col + row) / denom)
                px[col, row] = c
    img.paste(grad, (x, y), grad)
    return img


def _resolve_anchor(anchor, w, h):
    """
    Convert anchor name into (dx, dy) offsets relative to (pos_x, pos_y)
    that should be subtracted to place the layer correctly.
    Anchor 'topleft' (default) returns (0, 0).
    """
    a = (anchor or "topleft").lower()
    aliases = {
        "left": "left_center",
        "right": "right_center",
        "top": "center_top",
        "bottom": "center_bottom",
    }
    a = aliases.get(a, a)

    if a == "topleft":
        return (0, 0)
    if a == "topright":
        return (w, 0)
    if a == "right_top":
        return (w, 0)
    if a == "bottomleft":
        return (0, h)
    if a == "bottomright":
        return (w, h)
    if a == "center":
        return (w // 2, h // 2)
    if a == "center_top":
        return (w // 2, 0)
    if a == "center_bottom":
        return (w // 2, h)
    if a == "left_center":
        return (0, h // 2)
    if a == "right_center":
        return (w, h // 2)
    return (0, 0)


def _fit_anchor_to_xy(anchor):
    """
    Map a `fit_anchor` value to (fx, fy) fractions ∈ [0, 1].
    Used to control where to crop from when fit='cover' has to drop
    pixels (e.g. 'top' keeps the top of the image, drops the bottom).

    Accepted forms:
        "center" / "top" / "bottom" / "left" / "right" / "top left" / etc.
        [fx, fy]  — explicit fractions, e.g. [0.5, 0.2] means
                    "horizontally centered, 20% from the top". Useful
                    when a god's face sits at a specific vertical
                    fraction of the source art and you want it landing
                    in the middle of the rendered card without
                    clipping the head.
    """
    # Numeric list/tuple form — explicit fractions
    if isinstance(anchor, (list, tuple)) and len(anchor) == 2:
        fx, fy = anchor
        try:
            fx = max(0.0, min(1.0, float(fx)))
            fy = max(0.0, min(1.0, float(fy)))
            return (fx, fy)
        except (TypeError, ValueError):
            return (0.5, 0.5)

    a = (anchor or "center").lower().replace("_", " ")
    presets = {
        "center":      (0.5, 0.5),
        "top":         (0.5, 0.0),
        "bottom":      (0.5, 1.0),
        "left":        (0.0, 0.5),
        "right":       (1.0, 0.5),
        "top left":    (0.0, 0.0),
        "topleft":     (0.0, 0.0),
        "top right":   (1.0, 0.0),
        "topright":    (1.0, 0.0),
        "bottom left": (0.0, 1.0),
        "bottomleft":  (0.0, 1.0),
        "bottom right":(1.0, 1.0),
        "bottomright": (1.0, 1.0),
    }
    return presets.get(a, (0.5, 0.5))


def _fit_image(img, target_w, target_h, fit, fit_anchor="center"):
    """
    Resize+crop/pad an image to (target_w, target_h) with the given fit mode.
    `fit_anchor` controls which part of the source is preserved when
    fit='cover' has to drop pixels.
    """
    src_w, src_h = img.size
    fit = (fit or "cover").lower()
    if fit == "stretch":
        return img.resize((target_w, target_h), Image.LANCZOS)
    if fit == "contain":
        scale = min(target_w / src_w, target_h / src_h)
        new_w, new_h = max(1, int(src_w * scale)), max(1, int(src_h * scale))
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        canvas.paste(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2), resized)
        return canvas
    # cover (default)
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = max(1, int(src_w * scale)), max(1, int(src_h * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    fx, fy = _fit_anchor_to_xy(fit_anchor)
    left = int((new_w - target_w) * fx)
    top = int((new_h - target_h) * fy)
    # Clamp so we never crop past the edges of the resized image.
    left = max(0, min(left, new_w - target_w))
    top = max(0, min(top, new_h - target_h))
    return resized.crop((left, top, left + target_w, top + target_h))


def _apply_edge_feather(img, *, left=0, right=0, top=0, bottom=0):
    """
    Soften image edges with linear alpha ramps. Edge widths in pixels.
    Used by image layers (preset key `feather_edges`) to blend two
    adjacent cards across a seam without the hard line being visible.
    """
    if not (left or right or top or bottom):
        return img
    w, h = img.size

    def edge_mask(side, distance):
        m = Image.new("L", (w, h), 255)
        if distance <= 0:
            return m
        if side == "left":
            for x in range(min(distance, w)):
                m.paste(int(255 * x / distance), (x, 0, x + 1, h))
        elif side == "right":
            for i in range(min(distance, w)):
                m.paste(int(255 * i / distance), (w - 1 - i, 0, w - i, h))
        elif side == "top":
            for y in range(min(distance, h)):
                m.paste(int(255 * y / distance), (0, y, w, y + 1))
        elif side == "bottom":
            for i in range(min(distance, h)):
                m.paste(int(255 * i / distance), (0, h - 1 - i, w, h - i))
        return m

    mask = Image.new("L", (w, h), 255)
    for side, dist in (("left", left), ("right", right),
                       ("top", top), ("bottom", bottom)):
        if dist > 0:
            mask = ImageChops.multiply(mask, edge_mask(side, dist))

    alpha = img.split()[-1]
    new_alpha = ImageChops.multiply(alpha, mask)
    r, g, b, _a = img.split()
    return Image.merge("RGBA", (r, g, b, new_alpha))


def render_image(layer, canvas_size, placeholders):
    img = _new_canvas(canvas_size)
    src = substitute(layer.get("src", ""), placeholders)

    if not src or not Path(src).exists():
        # Use fallback_text rendering so the thumbnail isn't broken when
        # an asset hasn't been downloaded yet.
        fb_text = substitute(layer.get("fallback_text") or "", placeholders)
        if not fb_text:
            return img
        x, y, w, h = _resolve_pos_size(layer, canvas_size)
        rect = Image.new("RGBA", (w, h), (32, 36, 44, 255))
        d = ImageDraw.Draw(rect)
        font = load_font("Impact", min(h // 4, 96))
        bbox = d.textbbox((0, 0), fb_text, font=font, stroke_width=4)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(
            ((w - tw) // 2, (h - th) // 2),
            fb_text,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=4,
            stroke_fill=(0, 0, 0, 255),
        )
        img.paste(rect, (x, y), rect)
        return img

    try:
        src_img = Image.open(src).convert("RGBA")
    except Exception as exc:
        print(f"  [warn] could not open {src}: {exc}")
        return img

    if layer.get("flip_h"):
        src_img = src_img.transpose(Image.FLIP_LEFT_RIGHT)

    x, y, w, h = _resolve_pos_size(layer, canvas_size)
    fit = layer.get("fit") or "cover"
    fit_anchor = layer.get("fit_anchor") or "center"
    fitted = _fit_image(src_img, w, h, fit, fit_anchor=fit_anchor)

    # Optional edge feathering — used to blend overlapping cards across
    # a seam. preset key: feather_edges: {left, right, top, bottom}.
    feather = layer.get("feather_edges") or {}
    fitted = _apply_edge_feather(
        fitted,
        left=int(feather.get("left", 0)),
        right=int(feather.get("right", 0)),
        top=int(feather.get("top", 0)),
        bottom=int(feather.get("bottom", 0)),
    )

    # Apply anchor offset
    dx, dy = _resolve_anchor(layer.get("anchor"), w, h)

    # Apply opacity
    opacity = float(layer.get("opacity", 1.0))
    if opacity < 1.0:
        alpha = fitted.split()[-1].point(lambda v: int(v * opacity))
        fitted.putalpha(alpha)

    img.paste(fitted, (x - dx, y - dy), fitted)
    return img


def _make_shadow(rgba_layer, shadow):
    """Return a shadow PIL.Image to be pasted *under* a layer."""
    if not shadow:
        return None
    color = resolve_color(shadow.get("color"), (0, 0, 0, 255))
    color = apply_opacity(color, shadow.get("opacity", 0.85))
    blur = float(shadow.get("blur", 8))

    # Build silhouette: same alpha as source, color filled.
    alpha = rgba_layer.split()[-1]
    sil = Image.new("RGBA", rgba_layer.size, color)
    sil.putalpha(alpha)
    if blur > 0:
        sil = sil.filter(ImageFilter.GaussianBlur(blur))
    return sil


def render_icon(layer, canvas_size, placeholders):
    img = _new_canvas(canvas_size)
    god = substitute(layer.get("god", ""), placeholders)
    if not god and layer.get("skip_if_empty", False):
        return img

    icon_path = resolve_god_icon(god) if god else None

    pos = layer.get("pos") or [0, 0]
    size = int(layer.get("size", 128))
    border = layer.get("border") or {}
    border_w = int(border.get("width", 0))
    border_color = resolve_color(border.get("color"), (255, 255, 255, 255))

    # We render at the icon's footprint (size × size). border is drawn
    # *outside* the icon area, so the bounding box becomes
    # (size + 2*border_w) on each side.
    box_size = size + 2 * border_w

    layer_img = Image.new("RGBA", (box_size, box_size), (0, 0, 0, 0))

    if icon_path and Path(icon_path).exists():
        try:
            src = Image.open(icon_path).convert("RGBA")
            src = src.resize((size, size), Image.LANCZOS)
            layer_img.paste(src, (border_w, border_w), src)
        except Exception as exc:
            print(f"  [warn] could not open icon {icon_path}: {exc}")
    else:
        # Fallback: colored rect with first letter.
        rect = Image.new("RGBA", (size, size), (32, 36, 44, 255))
        d = ImageDraw.Draw(rect)
        letter = (god[:1] or "?").upper()
        font = load_font("Impact", int(size * 0.7))
        bbox = d.textbbox((0, 0), letter, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((size - tw) // 2, (size - th) // 2), letter,
               font=font, fill=(220, 220, 220, 255))
        layer_img.paste(rect, (border_w, border_w), rect)

    if border_w > 0:
        d = ImageDraw.Draw(layer_img)
        # Draw the border as a rectangle outline at the outer edge.
        for offset in range(border_w):
            d.rectangle(
                [offset, offset, box_size - 1 - offset, box_size - 1 - offset],
                outline=border_color,
            )

    # Optional drop shadow
    shadow = layer.get("shadow")
    if shadow:
        sh = _make_shadow(layer_img, shadow)
        if sh:
            ox, oy = shadow.get("offset", [4, 4])
            shadow_canvas = Image.new("RGBA", (box_size + abs(int(ox)) * 2,
                                               box_size + abs(int(oy)) * 2),
                                      (0, 0, 0, 0))
            shadow_canvas.paste(sh, (int(ox) + abs(int(ox)),
                                     int(oy) + abs(int(oy))), sh)
            shadow_canvas.alpha_composite(layer_img,
                                          (abs(int(ox)), abs(int(oy))))
            layer_img = shadow_canvas
            box_size = layer_img.size[0]  # square assumption

    # Apply anchor offset. By default `pos` is the top-left corner of the
    # icon's bounding box (icon + border + shadow). Setting `anchor` to
    # topright / bottomright / center / etc. lets the preset position the
    # icon by a different reference point — useful for "stick the icon
    # 40px from the right edge of the canvas" without having to compute
    # the topleft x manually.
    final_w, final_h = layer_img.size
    dx, dy = _resolve_anchor(layer.get("anchor"), final_w, final_h)
    img.paste(layer_img, (int(pos[0]) - dx, int(pos[1]) - dy), layer_img)
    return img


def _wrap_text(text, font, max_width, draw):
    """Greedy word wrap. Returns list of lines."""
    if not max_width:
        return [text]
    words = text.split()
    lines, current = [], ""
    for w in words:
        candidate = (current + " " + w).strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = w
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render_text(layer, canvas_size, placeholders):
    img = _new_canvas(canvas_size)
    raw = substitute(layer.get("value", ""), placeholders)

    if not raw and layer.get("skip_if_empty", False):
        return img

    font = load_font(layer.get("font", "Impact"), layer.get("size", 48))
    fill = resolve_color(layer.get("fill"), (255, 255, 255, 255))
    stroke = layer.get("stroke") or {}
    stroke_width = int(stroke.get("width", 0))
    stroke_color = resolve_color(stroke.get("color"), (0, 0, 0, 255))

    # Wrap into lines if max_width is given.
    tmp = ImageDraw.Draw(img)
    max_width = layer.get("max_width")
    lines = _wrap_text(raw, font, max_width, tmp)

    line_heights = []
    line_widths = []
    for line in lines:
        bbox = tmp.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        line_widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])
    if not lines:
        return img

    # Use font metrics (ascent + descent) as a floor on line height —
    # textbbox() underreports the actual rendered stroke extent for
    # some fonts (e.g. Big Noodle Titling at high sizes), which would
    # otherwise clip the bottom of glyphs like V and S.
    try:
        ascent, descent = font.getmetrics()
        font_height = ascent + descent + stroke_width * 2
    except Exception:
        font_height = int(font.size * 1.25)
    safe_line_h = max(line_heights[-1], font_height)

    line_spacing = max(int(font.size * 1.1), font_height)
    total_h = (len(lines) - 1) * line_spacing + safe_line_h
    block_w = max(line_widths) if line_widths else 0

    # Render text + stroke onto a generously oversized RGBA canvas so
    # there's no chance of clipping. We crop to the actual rendered
    # content via getbbox() once we're done drawing.
    pad = max(stroke_width * 3, 16)
    block = Image.new(
        "RGBA",
        (block_w + pad * 2, total_h + pad * 2),
        (0, 0, 0, 0),
    )
    bd = ImageDraw.Draw(block)
    cy = pad
    for i, line in enumerate(lines):
        # Center each line horizontally in the block.
        bbox = bd.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        lw = bbox[2] - bbox[0]
        cx = pad + (block_w - lw) // 2
        bd.text(
            (cx, cy),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_color,
        )
        cy += line_spacing

    # Crop to the actual rendered content so the block stays tight
    # around the glyphs (anchor placement assumes the block is the
    # text's bounding box). Add a small margin for any subpixel stroke
    # spillover.
    content_bbox = block.getbbox()
    if content_bbox:
        margin = max(stroke_width, 4)
        l = max(0, content_bbox[0] - margin)
        t = max(0, content_bbox[1] - margin)
        r = min(block.size[0], content_bbox[2] + margin)
        b = min(block.size[1], content_bbox[3] + margin)
        block = block.crop((l, t, r, b))

    # Optional shadow drawn on a slightly larger canvas behind the block.
    shadow = layer.get("shadow")
    if shadow:
        sh = _make_shadow(block, shadow)
        if sh:
            ox, oy = shadow.get("offset", [3, 3])
            ox, oy = int(ox), int(oy)
            extra = max(abs(ox), abs(oy)) + int(shadow.get("blur", 0)) + 4
            wrap = Image.new("RGBA", (block.size[0] + extra * 2,
                                      block.size[1] + extra * 2), (0, 0, 0, 0))
            wrap.paste(sh, (extra + ox, extra + oy), sh)
            wrap.alpha_composite(block, (extra, extra))
            block = wrap
            pad = pad + extra
            block_w = block.size[0]
            total_h = block.size[1]

    # Anchor placement: pos is the anchor point.
    px, py = layer.get("pos") or [0, 0]
    px, py = int(px), int(py)
    dx, dy = _resolve_anchor(layer.get("anchor"), block.size[0], block.size[1])
    img.paste(block, (px - dx, py - dy), block)
    return img


LAYER_RENDERERS = {
    "solid":    render_solid,
    "gradient": render_gradient,
    "image":    render_image,
    "icon":     render_icon,
    "text":     render_text,
}


# ============================================================
# COMPOSITION
# ============================================================

def compose(preset, placeholders, override_size=None):
    """
    Returns (final_composite_image, [(layer_name, layer_image), ...]).
    The list preserves render order (bottom -> top).
    """
    canvas_size = tuple(override_size or preset.get("size", [1280, 720]))
    base = Image.new("RGBA", canvas_size, (0, 0, 0, 255))
    layer_outputs = []

    for idx, layer in enumerate(preset.get("layers", [])):
        renderer = LAYER_RENDERERS.get(layer.get("type"))
        if not renderer:
            print(f"  [warn] unknown layer type at index {idx}: {layer.get('type')!r}")
            continue
        if layer.get("type") in ("image", "icon", "text"):
            rendered = renderer(layer, canvas_size, placeholders)
        else:
            rendered = renderer(layer, canvas_size)

        layer_name = layer.get("name") or f"{layer.get('type', 'layer')}_{idx}"
        layer_outputs.append((layer_name, rendered))
        base.alpha_composite(rendered)

    return base, layer_outputs


# ============================================================
# OUTPUT
# ============================================================

def write_layered_pngs(layers_dir, layer_outputs):
    """Write one PNG per layer to <stem>_layers/ in render order."""
    layers_dir.mkdir(parents=True, exist_ok=True)
    # Wipe any old layer PNGs so stale layers from a previous run don't linger.
    for old in layers_dir.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass
    for idx, (name, img) in enumerate(layer_outputs):
        safe = safe_filename(name)
        img.save(layers_dir / f"{idx:02d}_{safe}.png")


def try_write_psd(psd_path, canvas_size, layer_outputs):
    """
    Optional layered PSD output. Requires `psd-tools` to be installed.
    Returns True if the PSD was written, False otherwise.
    """
    try:
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
    except Exception:
        return False

    try:
        psd = PSDImage.new(mode="RGBA", size=canvas_size, color=(0, 0, 0, 0))
        for name, img in layer_outputs:
            # psd-tools 1.10+ requires a `parent` argument; older versions
            # don't accept one. Try the new signature first, fall back.
            try:
                layer = PixelLayer.frompil(
                    img, psd_file=psd, parent=psd, layer_name=name
                )
            except TypeError:
                layer = PixelLayer.frompil(img, psd_file=psd, layer_name=name)
            psd.append(layer)
        psd.save(str(psd_path))
        return True
    except Exception as exc:
        print(f"  [warn] could not write PSD ({exc}). Continuing without it.")
        return False


def open_in_paint_net(path):
    """
    Try to launch Paint.NET on the given file. Falls back to the
    OS default handler if Paint.NET isn't found at the usual paths.
    """
    path = str(path)
    for candidate in PAINT_NET_CANDIDATES:
        if candidate.exists():
            try:
                subprocess.Popen([str(candidate), path], close_fds=True)
                return True
            except Exception as exc:
                print(f"  [warn] Paint.NET launch failed ({exc}); trying OS default.")
                break
    # OS default handler fallback (Windows: associates .pdn/.png with whatever
    # the user has set up; most Paint.NET users have .pdn associated).
    if hasattr(os, "startfile"):
        try:
            os.startfile(path)  # noqa: S606 - intentional Windows-only call
            return True
        except Exception as exc:
            print(f"  [warn] os.startfile failed: {exc}")
    return False


# ============================================================
# CLI
# ============================================================

def list_presets():
    if not PRESETS_DIR.exists():
        return []
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json"))


def load_preset(name):
    path = PRESETS_DIR / f"{name}.json"
    if not path.exists():
        avail = ", ".join(list_presets()) or "(none)"
        raise SystemExit(f"Preset '{name}' not found at {path}.\nAvailable: {avail}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_size(s):
    """Parse '1280x720' -> (1280, 720)."""
    if "x" not in s.lower():
        raise argparse.ArgumentTypeError(f"--size must be WxH, got '{s}'")
    parts = s.lower().split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"--size must be WxH, got '{s}'")
    return (int(parts[0]), int(parts[1]))


def _default_stem(args):
    """Build a default filename stem from inputs."""
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bits = [stamp, args.preset, safe_filename(args.god)]
    if args.vs:
        bits.append("vs_" + safe_filename(args.vs))
    if getattr(args, "god2", ""):
        bits.append("then_" + safe_filename(args.god2))
    if getattr(args, "vs2", ""):
        bits.append("vs_" + safe_filename(args.vs2))
    return "_".join(bits)


def main():
    parser = argparse.ArgumentParser(
        description="Build a YouTube thumbnail from a preset + god names."
    )
    parser.add_argument("--god", default="",
                        help="My god display name (used for {my_god}, card, icon).")
    parser.add_argument("--vs", default="",
                        help="Opposing god display name (1v1 / 1v2 / 2matches preset).")
    parser.add_argument("--vs2", default="",
                        help="Second opposing god display name (1v2 / 2matches preset). "
                             "Empty for 1v1.")
    parser.add_argument("--god2", default="",
                        help="My second god display name (2matches preset only — "
                             "for videos covering two matches where I switched gods "
                             "between matches). Empty for 1v1 / 1v2 / single.")
    parser.add_argument("--skin", default="",
                        help="Optional skin variant for --god. Looks for "
                             "'Custom God Cards/<God>-<Skin>.png' first, falling "
                             "back to 'Custom God Cards/<God>.png' and then the "
                             "auto-downloaded base art. Use this when you want a "
                             "specific skin's card art (e.g. a new Sylvanus skin) "
                             "instead of the default god card.")
    parser.add_argument("--skin2", default="",
                        help="Optional skin variant for --god2 (same lookup rules "
                             "as --skin). Used by 2matches and 2gods presets.")
    parser.add_argument("--flip-god", dest="flip_god", action="store_true",
                        help="Mirror my god's card horizontally (toggles whatever "
                             "the preset already does — useful when the splash "
                             "art is facing the wrong way and you want it looking "
                             "toward the opponent).")
    parser.add_argument("--flip-vs", dest="flip_vs", action="store_true",
                        help="Mirror the opposing god's card horizontally "
                             "(toggles the preset default).")
    parser.add_argument("--flip-god2", dest="flip_god2", action="store_true",
                        help="Mirror my second god's card horizontally "
                             "(2matches / 2gods presets — toggles the preset default).")
    parser.add_argument("--flip-vs2", dest="flip_vs2", action="store_true",
                        help="Mirror the second opposing god's card horizontally "
                             "(1v2 / 2matches presets — toggles the preset default).")
    parser.add_argument("--preset", default="1v1",
                        help=f"Preset name (default: 1v1). Available: {', '.join(list_presets()) or '(none)'}")
    parser.add_argument("--text", default=None,
                        help="Headline text above VS. Default: my god's name. Pass --no-text to disable.")
    parser.add_argument("--no-text", action="store_true",
                        help="Disable the headline above VS (overrides the auto-fill default).")
    parser.add_argument("--subtext", default=None,
                        help="Sub-headline text below VS. Default: opposing god's name. Pass --no-subtext to disable.")
    parser.add_argument("--no-subtext", action="store_true",
                        help="Disable the subtext below VS (overrides the auto-fill default).")
    parser.add_argument("--result", default="",
                        help="win/loss - fills {result} as WIN/LOSS, otherwise blank.")
    parser.add_argument("--result2", default="",
                        help="win/loss for the second matchup (1v2 preset) - "
                             "fills {result2} as WIN/LOSS, otherwise blank.")
    parser.add_argument("--kda", default="",
                        help="KDA string for {kda} placeholder, e.g. 12/3/8.")
    parser.add_argument("--size", type=parse_size, default=None,
                        help="Override canvas size, e.g. 1920x1080. Default: preset size.")
    parser.add_argument("--out", default=None,
                        help="Output PNG path. Default: thumbnails/<auto>.png")
    parser.add_argument("--no-open", action="store_true",
                        help="Don't auto-launch Paint.NET on the result.")
    parser.add_argument("--no-random-icons", action="store_true",
                        help="Always use the primary <God>.png icon, even when Custom God Icons variants exist.")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for the icon variant picker (reproducible renders).")
    parser.add_argument("--list", action="store_true",
                        help="List available presets and exit.")
    args = parser.parse_args()

    if args.list:
        for p in list_presets():
            print(p)
        return

    if not args.god:
        parser.error("--god is required (unless --list is used)")

    # Configure icon variant randomization
    global _RANDOMIZE_ICONS
    if args.no_random_icons:
        _RANDOMIZE_ICONS = False
    if args.seed is not None:
        random.seed(args.seed)

    preset = load_preset(args.preset)
    apply_flip_overrides(preset, args)
    placeholders = build_placeholders(args)

    if not placeholders["my_god_card"]:
        print(f"  [warn] No card found for '{args.god}'. Run "
              f"`python tools/download_god_cards.py --add \"{args.god}\"` to fetch it.")
    if args.vs and not placeholders["vs_god_card"]:
        print(f"  [warn] No card found for '{args.vs}'. Run "
              f"`python tools/download_god_cards.py --add \"{args.vs}\"` to fetch it.")
    if args.vs2 and not placeholders["vs2_god_card"]:
        print(f"  [warn] No card found for '{args.vs2}'. Run "
              f"`python tools/download_god_cards.py --add \"{args.vs2}\"` to fetch it.")
    if args.god2 and not placeholders["my_god2_card"]:
        print(f"  [warn] No card found for '{args.god2}'. Run "
              f"`python tools/download_god_cards.py --add \"{args.god2}\"` to fetch it.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.out:
        out_png = Path(args.out)
        if out_png.is_dir():
            out_png = out_png / f"{_default_stem(args)}.png"
    else:
        out_png = OUT_DIR / f"{_default_stem(args)}.png"
    out_png.parent.mkdir(parents=True, exist_ok=True)

    print(f"Building thumbnail: preset={args.preset}, god={args.god}, vs={args.vs or '-'}")

    # Surface available icon variants per god
    for label, god_name in (("My god", args.god), ("Vs god", args.vs)):
        if not god_name:
            continue
        candidates = resolve_god_icon_candidates(god_name)
        if len(candidates) > 1:
            mode = "primary only" if not _RANDOMIZE_ICONS else "random pick"
            print(f"  {label} '{god_name}': {len(candidates)} icon variants "
                  f"({mode}) - {', '.join(c.name for c in candidates)}")
        elif candidates:
            print(f"  {label} '{god_name}': 1 icon - {candidates[0].name}")

    composite, layer_outputs = compose(preset, placeholders, override_size=args.size)

    composite.convert("RGB").save(out_png, "PNG", optimize=True)
    print(f"  Wrote flat composite: {out_png}")

    layers_dir = out_png.with_name(out_png.stem + "_layers")
    write_layered_pngs(layers_dir, layer_outputs)
    print(f"  Wrote {len(layer_outputs)} layer PNGs: {layers_dir}")

    psd_path = out_png.with_suffix(".psd")
    canvas_size = tuple(args.size or preset.get("size", [1280, 720]))
    psd_written = try_write_psd(psd_path, canvas_size, layer_outputs)
    if psd_written:
        print(f"  Wrote layered PSD: {psd_path}")
    else:
        print("  (skipped PSD - install `psd-tools` for single-file layered output)")

    if not args.no_open:
        target = psd_path if psd_written and psd_path.exists() else out_png
        if open_in_paint_net(target):
            print(f"  Opened in Paint.NET: {target}")
        else:
            print(f"  Could not auto-open. File is at: {target}")


if __name__ == "__main__":
    main()
