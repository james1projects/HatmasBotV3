"""
tools/redblue_thumbnail.py
==========================

Companion to `tools/redblue_tally.py`. Reads the current vote tally
out of `data/redblue.db` for a given YouTube video, composites a
red/blue threshold bar onto `data/redblue/template.png`, writes the
result to `data/redblue/<video_id>.png`, and (optionally) uploads it
to YouTube via the existing OAuth credentials in
`tools/youtube_live_badge.py`.

Strip layout (verified against the saved template):

    The template is 1280x720. The black bottom strip occupies rows
    575..719 (145px tall, x = 0..1280). All compositing happens inside
    that strip — buttons above are never touched.

    +-------------------------------------------------+ y=575
    |   [bar inset 40px each side]                    |
    |   RED 247                              BLUE 153 |
    |   ##############|::::::::                       | bar
    |                  ↑ 50% threshold (dashed)       |
    +-------------------------------------------------+ y=720

    Red fills from the left up to red%, blue fills from the right up
    to blue%. They meet at the actual vote ratio. The dashed white
    line at x=640 marks the 50% threshold (the whole point of the
    trend — does Blue cross it or not).

Subcommands:

    render <video_id>            Composite a thumbnail from current DB tally.
    render <video_id> --counts R,B
                                 Composite from explicit counts (no DB read).
                                 Useful for design previews without scanning.
    upload <video_id>            Push the most recent rendered PNG to YouTube.
    update <video_id>            Render + upload in one step. Idempotent —
                                 skips the upload if counts are unchanged
                                 since the last successful upload (use --force
                                 to override).

Quota / rate notes:

    `thumbnails.set` is 50 quota units per call. With the default 10k/day
    budget that's 200 thumbnail edits per day — way more than you'd want
    anyway. YouTube's heuristic flags rapid thumbnail churn (>5/hr per
    video). Don't run `update` more often than every ~15 minutes for the
    same video.

Setup:

    1. `data/redblue/template.png` — your 1280x720 base thumbnail with
       the bottom strip empty (black). Already saved.
    2. OAuth — reuses `data/youtube_oauth.json` from
       `tools/youtube_live_badge.py`. If you've already run `go_live`
       once, you're done. Otherwise:
           python tools/youtube_live_badge.py auth

Usage:

    # Quick design preview, no DB read, no upload
    python tools/redblue_thumbnail.py render abc123XYZ --counts 247,153 --open

    # Render based on the live tally
    python tools/redblue_thumbnail.py render abc123XYZ --open

    # Full loop: render + upload (skipped if unchanged)
    python tools/redblue_thumbnail.py update abc123XYZ
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Force unbuffered stdout so .bat wrappers / loop-watchers see live output.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("[!] Pillow is required. pip install pillow", file=sys.stderr)
    sys.exit(2)

# Reuse the tally DB helpers so there is exactly one source of truth
# for vote counts.
from tools.redblue_tally import open_db, current_tally  # noqa: E402


# ============================================================
# PATHS
# ============================================================

DATA_DIR = REPO_ROOT / "data" / "redblue"
TEMPLATE_PATH = DATA_DIR / "template.png"
STATE_PATH = DATA_DIR / "state.json"


# ============================================================
# STRIP / BAR GEOMETRY (matches the verified template)
# ============================================================

CANVAS_W = 1280
CANVAS_H = 720

STRIP_TOP = 575
STRIP_BOTTOM = 720
STRIP_HEIGHT = STRIP_BOTTOM - STRIP_TOP   # 145

# Bar geometry — fills the strip edge-to-edge so the entire black
# region is overwritten by red/blue (no black margin above, below, or
# on the sides).
BAR_INSET_X = 0
BAR_LEFT = BAR_INSET_X                     # 0
BAR_RIGHT = CANVAS_W - BAR_INSET_X         # 1280
BAR_WIDTH = BAR_RIGHT - BAR_LEFT           # 1280
BAR_HEIGHT = STRIP_HEIGHT                  # 145 — full strip height
BAR_TOP = STRIP_TOP                        # 575
BAR_BOTTOM = STRIP_BOTTOM                  # 720

# 50% threshold marker — vertical dashed line at the canvas center.
THRESHOLD_X = CANVAS_W // 2                # 640

# Text padding from canvas edges.
TEXT_PAD_X = 60

# Display-text font size. Bumped for the full-height bar.
LABEL_FONT_SIZE = 96


# ============================================================
# COLORS
# ============================================================

# Matched to the actual button colors in the template image.
RED_COLOR = (200, 48, 46)
BLUE_COLOR = (45, 88, 209)
TEXT_COLOR = (255, 255, 255)
TEXT_STROKE_COLOR = (0, 0, 0)
THRESHOLD_COLOR = (255, 255, 255)


# ============================================================
# FONT LOADER
# ============================================================
#
# Mirrors build_thumbnail.py's font discovery — looks in both
# system-wide and per-user font folders, prefers Big Noodle Titling
# (the brand face), and gracefully falls back to Impact / Arial Black /
# DejaVu Sans Bold so the renderer never crashes on a clean machine.

PREFERRED_FONTS = [
    "BigNoodleTitling",
    "Big Noodle Titling",
    "BigNoodleTooOblique",
]
FALLBACK_FONTS = ["Impact", "Arial Black", "Arial Bold", "DejaVu Sans Bold"]


def _font_search_dirs():
    dirs = [Path("C:/Windows/Fonts")]
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return dirs


def find_font(size: int) -> ImageFont.FreeTypeFont:
    """Locate the best-available display font at `size` pt."""
    search_dirs = _font_search_dirs()
    candidates = []
    for name in PREFERRED_FONTS + FALLBACK_FONTS:
        for d in search_dirs:
            for ext in (".ttf", ".otf"):
                candidates.append(d / f"{name}{ext}")
        # Also try to let PIL find the font by family name (works on some systems).
        candidates.append(name)

    last_err: Optional[Exception] = None
    for cand in candidates:
        try:
            if isinstance(cand, Path):
                if cand.exists():
                    return ImageFont.truetype(str(cand), size)
            else:
                return ImageFont.truetype(cand, size)
        except Exception as e:
            last_err = e
            continue

    # Last-resort: bitmap default (looks bad but won't crash).
    print(
        f"[!] Could not locate a TrueType font (last error: {last_err}). "
        "Falling back to PIL default — text will look blocky.",
        file=sys.stderr,
    )
    return ImageFont.load_default()


# ============================================================
# RENDER
# ============================================================

def render(
    red: int,
    blue: int,
    *,
    out_path: Path,
) -> Path:
    """
    Composite the threshold bar + text onto the template and save.

    Always paints inside the strip area (y=575..720) — never touches
    the buttons above. Returns the absolute output path.
    """
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(
            f"Template not found at {TEMPLATE_PATH}. "
            "Save your 1280x720 base PNG there first."
        )

    base = Image.open(TEMPLATE_PATH).convert("RGBA")
    if base.size != (CANVAS_W, CANVAS_H):
        raise ValueError(
            f"Template must be exactly {CANVAS_W}x{CANVAS_H} "
            f"(got {base.size}). Resize and re-save."
        )

    # Compute bar split.
    total = red + blue
    if total == 0:
        red_extent = 0
        blue_extent = 0
    else:
        red_frac = red / total
        red_extent = int(round(BAR_WIDTH * red_frac))
        # Clamp so floating-point rounding never pushes us past the bar.
        red_extent = max(0, min(BAR_WIDTH, red_extent))
        blue_extent = BAR_WIDTH - red_extent

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    # Red bar (from left).
    if red_extent > 0:
        d.rectangle(
            [(BAR_LEFT, BAR_TOP), (BAR_LEFT + red_extent, BAR_BOTTOM)],
            fill=RED_COLOR + (255,),
        )

    # Blue bar (from right).
    if blue_extent > 0:
        d.rectangle(
            [(BAR_RIGHT - blue_extent, BAR_TOP), (BAR_RIGHT, BAR_BOTTOM)],
            fill=BLUE_COLOR + (255,),
        )

    # Dashed 50% threshold marker — drawn LAST so it sits on top of the bar.
    _draw_dashed_vertical(
        d,
        x=THRESHOLD_X,
        y_top=BAR_TOP,
        y_bottom=BAR_BOTTOM,
        dash_on=14,
        dash_off=10,
        width=5,
        color=THRESHOLD_COLOR + (230,),
    )

    # Text labels — centered vertically on the bar.
    font = find_font(LABEL_FONT_SIZE)
    text_y = (BAR_TOP + BAR_BOTTOM) // 2

    red_text = f"RED {red}"
    blue_text = f"BLUE {blue}"

    d.text(
        (TEXT_PAD_X, text_y),
        red_text,
        font=font,
        fill=TEXT_COLOR + (255,),
        anchor="lm",
        stroke_width=3,
        stroke_fill=TEXT_STROKE_COLOR + (255,),
    )
    d.text(
        (CANVAS_W - TEXT_PAD_X, text_y),
        blue_text,
        font=font,
        fill=TEXT_COLOR + (255,),
        anchor="rm",
        stroke_width=3,
        stroke_fill=TEXT_STROKE_COLOR + (255,),
    )

    composited = Image.alpha_composite(base, overlay).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    composited.save(out_path, "PNG", optimize=True)
    return out_path


def _draw_dashed_vertical(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y_top: int,
    y_bottom: int,
    dash_on: int,
    dash_off: int,
    width: int,
    color: tuple,
):
    """Pillow has no native dashed-line primitive. Walk the segment manually."""
    y = y_top
    period = dash_on + dash_off
    while y < y_bottom:
        y2 = min(y + dash_on, y_bottom)
        draw.line([(x, y), (x, y2)], fill=color, width=width)
        y += period


# ============================================================
# STATE (idempotent uploads)
# ============================================================

def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"videos": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"videos": {}}


def _save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ============================================================
# COMMANDS
# ============================================================

def _output_path(video_id: str) -> Path:
    return DATA_DIR / f"{video_id}.png"


def _resolve_counts(args) -> Tuple[int, int]:
    """Either parse --counts R,B or pull live counts from the tally DB."""
    if args.counts:
        try:
            r_str, b_str = args.counts.split(",", 1)
            return int(r_str), int(b_str)
        except Exception:
            print(
                f"[!] --counts must look like 'RED,BLUE' (got {args.counts!r})",
                file=sys.stderr,
            )
            sys.exit(2)
    conn = open_db()
    try:
        return current_tally(conn, args.video_id)
    finally:
        conn.close()


def cmd_render(args):
    red, blue = _resolve_counts(args)
    out = Path(args.out) if args.out else _output_path(args.video_id)
    render(red, blue, out_path=out)
    total = red + blue
    print(f"[+] Rendered {out}")
    print(f"    Red {red}  |  Blue {blue}  |  Total {total}")
    if args.open:
        try:
            os.startfile(str(out))  # Windows-only
        except AttributeError:
            print("[i] --open is Windows-only. Skipping.")
        except Exception as e:
            print(f"[!] Could not open: {e}")


def cmd_upload(args):
    out = _output_path(args.video_id)
    if not out.exists():
        print(
            f"[!] No rendered thumbnail at {out}. Run `render` first.",
            file=sys.stderr,
        )
        sys.exit(2)

    yt, set_thumbnail = _youtube_handles()
    print(f"[i] Uploading {out} -> video {args.video_id}")
    set_thumbnail(yt, args.video_id, out)
    print("[+] Upload OK.")


def cmd_update(args):
    """Render + upload, with idempotency."""
    red, blue = _resolve_counts(args)
    state = _load_state()
    last = state.setdefault("videos", {}).get(args.video_id, {})

    same = (
        last.get("last_uploaded_red") == red
        and last.get("last_uploaded_blue") == blue
    )
    if same and not args.force:
        print(
            f"[i] Counts unchanged since last upload (red={red}, blue={blue}). "
            "Skipping upload. Use --force to override."
        )
        # Still re-render so the local PNG is up to date in case the user
        # tweaked the renderer between runs.
        out = _output_path(args.video_id)
        render(red, blue, out_path=out)
        print(f"[+] Re-rendered {out}")
        return

    out = _output_path(args.video_id)
    render(red, blue, out_path=out)
    print(f"[+] Rendered {out}  (red={red}, blue={blue})")

    yt, set_thumbnail = _youtube_handles()
    print(f"[i] Uploading -> video {args.video_id}")
    set_thumbnail(yt, args.video_id, out)
    print("[+] Upload OK.")

    state["videos"][args.video_id] = {
        "last_uploaded_red": red,
        "last_uploaded_blue": blue,
        "last_uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save_state(state)


def _youtube_handles():
    """
    Lazy-import the youtube_live_badge OAuth helpers. Keeps the
    `render`-only path zero-dependency on Google client libs.
    """
    try:
        from tools.youtube_live_badge import (
            youtube_client,
            upload_thumbnail,
        )
    except Exception as e:
        print(
            f"[!] Could not import youtube_live_badge helpers: {e}",
            file=sys.stderr,
        )
        print(
            "[!] If google-auth is missing: pip install "
            "google-auth-oauthlib google-api-python-client",
            file=sys.stderr,
        )
        sys.exit(2)
    return youtube_client(), upload_thumbnail


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Render and upload a Red/Blue vote thumbnail.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("render", help="Render a thumbnail PNG locally.")
    sp.add_argument("video_id")
    sp.add_argument(
        "--counts",
        help='Override DB read with explicit "RED,BLUE" counts '
             '(e.g. --counts 247,153). Useful for design previews.',
    )
    sp.add_argument("--out", help="Output PNG path (default: data/redblue/<video>.png).")
    sp.add_argument(
        "--open", action="store_true",
        help="Open the result in the default image viewer (Windows only).",
    )
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("upload", help="Upload the latest rendered thumbnail.")
    sp.add_argument("video_id")
    sp.set_defaults(func=cmd_upload)

    sp = sub.add_parser(
        "update",
        help="Render + upload. Skipped if counts unchanged (use --force to override).",
    )
    sp.add_argument("video_id")
    sp.add_argument(
        "--counts",
        help='Override DB read with explicit "RED,BLUE" counts.',
    )
    sp.add_argument(
        "--force", action="store_true",
        help="Upload even if counts are unchanged since last upload.",
    )
    sp.set_defaults(func=cmd_update)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
