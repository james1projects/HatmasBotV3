"""
Outro Card Builder
==================
Generates the 1920x1080 end-of-video "outro page" PNG for full gameplay
uploads: the played god's card + per-god career stats (kills, wins,
losses, win rate) and current share price on the left, with reserved
blank regions on the right for YouTube end-screen elements (one 16:9
video link box + one circular subscribe button slot). Drop the PNG at
the end of the DaVinci timeline, hold it ~10-20s, then position the
YouTube end-screen elements over the reserved boxes in YouTube Studio.

Visual style matches the hatmaster.tv theme (public/theme.css): Bebas
Neue display, Inter UI, JetBrains Mono numbers, flat panels, bronze
accent. Fonts ship in assets/fonts/ (OFL-licensed).

Data comes read-only from data/economy.db:
    god_prices        -> price, total_wins/losses/kills per god
    processed_matches -> most recently played god (default when --god
                         is omitted)

Card art resolution is shared with build_thumbnail.py, so files in
"Custom God Cards/" override the auto-downloaded base art in
data/god_cards/ exactly like thumbnails do.

Usage:
    python tools/build_outro.py                     # god = last match played
    python tools/build_outro.py --god "Hou Yi"
    python tools/build_outro.py --god Sylvanus --skin HighNoon
    python tools/build_outro.py --bg-art            # blurred card as background
    python tools/build_outro.py --out my_outro.png
"""

import sys
import argparse
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from build_thumbnail import resolve_god_card, slugify  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "economy.db"
FONTS_DIR = REPO_ROOT / "assets" / "fonts"
OUT_DIR = REPO_ROOT / "rendered"

CANVAS = (1920, 1080)

# hatmaster.tv palette (public/theme.css)
BG = (0x20, 0x2C, 0x39)          # --bg      Jet Black
BG_BOTTOM = (0x15, 0x1E, 0x28)   # page gradient bottom stop
PANEL = (0x2A, 0x36, 0x45)       # --panel
PANEL_HI = (0x34, 0x42, 0x52)    # --panel-hi
BORDER = (125, 152, 161, 56)     # --border  Cool Steel @ 0.22
BORDER_HI = (0x7D, 0x98, 0xA1)   # --border-hi
ACCENT = (0xDF, 0xA0, 0x6E)      # --accent  Light Bronze (also "gain")
RED = (0xFF, 0x6B, 0x73)         # --red     loss
TEXT = (0xFF, 0xFF, 0xFF)        # --text
TEXT_DIM = (0x9F, 0xB4, 0xBB)    # --text-dim
TEXT_FADE = (159, 180, 187, 199) # --text-fade @ 0.78


def _font(path, size, weight=None):
    f = ImageFont.truetype(str(path), size)
    if weight:
        try:
            f.set_variation_by_name(weight)
        except Exception:
            pass
    return f


def bebas(size):
    return _font(FONTS_DIR / "BebasNeue-Regular.ttf", size)


def inter(size, weight="Regular"):
    return _font(FONTS_DIR / "Inter-Variable.ttf", size, weight)


def mono(size, weight="Regular"):
    return _font(FONTS_DIR / "JetBrainsMono-Variable.ttf", size, weight)


def draw_tracked(draw, pos, text, font, fill, tracking=0, anchor="left"):
    """Draw text with per-character letter spacing (px). Returns end x."""
    x, y = pos
    total = sum(draw.textlength(ch, font=font) + tracking for ch in text) - tracking
    if anchor == "right":
        x -= total
    elif anchor == "center":
        x -= total / 2
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x


def panel(draw, box, fill=PANEL, outline=BORDER, width=1, radius=10):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


# ---------------------------------------------------------------- data

def fetch_last_played_god(con):
    row = con.execute(
        """SELECT god_name FROM processed_matches
           WHERE god_name NOT LIKE '<%'
           ORDER BY COALESCE(played_at, processed_at) DESC LIMIT 1"""
    ).fetchone()
    return row[0] if row else None


def fetch_god_stats(con, god_name):
    """Case-insensitive lookup in god_prices; returns dict or None."""
    row = con.execute(
        """SELECT god_name, price, total_wins, total_losses, total_kills,
                  games_played
           FROM god_prices WHERE lower(god_name) = lower(?)""",
        (god_name,),
    ).fetchone()
    if row is None:
        return None
    name, price, wins, losses, kills, games = row
    decided = (wins or 0) + (losses or 0)
    return {
        "name": name,
        "price": price or 0.0,
        "wins": wins or 0,
        "losses": losses or 0,
        "kills": kills or 0,
        "games": games or 0,
        "win_rate": (100.0 * wins / decided) if decided else None,
    }


# -------------------------------------------------------------- render

def render(stats, card_path, bg_art=False):
    img = Image.new("RGB", CANVAS, BG)

    # Vertical page gradient, same stops as the website body background.
    grad = Image.new("L", (1, CANVAS[1]))
    grad.putdata([int(255 * y / CANVAS[1]) for y in range(CANVAS[1])])
    img = Image.composite(Image.new("RGB", CANVAS, BG_BOTTOM), img,
                          grad.resize(CANVAS))

    if bg_art:
        art = Image.open(card_path).convert("RGB")
        scale = max(CANVAS[0] / art.width, CANVAS[1] / art.height)
        art = art.resize((int(art.width * scale), int(art.height * scale)))
        art = art.crop((
            (art.width - CANVAS[0]) // 2, (art.height - CANVAS[1]) // 2,
            (art.width - CANVAS[0]) // 2 + CANVAS[0],
            (art.height - CANVAS[1]) // 2 + CANVAS[1],
        )).filter(ImageFilter.GaussianBlur(42))
        img = Image.blend(img, art, 0.18)

    img = img.convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    # ---- header -----------------------------------------------------
    draw.text((80, 38), "THANKS FOR WATCHING", font=bebas(96), fill=TEXT)
    draw_tracked(draw, (1840, 74), "HATMASTER.TV", mono(30, "Medium"),
                 ACCENT, tracking=6, anchor="right")

    top = 180          # content band below the header
    bottom = 1000

    # ---- left: god card panel ---------------------------------------
    card_panel = (80, top, 560, bottom)
    panel(draw, card_panel)

    card = Image.open(card_path).convert("RGBA")
    card = card.resize((400, 600))
    cx = card_panel[0] + (card_panel[2] - card_panel[0] - 400) // 2
    img.paste(card, (cx, top + 40), card)
    draw.rectangle((cx, top + 40, cx + 400, top + 40 + 600),
                   outline=BORDER, width=1)

    name_cx = (card_panel[0] + card_panel[2]) // 2
    draw.text((name_cx, top + 40 + 600 + 20), stats["name"].upper(),
              font=bebas(64), fill=TEXT, anchor="ma")
    # thin bronze rule under the name, same device as site h2 underlines
    draw.rectangle((name_cx - 60, bottom - 62, name_cx + 60, bottom - 58),
                   fill=ACCENT)
    draw_tracked(draw, (name_cx, bottom - 44), f"{stats['games']} GAMES",
                 inter(20, "SemiBold"), TEXT_DIM, tracking=3, anchor="center")

    # ---- middle: stat rows ------------------------------------------
    col_x = 620
    col_w = 320
    rows = [
        ("KILLS", f"{stats['kills']:,}", TEXT),
        ("WINS", f"{stats['wins']:,}", TEXT),
        ("LOSSES", f"{stats['losses']:,}", TEXT),
    ]
    wr = stats["win_rate"]
    if wr is not None:
        wr_color = ACCENT if wr >= 50 else RED
        rows.append(("WIN RATE", f"{wr:.1f}%", wr_color))
    else:
        rows.append(("WIN RATE", "--", TEXT_DIM))

    row_h = 128
    y = top
    for label, value, color in rows:
        draw_tracked(draw, (col_x, y), label, inter(22, "SemiBold"),
                     TEXT_DIM, tracking=4)
        draw.text((col_x, y + 30), value, font=mono(62, "Bold"), fill=color)
        y += row_h

    # share price hero panel at the bottom of the stat column. Priced in
    # hats, not dollars: number first, then the hatmaster.tv hat icon.
    price_font = mono(64, "Bold")
    price_text = f"{stats['price']:,.2f}"
    price_w = draw.textlength(price_text, font=price_font)
    hat = Image.open(REPO_ROOT / "public" / "hat.png").convert("RGBA")
    hat_h = 54
    hat = hat.resize((hat_h, hat_h))
    box_w = max(col_w, int(price_w + 16 + hat_h) + 20)
    price_box = (col_x - 20, bottom - 170, col_x + box_w, bottom)
    panel(draw, price_box, fill=PANEL_HI)
    draw_tracked(draw, (col_x, bottom - 148), "SHARE PRICE",
                 inter(22, "SemiBold"), TEXT_DIM, tracking=4)
    draw.text((col_x, bottom - 116), price_text, font=price_font, fill=ACCENT)
    # center the hat on the digits' vertical midline
    d_top, d_bot = price_font.getbbox("0")[1], price_font.getbbox("0")[3]
    hat_y = int(bottom - 116 + (d_top + d_bot) / 2 - hat_h / 2)
    img.paste(hat, (int(col_x + price_w + 16), hat_y), hat)

    # ---- right: reserved end-screen regions -------------------------
    vid = (990, top, 1840, top + int((1840 - 990) * 9 / 16))  # 850x478
    panel(draw, vid, fill=PANEL, outline=BORDER_HI, width=2, radius=12)
    vcx, vcy = (vid[0] + vid[2]) // 2, (vid[1] + vid[3]) // 2
    draw.text((vcx, vcy - 24), "NEXT VIDEO", font=bebas(56),
              fill=TEXT_FADE, anchor="mm")
    draw.text((vcx, vcy + 28), "end-screen video element goes here",
              font=inter(22), fill=TEXT_FADE, anchor="mm")

    sub_c = (vcx, vid[3] + 30 + 110)
    draw.ellipse((sub_c[0] - 110, sub_c[1] - 110,
                  sub_c[0] + 110, sub_c[1] + 110),
                 fill=PANEL, outline=BORDER_HI, width=2)
    draw.text((sub_c[0], sub_c[1]), "SUBSCRIBE", font=bebas(36),
              fill=TEXT_FADE, anchor="mm")

    return img.convert("RGB")


def main():
    ap = argparse.ArgumentParser(description="Build the outro card PNG.")
    ap.add_argument("--god", help="God name (default: last match played)")
    ap.add_argument("--skin", help="Skin variant passed to the card resolver")
    ap.add_argument("--out", help="Output path (default rendered/outro_<god>.png)")
    ap.add_argument("--bg-art", action="store_true",
                    help="Blend the blurred god card into the background")
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

    card_path = resolve_god_card(stats["name"], skin=args.skin)
    if not card_path or not Path(card_path).exists():
        sys.exit(f"No card art for {stats['name']} — run "
                 f"tools/download_god_cards.py --add \"{stats['name']}\"")

    out = Path(args.out) if args.out else OUT_DIR / f"outro_{slugify(stats['name'])}.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    img = render(stats, card_path, bg_art=args.bg_art)
    img.save(out)
    print(f"[ok] {stats['name']}: K {stats['kills']}  W {stats['wins']}  "
          f"L {stats['losses']}  price {stats['price']:,.2f} hats")
    print(f"[ok] wrote {out}")


if __name__ == "__main__":
    main()
