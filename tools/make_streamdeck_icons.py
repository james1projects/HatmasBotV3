"""
tools/make_streamdeck_icons.py — generate Stream Deck button icons.

Renders 288x288 PNG icons (Stream Deck @3x key size) into
assets/streamdeck_icons/, one per HatmasBot button, using the
hatmas_theme.css palette: Jet Black background, a phase-colored
top bar (Cool Steel = pre-stream, Scarlet = live, Bronze = end of
stream, muted steel = utility), and a bold condensed label baked
into the image so the Stream Deck Title field can stay empty.

Usage:
    py tools/make_streamdeck_icons.py

Re-run any time; overwrites in place. Add new buttons to ICONS.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "assets" / "streamdeck_icons"

SIZE = 288
BAR_H = 24
PAD = 20

# hatmas_theme.css palette
BG = (32, 44, 57)          # --hm-bg Jet Black
TEXT = (255, 255, 255)     # --hm-text
BORDER = (66, 65, 67)      # bronze @18% pre-blended over BG

PHASE_COLORS = {
    "pre": (125, 152, 161),   # --hm-blue Cool Steel
    "live": (223, 41, 53),    # --hm-red Scarlet Rush
    "end": (223, 160, 110),   # --hm-gold Light Bronze
    "util": (143, 166, 175),  # --hm-text-muted
}

# filename -> (label lines, phase)
ICONS = {
    "check_ready": (["CHECK", "READY"], "pre"),
    "bot_restart": (["BOT", "RESTART"], "pre"),
    "dashboard": (["DASH", "BOARD"], "pre"),
    "launch_stack": (["OBS +", "MIXITUP"], "pre"),
    "factorio": (["START", "FACTORIO"], "pre"),
    "go_live": (["GO", "LIVE"], "live"),
    "go_offline": (["GO", "OFFLINE"], "end"),
    "process_recordings": (["PROCESS", "RECS"], "end"),
    "sort_unknowns": (["SORT", "UNKNOWNS"], "end"),
    "thumbnail_studio": (["THUMB", "STUDIO"], "end"),
    "resolve_import": (["RESOLVE", "IMPORT"], "end"),
    "resolve_tiktok": (["RESOLVE", "TIKTOK"], "end"),
    "bot_stop": (["BOT", "STOP"], "util"),
    "discord_test": (["DISCORD", "TEST"], "util"),
    "earpiece": (["EAR", "PIECE"], "util"),
    "vod_review": (["VOD", "REVIEW"], "end"),
    "vod_search": (["VOD", "SEARCH"], "end"),
}


def load_font(px: int) -> ImageFont.FreeTypeFont:
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/bahnschrift.ttf", px)
        try:
            font.set_variation_by_name("SemiBold Condensed")
        except Exception:
            pass
        return font
    except OSError:
        return ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", px)


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_px: int) -> ImageFont.FreeTypeFont:
    px = start_px
    while px > 20:
        font = load_font(px)
        if draw.textlength(text, font=font) <= max_width:
            return font
        px -= 4
    return load_font(20)


def render(name: str, lines: list[str], phase: str) -> None:
    accent = PHASE_COLORS[phase]
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, SIZE - 1, BAR_H - 1], fill=accent)
    draw.rectangle([0, 0, SIZE - 1, SIZE - 1], outline=BORDER, width=3)

    max_width = SIZE - 2 * PAD
    longest = max(lines, key=len)
    font = fit_font(draw, longest, max_width, 72)

    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    gap = 6
    block_h = line_h * len(lines) + gap * (len(lines) - 1)
    y = BAR_H + (SIZE - BAR_H - block_h) // 2

    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((SIZE - w) // 2, y), line, font=font, fill=TEXT)
        y += line_h + gap

    out = OUT_DIR / f"{name}.png"
    img.save(out)
    print(f"wrote {out.relative_to(REPO_ROOT)}")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, (lines, phase) in ICONS.items():
        render(name, lines, phase)
    print(f"\n{len(ICONS)} icons in {OUT_DIR}")


if __name__ == "__main__":
    main()
