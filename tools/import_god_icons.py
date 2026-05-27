"""
Import God Icons (auto crop / resize / center)
==============================================
Drop candidate images into Custom_Icons_Inbox/ and this tool will:

  1. Detect which god each image is for, from the filename.
  2. Smart-crop to 1:1 aspect (top-biased by default — heads up).
  3. Resize to 512x512 with high-quality LANCZOS.
  4. Save as PNG into Custom God Icons/ with these names:
        <God>.png        primary icon (used by build_thumbnail.py)
        <God>-1.png      first numbered variant
        <God>-2.png      second numbered variant, etc.
  5. Move processed source files into Custom_Icons_Inbox/_processed/
     so you can see at a glance what's been imported.

Filename detection
------------------
The tool fuzzy-matches the filename stem against the known god list
(parsed from the saved 'Gods - SMITE 2 Wiki.html'). All of these get
mapped to "Hou Yi":
    Hou Yi.png
    Hou_Yi.jpg
    HouYi.gif
    Hou Yi-2.png
    HouYi-3.webp
    hou_yi-skin.png

If the filename can't be matched to any god, the file is left in the
inbox with an [??] tag in the output so you can rename it and retry.

Existing-icon behavior
----------------------
By default, if <God>.png already exists in Custom God Icons/, the
input image is SKIPPED entirely (your "leave them alone" preference).
Use:
    --variants     to save as the next free <God>-N.png instead of skipping
    --force        to overwrite the existing primary

Crop bias
---------
Controls which slice of a tall portrait survives the 1:1 crop:
    --crop-bias top     keep the top of the image (DEFAULT — heads up)
    --crop-bias center  keep the middle
    --crop-bias bottom  keep the bottom

For wide (landscape) inputs, the crop is always horizontally centered.
For square inputs, no crop is needed and only the resize runs.

Usage
-----
    python tools/import_god_icons.py
    python tools/import_god_icons.py --dry-run
    python tools/import_god_icons.py --variants                   # add to gods that already have a primary
    python tools/import_god_icons.py --crop-bias center
    python tools/import_god_icons.py --inbox staging/             # alternate inbox folder
    python tools/import_god_icons.py --list-missing               # list gods without a primary in Custom God Icons/
"""

import argparse
import re
import shutil
import sys
import traceback
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("[!] Pillow is required. Install with: pip install Pillow", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Reuse the wiki god-list parser that download_god_icons.py exposes.
import importlib.util as _importlib_util
try:
    _icon_spec = _importlib_util.spec_from_file_location(
        "_download_god_icons", REPO_ROOT / "download_god_icons.py"
    )
    _icon_mod = _importlib_util.module_from_spec(_icon_spec)
    _icon_spec.loader.exec_module(_icon_mod)
    parse_wiki_html = _icon_mod.parse_wiki_html
except Exception as _exc:
    print(f"[!] Could not load parse_wiki_html from download_god_icons.py: "
          f"{type(_exc).__name__}: {_exc}", file=sys.stderr)
    traceback.print_exc()
    sys.exit(2)

DEFAULT_INBOX = REPO_ROOT / "Custom_Icons_Inbox"
DEFAULT_OUTPUT = REPO_ROOT / "Custom God Icons"
PROCESSED_SUBFOLDER = "_processed"

SUPPORTED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
}


# ============================================================
# FILENAME -> GOD NAME MATCHING
# ============================================================

def _normalize(s):
    """Lowercase, strip spaces / underscores / hyphens / apostrophes."""
    return re.sub(r"[\s_\-']+", "", (s or "").lower())


def parse_god_from_filename(filename, known_gods):
    """
    Find which god a filename refers to.

    Tries the full stem first, then progressively strips trailing
    "-<thing>" suffixes (variant markers like -2, -skin, -3 etc.).

    Returns the canonical display name or None.
    """
    stem = Path(filename).stem
    if not stem:
        return None

    # Build a normalized -> display lookup once
    by_norm = {_normalize(g["name"]): g["name"] for g in known_gods}

    # Generate candidate substrings, longest first.
    candidates = [stem]

    # Strip trailing "-<anything>" suffixes one at a time.
    cur = stem
    while "-" in cur:
        cur = cur.rsplit("-", 1)[0]
        if cur:
            candidates.append(cur)

    # Same idea but for underscores (e.g. "Hou_Yi_skin")
    cur = stem
    while "_" in cur:
        cur = cur.rsplit("_", 1)[0]
        if cur:
            candidates.append(cur)

    # Match (longest first), case- and separator-insensitive.
    for candidate in sorted(set(candidates), key=lambda c: -len(c)):
        norm = _normalize(candidate)
        if norm in by_norm:
            return by_norm[norm]

    return None


# ============================================================
# CROP / RESIZE
# ============================================================

def smart_crop_to_square(img, bias="top"):
    """Crop to 1:1 aspect with vertical bias for portrait images."""
    w, h = img.size
    if w == h:
        return img
    if w < h:
        # Portrait: crop top, center, or bottom.
        side = w
        if bias == "top":
            top = 0
        elif bias == "bottom":
            top = h - side
        else:  # center
            top = (h - side) // 2
        return img.crop((0, top, side, top + side))
    # Landscape: always crop center horizontally.
    side = h
    left = (w - side) // 2
    return img.crop((left, 0, left + side, side))


# ============================================================
# OUTPUT NAMING
# ============================================================

def next_output_path(output_dir, god_name, *, variants=False, force=False):
    """
    Decide where to write the icon for `god_name`.

    Returns (path, kind) where kind is one of:
      'primary'    — saving as <God>.png (no existing primary, or --force)
      'variant_N'  — saving as <God>-N.png because primary exists and --variants was passed
      'skip'       — primary exists and we're not allowed to overwrite/variant
    """
    primary = output_dir / f"{god_name}.png"
    if not primary.exists() or force:
        return (primary, "primary")
    if not variants:
        return (None, "skip")
    n = 1
    while (output_dir / f"{god_name}-{n}.png").exists():
        n += 1
    return (output_dir / f"{god_name}-{n}.png", f"variant_{n}")


# ============================================================
# PER-IMAGE PROCESSING
# ============================================================

def process_image(src_path, output_dir, known_gods, *,
                  size=512, bias="top",
                  variants=False, force=False, dry_run=False):
    """
    Process one image. Returns (status, message, output_path).
    Status: 'processed', 'skipped', 'unmatched', 'error'.
    """
    god = parse_god_from_filename(src_path.name, known_gods)
    if not god:
        return ("unmatched",
                f"no god matched from filename '{src_path.name}'",
                None)

    out_path, kind = next_output_path(
        output_dir, god, variants=variants, force=force
    )
    if kind == "skip":
        return ("skipped",
                f"'{god}' already has a primary icon "
                f"(use --variants to add as <God>-N.png)",
                None)

    if dry_run:
        return ("processed",
                f"would save {src_path.name} -> {out_path.name} ({kind})",
                out_path)

    try:
        img = Image.open(src_path).convert("RGBA")
    except Exception as exc:
        return ("error",
                f"could not open {src_path.name}: "
                f"{type(exc).__name__}: {exc}",
                None)

    cropped = smart_crop_to_square(img, bias=bias)
    if cropped.size != (size, size):
        cropped = cropped.resize((size, size), Image.LANCZOS)

    output_dir.mkdir(parents=True, exist_ok=True)
    cropped.save(out_path, "PNG", optimize=True)

    return ("processed",
            f"{src_path.name} -> {out_path.name} ({kind})",
            out_path)


# ============================================================
# CLI
# ============================================================

def list_missing(known_gods, output_dir):
    """Print every god that doesn't have a primary <God>.png in output_dir."""
    missing = [
        g for g in known_gods
        if not (output_dir / f"{g['name']}.png").exists()
    ]
    if not missing:
        print(f"All {len(known_gods)} gods have a primary icon in {output_dir}/")
        return
    print(f"{len(missing)} of {len(known_gods)} gods missing a primary icon:")
    for g in missing:
        print(f"  - {g['name']}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Auto-import god icons: crop, size, center, and place "
            "into Custom God Icons/."
        )
    )
    parser.add_argument(
        "--inbox", type=Path, default=DEFAULT_INBOX,
        help=f"Inbox folder to scan (default: {DEFAULT_INBOX.name})"
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output folder for processed icons (default: 'Custom God Icons')"
    )
    parser.add_argument(
        "--size", type=int, default=512,
        help="Output square dimension in pixels (default: 512)"
    )
    parser.add_argument(
        "--crop-bias", choices=["top", "center", "bottom"], default="top",
        help="Vertical anchor for the 1:1 crop on portrait inputs (default: top)"
    )
    parser.add_argument(
        "--variants", action="store_true",
        help="If <God>.png already exists, save as next free <God>-N.png "
             "instead of skipping."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite the primary <God>.png if it exists."
    )
    parser.add_argument(
        "--keep-source", action="store_true",
        help="Don't move processed files into _processed/ subfolder."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without saving anything."
    )
    parser.add_argument(
        "--list-missing", action="store_true",
        help="List gods without a primary icon in the output folder, then exit."
    )
    args = parser.parse_args()

    print(f"[i] Inbox:  {args.inbox}")
    print(f"[i] Output: {args.output}")
    print(f"[i] Size:   {args.size}x{args.size}, crop-bias={args.crop_bias}")

    known_gods = parse_wiki_html()
    if not known_gods:
        print("[!] Could not parse god list from 'Gods - SMITE 2 Wiki.html'.")
        print("[!] Re-save the wiki Gods page to refresh the list.")
        return 1
    print(f"[i] {len(known_gods)} gods in known list")

    if args.list_missing:
        list_missing(known_gods, args.output)
        return 0

    if not args.inbox.exists():
        print(f"\n[!] Inbox folder does not exist: {args.inbox}")
        print(f"[!] Create it and drop candidate images, then re-run.")
        print(f"[!] Suggested files: 'Atlas.png', 'Hou Yi.jpg', 'Geb-2.gif', etc.")
        return 1

    candidates = sorted(
        p for p in args.inbox.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not candidates:
        print(f"\n[i] No candidate images in {args.inbox}/")
        print(f"[i] Drop some files in there and re-run.")
        return 0

    print(f"[i] Found {len(candidates)} candidate image(s)\n")

    stats = {"processed": 0, "skipped": 0, "unmatched": 0, "error": 0}
    processed_paths = []

    for src in candidates:
        status, message, _out = process_image(
            src, args.output, known_gods,
            size=args.size, bias=args.crop_bias,
            variants=args.variants, force=args.force,
            dry_run=args.dry_run,
        )
        stats[status] += 1
        prefix = {
            "processed": "[OK]  ",
            "skipped":   "[skip]",
            "unmatched": "[??]  ",
            "error":     "[!]   ",
        }.get(status, "[?]  ")
        print(f"  {prefix} {message}")
        if status == "processed":
            processed_paths.append(src)

    # Move successfully-processed source files to _processed/ subfolder.
    if processed_paths and not args.keep_source and not args.dry_run:
        processed_dir = args.inbox / PROCESSED_SUBFOLDER
        processed_dir.mkdir(exist_ok=True)
        for src in processed_paths:
            try:
                shutil.move(str(src), str(processed_dir / src.name))
            except Exception as exc:
                print(f"  [warn] could not move {src.name} to "
                      f"{PROCESSED_SUBFOLDER}/: {exc}")

    print(f"\nDone!")
    for k in ("processed", "skipped", "unmatched", "error"):
        if stats[k]:
            print(f"  {k}: {stats[k]}")

    if stats["unmatched"]:
        print(f"\nUnmatched filenames couldn't be parsed to a known god.")
        print(f"Rename them to '<God Name>.png' or '<God Name>-1.png' and re-run.")
        print(f"Tip: 'python tools/import_god_icons.py --list-missing' "
              f"shows which gods need icons.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[!] Unhandled error: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
