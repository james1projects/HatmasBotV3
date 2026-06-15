"""
build_site_meta.py — generate hatmaster.tv social card + favicons
=================================================================

Produces the static brand assets the public pages reference:

    public/og-image.png          1200x630 social share card (Discord,
                                 Bluesky, Twitter link previews)
    public/favicon.ico           multi-size .ico (16/32/48)
    public/favicon-32.png        modern browsers
    public/apple-touch-icon.png  180x180 iOS home screen

The og-image is composed from brand assets already in the repo: the
hat icon (overlays/hat.png), god card splash art (data/god_cards/),
and the Hatmas brand palette from public/theme.css (Jet Black, Light
Bronze, Cool Steel, Scarlet Rush).

Run once, commit the outputs, re-run whenever the brand changes:

    python tools/build_site_meta.py
    python tools/build_site_meta.py --gods "Ymir,Loki,Hou Yi"   # pick card art
    python tools/build_site_meta.py --no-cards                  # text-only card

Fonts: tries Big Noodle Titling, then Inter/Impact, then DejaVu —
same fallback philosophy as tools/build_thumbnail.py, so it renders
on any machine (just better on the streaming PC where the brand
fonts are installed).
"""

import argparse
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError:
    print("Missing Pillow. Install with: pip install Pillow")
    sys.exit(1)

PUBLIC = REPO_ROOT / "public"
HAT = REPO_ROOT / "overlays" / "hat.png"
GOD_CARDS = REPO_ROOT / "data" / "god_cards"

# Hatmas brand palette (matches public/theme.css)
JET_BLACK = (32, 44, 57)
JET_DEEP = (21, 30, 40)
COOL_STEEL = (125, 152, 161)
SCARLET = (223, 41, 53)
BRONZE = (223, 160, 110)
WHITE = (255, 255, 255)


def find_font(names, size):
    """Try a list of font filenames across the usual directories."""
    import os
    dirs = [
        Path("C:/Windows/Fonts"),
        Path(os.path.expandvars("%LOCALAPPDATA%"))
        / "Microsoft" / "Windows" / "Fonts",
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype"),
    ]
    for name in names:
        for d in dirs:
            p = d / name
            try:
                if p.exists():
                    return ImageFont.truetype(str(p), size)
            except Exception:
                continue
        # Also let PIL search by family name
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def display_font(size):
    return find_font(
        ["BigNoodleTitling.ttf", "big_noodle_titling.ttf",
         "Impact.ttf", "impact.ttf", "Inter-Bold.ttf",
         "DejaVuSans-Bold.ttf"], size)


def mono_font(size):
    return find_font(
        ["JetBrainsMono-Regular.ttf", "consola.ttf",
         "DejaVuSansMono.ttf"], size)


def build_og_image(god_names, use_cards=True):
    W, H = 1200, 630
    im = Image.new("RGB", (W, H), JET_BLACK)
    dr = ImageDraw.Draw(im)

    # Vertical gradient: Jet Black -> deeper tint (mirrors the site bg)
    for y in range(H):
        t = y / H
        c = tuple(int(JET_BLACK[i] + (JET_DEEP[i] - JET_BLACK[i]) * t)
                  for i in range(3))
        dr.line([(0, y), (W, y)], fill=c)

    # Right side: god card art collage, feathered into the background.
    if use_cards and GOD_CARDS.exists():
        slugs = []
        for n in god_names:
            slug = n.lower().replace(" ", "-").replace("'", "")
            p = GOD_CARDS / f"{slug}.png"
            if p.exists():
                slugs.append(p)
        if not slugs:
            pool = sorted(GOD_CARDS.glob("*.png"))
            slugs = random.sample(pool, min(3, len(pool))) if pool else []
        cw, ch = 190, 285
        x = W - cw * len(slugs) - 36
        for p in slugs:
            try:
                card = Image.open(p).convert("RGBA")
                card = card.resize((cw, ch))
                # darken + fade hard so the text side stays dominant
                overlay = Image.new("RGBA", card.size, (21, 30, 40, 185))
                card = Image.alpha_composite(card, overlay)
                mask = card.getchannel("A").point(lambda a: int(a * 0.85))
                im.paste(card, (x, H - ch - 70), mask)
                x += cw
            except Exception as e:
                print(f"  card skip {p.name}: {e}")

    dr = ImageDraw.Draw(im)

    # Fake ticker strip along the top — the site's signature element.
    dr.rectangle([0, 0, W, 54], fill=JET_DEEP)
    dr.rectangle([0, 54, W, 56], fill=BRONZE)
    tick_font = mono_font(22)
    ticks = [("YMIR", "287", True), ("LOKI", "164", False),
             ("HOU YI", "305", True), ("ATLAS", "198", True),
             ("GEB", "121", False), ("SYLVANUS", "342", True),
             ("ARES", "176", True)]
    tx = 28
    for name, px, up in ticks:
        dr.text((tx, 15), name, font=tick_font, fill=WHITE)
        tx += dr.textlength(name, font=tick_font) + 14
        arrow = "+" if up else "-"
        dr.text((tx, 15), arrow + px, font=tick_font,
                fill=BRONZE if up else SCARLET)
        tx += dr.textlength(arrow + px, font=tick_font) + 44
        if tx > W - 150:
            break

    # Hat icon
    if HAT.exists():
        hat = Image.open(HAT).convert("RGBA").resize((120, 120))
        im.paste(hat, (64, 130), hat)

    # Headline block — auto-fit the title so it never collides with
    # the card collage regardless of which font family was found.
    dr = ImageDraw.Draw(im)
    kicker_f = mono_font(26)
    sub_f = mono_font(30)
    url_f = mono_font(34)

    max_title_w = 580
    size = 150
    title_f = display_font(size)
    while size > 56 and dr.textlength("HATMAS MARKET",
                                      font=title_f) > max_title_w:
        size -= 6
        title_f = display_font(size)

    dr.text((68, 272), "// A SMITE 2 STOCK EXCHANGE",
            font=kicker_f, fill=COOL_STEEL)
    title_y = 308
    dr.text((62, title_y), "HATMAS", font=title_f, fill=WHITE)
    tw = dr.textlength("HATMAS ", font=title_f)
    dr.text((62 + tw, title_y), "MARKET", font=title_f, fill=BRONZE)
    bottom = dr.textbbox((62, title_y), "HATMAS", font=title_f)[3]
    dr.text((68, bottom + 26), "LIVE GOD PRICES - VIEWER PORTFOLIOS",
            font=sub_f, fill=WHITE)
    dr.text((68, bottom + 66), "WATCH. EARN SHARES. TRADE.",
            font=sub_f, fill=COOL_STEEL)

    # Bottom-left URL chip
    dr.rectangle([64, 568, 64 + dr.textlength("HATMASTER.TV",
                  font=url_f) + 28, 612], fill=BRONZE)
    dr.text((78, 572), "HATMASTER.TV", font=url_f, fill=JET_DEEP)

    out = PUBLIC / "og-image.png"
    im.save(out, "PNG")
    print(f"  wrote {out} ({W}x{H})")


def build_favicons():
    if not HAT.exists():
        print(f"  {HAT} missing — favicons skipped")
        return
    hat = Image.open(HAT).convert("RGBA")

    # Drop the hat on a Jet Black rounded square so it reads at 16px.
    def tile(size):
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        dr = ImageDraw.Draw(canvas)
        r = max(2, size // 8)
        dr.rounded_rectangle([0, 0, size - 1, size - 1],
                             radius=r, fill=JET_BLACK + (255,))
        pad = max(1, size // 10)
        inner = hat.resize((size - pad * 2, size - pad * 2))
        canvas.paste(inner, (pad, pad), inner)
        return canvas

    png32 = tile(32)
    png32.save(PUBLIC / "favicon-32.png", "PNG")
    tile(180).save(PUBLIC / "apple-touch-icon.png", "PNG")
    tile(48).save(PUBLIC / "favicon.ico", sizes=[(16, 16), (32, 32),
                                                 (48, 48)])
    print(f"  wrote {PUBLIC / 'favicon-32.png'}")
    print(f"  wrote {PUBLIC / 'apple-touch-icon.png'}")
    print(f"  wrote {PUBLIC / 'favicon.ico'} (16/32/48)")


def main():
    parser = argparse.ArgumentParser(
        description="Generate og-image.png + favicons for hatmaster.tv")
    parser.add_argument("--gods", default="Ymir,Sylvanus,Atlas",
                        help="comma-separated god card art for the "
                             "og-image collage")
    parser.add_argument("--no-cards", action="store_true",
                        help="text-only og-image (skip god card art)")
    args = parser.parse_args()

    gods = [g.strip() for g in args.gods.split(",") if g.strip()]
    print("Building og-image...")
    build_og_image(gods, use_cards=not args.no_cards)
    print("Building favicons...")
    build_favicons()
    print("Done. Restart the bot (or just refresh) - the routes serve "
          "these from public/.")


if __name__ == "__main__":
    main()
