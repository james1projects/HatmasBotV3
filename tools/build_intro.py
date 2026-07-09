"""
Intro Overlay Builder
=====================
Generates a transparent 1920x1080 PNG with a slim top-center strip
showing the played god's career stats — the companion to
build_outro.py for the START of a full gameplay upload. Overlay it on
the gameplay track in DaVinci for the first ~10 seconds (add a fade),
then let it drop out.

Strip contents: god icon, god name, KILLS / WINS / LOSSES / WIN RATE.
No share price by design — the intro is about the player, the outro
does the market pitch.

Everything outside the strip is fully transparent; style matches
public/theme.css via the shared palette/fonts in build_outro.py.

Usage:
    python tools/build_intro.py                   # god = last match played
    python tools/build_intro.py --god "Hou Yi"
    python tools/build_intro.py --over frame.png  # legibility check: also
                                                  #   writes *_preview.png
                                                  #   composited on a frame
"""

import sys
import argparse
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from build_thumbnail import resolve_god_icon, slugify  # noqa: E402
from build_outro import (  # noqa: E402
    DB_PATH, OUT_DIR, CANVAS,
    PANEL, BORDER, BORDER_HI, ACCENT, RED, TEXT, TEXT_DIM,
    bebas, inter, mono, draw_tracked,
    fetch_last_played_god, fetch_god_stats,
)

STRIP_TOP = 140       # px from the top edge — clears SMITE 2's scoreboard
                      # / timer / portrait row (~top 120px at 1080p)
STRIP_H = 110
PANEL_ALPHA = 235     # slight translucency so the strip reads as overlay
PAD = 28              # inner horizontal padding
GAP = 44              # gap between stat blocks


def render(stats, icon_path, strip_top=STRIP_TOP):
    img = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    name = stats["name"].upper()
    name_font = bebas(64)
    label_font = inter(19, "SemiBold")
    value_font = mono(44, "Bold")

    blocks = [
        ("KILLS", f"{stats['kills']:,}", TEXT),
        ("WINS", f"{stats['wins']:,}", TEXT),
        ("LOSSES", f"{stats['losses']:,}", TEXT),
    ]
    wr = stats["win_rate"]
    if wr is not None:
        blocks.append(("WIN RATE", f"{wr:.1f}%", ACCENT if wr >= 50 else RED))
    else:
        blocks.append(("WIN RATE", "--", TEXT_DIM))

    # ---- measure, then center the strip --------------------------------
    icon_size = STRIP_H - 24
    name_w = draw.textlength(name, font=name_font)

    def block_w(label, value):
        lw = sum(draw.textlength(ch, font=label_font) + 3 for ch in label) - 3
        vw = draw.textlength(value, font=value_font)
        return max(lw, vw)

    widths = [block_w(l, v) for l, v, _ in blocks]
    divider_w = 2
    content_w = (icon_size + 20 + name_w + PAD + divider_w + PAD
                 + sum(widths) + GAP * (len(blocks) - 1))
    strip_w = int(content_w + PAD * 2)
    x0 = (CANVAS[0] - strip_w) // 2
    x1 = x0 + strip_w
    y0, y1 = strip_top, strip_top + STRIP_H

    draw.rounded_rectangle((x0, y0, x1, y1), radius=12,
                           fill=(*PANEL, PANEL_ALPHA), outline=BORDER, width=1)
    # bronze baseline along the strip's bottom edge — the site's h2 rule
    draw.rectangle((x0 + 14, y1 - 4, x1 - 14, y1 - 2), fill=ACCENT)

    # ---- contents -------------------------------------------------------
    cx = x0 + PAD
    cy = (y0 + y1) // 2

    if icon_path and Path(icon_path).exists():
        icon = Image.open(icon_path).convert("RGBA")
        icon = icon.resize((icon_size, icon_size))
        img.paste(icon, (int(cx), cy - icon_size // 2), icon)
        draw.rectangle((int(cx), cy - icon_size // 2,
                        int(cx) + icon_size, cy + icon_size - icon_size // 2),
                       outline=BORDER, width=1)
    cx += icon_size + 20

    draw.text((cx, cy), name, font=name_font, fill=TEXT, anchor="lm")
    cx += name_w + PAD

    draw.rectangle((cx, y0 + 24, cx + divider_w, y1 - 24), fill=BORDER_HI)
    cx += divider_w + PAD

    for (label, value, color), w in zip(blocks, widths):
        draw_tracked(draw, (cx, y0 + 18), label, label_font,
                     TEXT_DIM, tracking=3)
        draw.text((cx, y1 - 20), value, font=value_font, fill=color,
                  anchor="ls")
        cx += w + GAP

    return img


def main():
    ap = argparse.ArgumentParser(description="Build the intro overlay PNG.")
    ap.add_argument("--god", help="God name (default: last match played)")
    ap.add_argument("--out", help="Output path (default rendered/intro_<god>.png)")
    ap.add_argument("--over", help="Gameplay frame to composite a *_preview.png "
                                   "onto, for a legibility check")
    ap.add_argument("--y", type=int, default=STRIP_TOP,
                    help=f"Strip top edge in px (default {STRIP_TOP}, below "
                         f"the SMITE scoreboard; use ~26 to hug the frame top)")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        god = args.god or fetch_last_played_god(con)
        if not god:
            sys.exit("No --god given and no matches found in processed_matches.")
        stats = fetch_god_stats(con, god)
        if stats is None:
            sys.exit(f"'{god}' not found in god_prices. Check the spelling "
                     f"against core/god_roster.py names.")
    finally:
        con.close()

    icon_path = resolve_god_icon(stats["name"])

    out = Path(args.out) if args.out else OUT_DIR / f"intro_{slugify(stats['name'])}.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    img = render(stats, icon_path, strip_top=args.y)
    img.save(out)
    print(f"[ok] {stats['name']}: K {stats['kills']}  W {stats['wins']}  "
          f"L {stats['losses']}")
    print(f"[ok] wrote {out}")

    if args.over:
        frame = Image.open(args.over).convert("RGBA").resize(CANVAS)
        preview = Image.alpha_composite(frame, img).convert("RGB")
        pv_path = out.with_name(out.stem + "_preview.png")
        preview.save(pv_path)
        print(f"[ok] wrote {pv_path}")


if __name__ == "__main__":
    main()
