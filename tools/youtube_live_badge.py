"""
YouTube Live-Badge Thumbnail System
====================================
When you go live, slap a "LIVE NOW" badge on your last 8 video
thumbnails. When you go offline, restore the originals. Every old-
video viewer who lands on your channel during a stream sees red and
clicks through.

Architecture
------------
1. List your latest N videos via YouTube Data API.
2. For each video:
   - First time: download the current thumbnail from the YouTube CDN
     and cache it at data/youtube_thumbnails/<video_id>.png
     (this is the "canonical original" we revert to later).
   - Apply the LIVE NOW badge → write to <video_id>_live.png
   - Upload the badged version via youtube.thumbnails.set()
3. Track which videos got badged in data/live_badge_state.json
4. On `revert`: re-upload the cached original for each tracked video,
   then clear state.

The cache means we only download a thumbnail once per video, ever.
Subsequent stream cycles (apply/revert/apply/revert/...) only do
uploads, never downloads.

Subcommands
-----------
    python tools/youtube_live_badge.py apply        # Stream start
    python tools/youtube_live_badge.py revert       # Stream end
    python tools/youtube_live_badge.py status       # Show current state
    python tools/youtube_live_badge.py auth         # Run the one-time
                                                    # OAuth browser flow

First-time setup
----------------
1. Google Cloud Console → enable YouTube Data API v3 → create OAuth
   2.0 Client ID (Desktop app type) → download as
   data/youtube_client_secrets.json
2. pip install google-auth-oauthlib google-api-python-client
3. python tools/youtube_live_badge.py auth   (opens browser, one-time)
4. From now on the apply/revert subcommands just work.
"""

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
import time
import traceback
from io import BytesIO
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Force unbuffered stdout so the .bat wrappers show progress live.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


# ============================================================
# CONFIG / PATHS
# ============================================================

DATA_DIR = REPO_ROOT / "data"
THUMB_CACHE_DIR = DATA_DIR / "youtube_thumbnails"
CLIENT_SECRETS_PATH = DATA_DIR / "youtube_client_secrets.json"
OAUTH_TOKEN_PATH = DATA_DIR / "youtube_oauth.json"
STATE_PATH = DATA_DIR / "live_badge_state.json"

# Optional Twitch logo asset. If this file exists at assets/twitch_logo.png,
# the badge composition uses it as the glyph instead of drawing one
# programmatically. Transparent background expected.
LOGO_PATH = REPO_ROOT / "assets" / "twitch_logo.png"

# YouTube OAuth scope: just enough to upload thumbnails.
# https://developers.google.com/youtube/v3/guides/auth/installed-apps
OAUTH_SCOPES = ["https://www.googleapis.com/auth/youtube"]

# Defaults — override via CLI flags
DEFAULT_VIDEO_COUNT = 8
DEFAULT_BADGE_TEXT = "LIVE"
DEFAULT_BADGE_CORNER = "top_right"
DEFAULT_OFFSET_X = 280    # px inset from the corner — places the badge left of
                          # the vs-god icon (which sits at x=1100 in the 1v1 preset)
DEFAULT_OFFSET_Y = 30      # px down from the top edge so the badge clears headlines
BADGE_HEIGHT_RATIO = 0.13  # fraction of canvas shorter-side

# Twitch brand colors
BADGE_BG_COLOR = (145, 70, 255, 255)        # Twitch purple #9146FF
BADGE_HIGHLIGHT_COLOR = (181, 118, 255, 200)  # Lighter purple inner stroke
BADGE_GLOW_COLOR = (145, 70, 255, 200)        # Same purple, lower alpha for glow
BADGE_TEXT_COLOR = (255, 255, 255, 255)       # White
BADGE_GLYPH_COLOR = (255, 255, 255, 255)      # White Twitch glyph
BADGE_TEXT_STROKE = (50, 18, 90, 200)         # Dark purple text outline

# Pulled from core.config so we don't duplicate the channel ID.
try:
    from core import config as bot_config
    YOUTUBE_CHANNEL_ID = getattr(bot_config, "YOUTUBE_CHANNEL_ID", "")
except Exception:
    YOUTUBE_CHANNEL_ID = ""


# ============================================================
# BADGE COMPOSITION (pure Pillow)
# ============================================================

def _draw_twitch_glyph(draw, cx: int, cy: int, size: int, color: tuple) -> None:
    """
    Draw a stylized Twitch chat-bubble glyph centered at (cx, cy).

    Approximates the iconic Twitch logo silhouette:
      - Vertical rounded rectangle as the chat-bubble body
      - Diagonal tail extending from the bottom-left, going down-and-left
      - Two thin vertical "T-tick" cutouts inside (the inner Twitch marks)

    The cutouts are drawn by punching the badge background color back
    through the glyph — so this function expects the underlying badge
    color to be drawn first. We pass them transparent here and rely on
    drawing order: we punch holes by drawing transparent shapes... wait,
    Pillow doesn't support that directly. Instead we draw the inner
    ticks in the BADGE background color, which renders the same effect
    when this glyph sits on top of the badge.
    """
    body_w = int(size * 0.62)
    body_h = int(size * 0.80)
    tail_h = size - body_h
    radius = max(2, body_w // 14)

    x = cx - body_w // 2
    y = cy - size // 2

    # Main body — slightly rounded vertical rectangle
    draw.rounded_rectangle(
        (x, y, x + body_w, y + body_h),
        radius=radius,
        fill=color,
    )

    # Diagonal tail going down-and-left from the bottom-left corner
    tail_points = [
        (x, y + body_h),                                    # body bottom-left
        (x + int(body_w * 0.45), y + body_h),               # body mid-bottom
        (x, y + body_h + tail_h),                           # tail tip (down-left)
    ]
    draw.polygon(tail_points, fill=color)

    # Two thin "T-tick" rectangles inside (Twitch's inner marks)
    tick_w = max(1, body_w // 8)
    tick_h = int(body_h * 0.42)
    tick_y = y + int(body_h * 0.20)
    tick_gap = int(body_w * 0.18)
    # Position two ticks symmetrically around the body center
    cx_body = x + body_w // 2
    tick1_x = cx_body - tick_gap // 2 - tick_w
    tick2_x = cx_body + tick_gap // 2
    # Punch through with the BADGE background color so the tick reads
    # as a "cut-out" of the white glyph
    draw.rectangle(
        (tick1_x, tick_y, tick1_x + tick_w, tick_y + tick_h),
        fill=BADGE_BG_COLOR,
    )
    draw.rectangle(
        (tick2_x, tick_y, tick2_x + tick_w, tick_y + tick_h),
        fill=BADGE_BG_COLOR,
    )


def apply_live_badge(
    input_path: Path,
    output_path: Path,
    *,
    text: str = DEFAULT_BADGE_TEXT,
    corner: str = DEFAULT_BADGE_CORNER,
    offset_x: int = 0,
    offset_y: int = 0,
) -> None:
    """
    Take an existing thumbnail PNG, paste a LIVE NOW badge in the
    requested corner, and save to output_path. Pure Pillow — no
    network, no preset system, decoupled from build_thumbnail.py.
    """
    from PIL import Image, ImageDraw, ImageFilter

    # Reuse the font loader from build_thumbnail.py so the badge
    # uses the same Big Noodle Titling as the rest of the brand.
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from build_thumbnail import load_font

    img = Image.open(input_path).convert("RGBA")
    canvas_w, canvas_h = img.size

    # Badge dimensions scale with the thumbnail (most YouTube
    # thumbnails are 1280x720, but we don't assume).
    base = min(canvas_w, canvas_h)
    badge_h = max(54, int(base * BADGE_HEIGHT_RATIO))
    pad_x = max(20, int(base * 0.025))                  # outer margin
    text_size = int(badge_h * 0.65)

    # Render the badge layer at 4x and downscale for clean edges.
    SCALE = 4
    bw_target = None  # computed from text width
    bh = badge_h * SCALE

    # Measure the text first
    tmp = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    font = load_font("Big Noodle Titling", text_size * SCALE)
    bbox = d.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    # Inner horizontal padding around text + space for the Twitch glyph
    inner_pad = int(text_size * 0.55) * SCALE
    glyph_size = int(text_size * 0.95) * SCALE     # roughly text-cap-height
    glyph_gap = int(text_size * 0.30) * SCALE
    bw = text_w + inner_pad * 2 + glyph_size + glyph_gap

    # Build the badge on a bigger transparent canvas (room for glow)
    glow_radius = int(badge_h * 0.35) * SCALE
    layer_w = bw + glow_radius * 2
    layer_h = bh + glow_radius * 2
    layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    # Twitch-purple rounded rectangle
    radius = int(badge_h * 0.18) * SCALE
    box_x = glow_radius
    box_y = glow_radius
    ld.rounded_rectangle(
        (box_x, box_y, box_x + bw, box_y + bh),
        radius=radius,
        fill=BADGE_BG_COLOR,
    )

    # Subtle inner highlight stroke
    ld.rounded_rectangle(
        (box_x + 2, box_y + 2, box_x + bw - 2, box_y + bh - 2),
        radius=radius - 2,
        outline=BADGE_HIGHLIGHT_COLOR,
        width=2 * SCALE,
    )

    # Twitch glyph: use the official logo PNG if it exists at repo
    # root, else fall back to the programmatic chat-bubble shape.
    glyph_cx = box_x + inner_pad + glyph_size // 2
    glyph_cy = box_y + bh // 2
    if LOGO_PATH.exists():
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            # Fit logo into a `glyph_size x glyph_size` box, preserving aspect
            scale = min(glyph_size / logo.width, glyph_size / logo.height)
            new_w = max(1, int(logo.width * scale))
            new_h = max(1, int(logo.height * scale))
            logo_resized = logo.resize((new_w, new_h), Image.LANCZOS)
            paste_x = glyph_cx - new_w // 2
            paste_y = glyph_cy - new_h // 2
            layer.alpha_composite(logo_resized, (paste_x, paste_y))
        except Exception as exc:
            print(f"  [warn] Failed to load {LOGO_PATH.name} ({exc}); falling back to programmatic glyph")
            _draw_twitch_glyph(ld, glyph_cx, glyph_cy, glyph_size, BADGE_GLYPH_COLOR)
    else:
        _draw_twitch_glyph(ld, glyph_cx, glyph_cy, glyph_size, BADGE_GLYPH_COLOR)

    # Text in white with subtle dark-purple stroke for legibility
    text_x = box_x + inner_pad + glyph_size + glyph_gap
    text_y = box_y + (bh - text_h) // 2 - bbox[1]
    ld.text(
        (text_x, text_y),
        text,
        font=font,
        fill=BADGE_TEXT_COLOR,
        stroke_width=2 * SCALE,
        stroke_fill=BADGE_TEXT_STROKE,
    )

    # Outer glow — separate purple blur layer composited under the badge
    glow_layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.rounded_rectangle(
        (box_x, box_y, box_x + bw, box_y + bh),
        radius=radius,
        fill=BADGE_GLOW_COLOR,
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_radius // 2))

    composed = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    composed = Image.alpha_composite(composed, glow_layer)
    composed = Image.alpha_composite(composed, layer)

    # Downscale to final size
    final_w = composed.size[0] // SCALE
    final_h = composed.size[1] // SCALE
    composed = composed.resize((final_w, final_h), Image.LANCZOS)

    # Place in chosen corner with margin. offset_x and offset_y push the
    # badge INWARD (toward the canvas center) by that many pixels — useful
    # for placing the badge next to other UI elements without overlapping.
    margin = pad_x
    if corner == "top_right":
        place_x = canvas_w - final_w + glow_radius // SCALE - margin - offset_x
        place_y = margin - glow_radius // SCALE + offset_y
    elif corner == "top_left":
        place_x = margin - glow_radius // SCALE + offset_x
        place_y = margin - glow_radius // SCALE + offset_y
    elif corner == "bottom_right":
        place_x = canvas_w - final_w + glow_radius // SCALE - margin - offset_x
        place_y = canvas_h - final_h + glow_radius // SCALE - margin - offset_y
    elif corner == "bottom_left":
        place_x = margin - glow_radius // SCALE + offset_x
        place_y = canvas_h - final_h + glow_radius // SCALE - margin - offset_y
    else:
        place_x = canvas_w - final_w + glow_radius // SCALE - margin - offset_x
        place_y = margin - glow_radius // SCALE + offset_y

    img.alpha_composite(composed, (place_x, place_y))
    img.convert("RGB").save(output_path, "PNG", optimize=True)


# ============================================================
# YOUTUBE OAUTH (one-time browser flow)
# ============================================================

def _check_oauth_libs():
    """Import oauth libs lazily so subcommands that don't need them work."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        return InstalledAppFlow, Request, Credentials, build, MediaFileUpload
    except ImportError as exc:
        print(f"[!] Required libraries not installed: {exc}", file=sys.stderr)
        print("[!] Run: pip install google-auth-oauthlib google-api-python-client",
              file=sys.stderr)
        sys.exit(2)


def get_oauth_credentials(*, force_refresh: bool = False):
    """
    Return a valid Credentials object. First call opens a browser
    for user consent; subsequent calls just refresh the access token.
    """
    InstalledAppFlow, Request, Credentials, _build, _media = _check_oauth_libs()

    creds = None
    if OAUTH_TOKEN_PATH.exists() and not force_refresh:
        try:
            creds = Credentials.from_authorized_user_file(
                str(OAUTH_TOKEN_PATH), OAUTH_SCOPES
            )
        except Exception as exc:
            print(f"[!] Could not load saved token, will re-auth: {exc}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                print(f"[!] Refresh failed, re-running browser flow: {exc}")
                creds = None
        if not creds:
            if not CLIENT_SECRETS_PATH.exists():
                print(f"[!] Missing client secrets at {CLIENT_SECRETS_PATH}")
                print(f"[!] Download from Google Cloud Console (OAuth 2.0 "
                      f"Client ID, Desktop type) and save it there.")
                sys.exit(2)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRETS_PATH), OAUTH_SCOPES
            )
            creds = flow.run_local_server(port=0)
        OAUTH_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        OAUTH_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        print(f"[i] OAuth token saved: {OAUTH_TOKEN_PATH}")
    return creds


def youtube_client():
    _, _, _, build, _ = _check_oauth_libs()
    creds = get_oauth_credentials()
    return build("youtube", "v3", credentials=creds)


# ============================================================
# YOUTUBE API HELPERS
# ============================================================

def list_recent_videos(yt, channel_id: str, count: int = DEFAULT_VIDEO_COUNT):
    """Return [{video_id, title, thumbnail_url}, ...] for the last `count` uploads."""
    if not channel_id:
        raise SystemExit("YOUTUBE_CHANNEL_ID is empty in core.config — set it first.")

    # search.list is the simplest path. Costs 100 quota.
    resp = yt.search().list(
        part="id,snippet",
        channelId=channel_id,
        order="date",
        maxResults=count,
        type="video",
    ).execute()

    videos = []
    for item in resp.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if not vid:
            continue
        snippet = item.get("snippet", {})
        thumbs = snippet.get("thumbnails", {})
        # Prefer maxres, fallback through standard sizes
        thumb_url = (
            thumbs.get("maxres", {}).get("url")
            or thumbs.get("standard", {}).get("url")
            or thumbs.get("high", {}).get("url")
            or thumbs.get("medium", {}).get("url")
        )
        videos.append({
            "id": vid,
            "title": snippet.get("title", ""),
            "thumbnail_url": thumb_url,
        })
    return videos


def download_thumbnail(url: str, dest_path: Path) -> None:
    """Download from YouTube's CDN — no API quota cost."""
    import urllib.request
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    with open(dest_path, "wb") as f:
        f.write(data)


def upload_thumbnail(yt, video_id: str, png_path: Path) -> None:
    """Upload a new thumbnail. Costs 50 quota units per call."""
    _, _, _, _, MediaFileUpload = _check_oauth_libs()
    media = MediaFileUpload(str(png_path), mimetype="image/png")
    yt.thumbnails().set(videoId=video_id, media_body=media).execute()


# ============================================================
# STATE TRACKING
# ============================================================

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"applied_at": None, "videos": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"applied_at": None, "videos": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ============================================================
# SUBCOMMANDS
# ============================================================

def cmd_auth(args):
    """Run the OAuth browser flow once."""
    creds = get_oauth_credentials(force_refresh=args.force)
    print(f"[i] OAuth OK. Token at {OAUTH_TOKEN_PATH}")
    yt = youtube_client()
    # Sanity probe: fetch our own channel info
    me = yt.channels().list(part="snippet", mine=True).execute()
    items = me.get("items", [])
    if items:
        snippet = items[0].get("snippet", {})
        print(f"[i] Authenticated as: {snippet.get('title', '?')}")


def cmd_apply(args):
    """Badge the last N video thumbnails."""
    yt = youtube_client()
    THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    videos = list_recent_videos(yt, YOUTUBE_CHANNEL_ID, count=args.count)
    if not videos:
        print("[!] No videos found.")
        return 1

    print(f"[i] Badging last {len(videos)} videos...")
    state_videos = []
    for v in videos:
        vid = v["id"]
        title = v["title"][:60]
        original_path = THUMB_CACHE_DIR / f"{vid}.png"
        live_path = THUMB_CACHE_DIR / f"{vid}_live.png"

        # Cache the original if we don't already have it
        if not original_path.exists():
            if not v["thumbnail_url"]:
                print(f"  [skip] {vid} ({title}) — no thumbnail URL")
                continue
            print(f"  [dl]   {vid} ({title}) — caching original...")
            download_thumbnail(v["thumbnail_url"], original_path)

        # Generate the live-badged version
        try:
            apply_live_badge(
                original_path, live_path,
                text=args.text, corner=args.corner,
                offset_x=args.offset_x, offset_y=args.offset_y,
            )
        except Exception as exc:
            print(f"  [warn] badge failed for {vid}: {exc}")
            continue

        # Upload
        try:
            upload_thumbnail(yt, vid, live_path)
        except Exception as exc:
            print(f"  [fail] upload failed for {vid}: {exc}")
            continue

        print(f"  [OK]   {vid} ({title}) — LIVE badge applied")
        state_videos.append({
            "id": vid,
            "title": v["title"],
            "original": str(original_path),
        })

    save_state({
        "applied_at": _dt.datetime.now().isoformat(),
        "videos": state_videos,
    })
    print(f"\nDone. Badged {len(state_videos)} videos. State saved.")
    return 0


def cmd_revert(args):
    """Restore originals for everything we previously badged."""
    state = load_state()
    videos = state.get("videos", [])
    if not videos:
        print("[i] Nothing to revert (no state file or empty).")
        return 0

    yt = youtube_client()
    print(f"[i] Reverting {len(videos)} videos...")
    failures = 0
    for v in videos:
        vid = v["id"]
        title = (v.get("title") or "")[:60]
        original = Path(v["original"])
        if not original.exists():
            print(f"  [warn] {vid} ({title}) — cached original missing, can't revert")
            failures += 1
            continue
        try:
            upload_thumbnail(yt, vid, original)
            print(f"  [OK]   {vid} ({title}) - original restored")
        except Exception as exc:
            print(f"  [fail] {vid} ({title}) - {exc}")
            failures += 1

    if failures == 0:
        save_state({"applied_at": None, "videos": []})
        print("\nDone. State cleared.")
    else:
        print(f"\nDone with {failures} failure(s). State NOT cleared so you can retry.")
    return 0 if failures == 0 else 1


def cmd_preview(args):
    """
    Render a badged version of an input thumbnail to a local file —
    NO YouTube API calls. Useful for iterating on badge look without
    risking real videos.
    """
    src = Path(args.input)
    if not src.exists():
        print(f"[!] Input file not found: {src}")
        return 1
    if args.output:
        out = Path(args.output)
    else:
        out = src.with_name(src.stem + "_preview" + src.suffix)
    out.parent.mkdir(parents=True, exist_ok=True)
    apply_live_badge(
        src, out,
        text=args.text, corner=args.corner,
        offset_x=args.offset_x, offset_y=args.offset_y,
    )
    print(f"[OK] Wrote {out}")
    print(f"     Open it in your image viewer to see how the badge looks.")
    print(f"     Iterate by re-running with --text / --corner flags.")
    return 0


def cmd_status(args):
    state = load_state()
    videos = state.get("videos", [])
    if not videos:
        print("No live badges currently applied. (No state file or empty.)")
        return 0
    print(f"Live badges currently applied to {len(videos)} videos:")
    print(f"  Applied at: {state.get('applied_at', '?')}")
    print(f"  Cache dir:  {THUMB_CACHE_DIR}")
    for v in videos:
        title = (v.get("title") or "")[:60]
        print(f"  - {v['id']}  {title}")
    return 0


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Apply / revert LIVE NOW badge on recent YouTube thumbnails.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_apply = sub.add_parser("apply", help="Badge last N videos (stream start)")
    p_apply.add_argument("--count", type=int, default=DEFAULT_VIDEO_COUNT)
    p_apply.add_argument("--text", default=DEFAULT_BADGE_TEXT)
    p_apply.add_argument("--corner", default=DEFAULT_BADGE_CORNER,
                         choices=["top_right", "top_left", "bottom_right", "bottom_left"])
    p_apply.add_argument("--offset-x", type=int, default=DEFAULT_OFFSET_X,
                         help=f"Shift badge inward from corner (default: {DEFAULT_OFFSET_X})")
    p_apply.add_argument("--offset-y", type=int, default=DEFAULT_OFFSET_Y,
                         help=f"Shift badge vertically from corner (default: {DEFAULT_OFFSET_Y})")
    p_apply.set_defaults(func=cmd_apply)

    p_revert = sub.add_parser("revert", help="Restore originals (stream end)")
    p_revert.set_defaults(func=cmd_revert)

    p_status = sub.add_parser("status", help="Show currently-badged videos")
    p_status.set_defaults(func=cmd_status)

    p_preview = sub.add_parser(
        "preview",
        help="Render a badged thumbnail to a local file (no API calls)",
    )
    p_preview.add_argument("input", help="Input PNG path")
    p_preview.add_argument("output", nargs="?",
                           help="Output PNG path (default: <input>_preview.png)")
    p_preview.add_argument("--text", default=DEFAULT_BADGE_TEXT,
                           help=f"Badge text (default: '{DEFAULT_BADGE_TEXT}')")
    p_preview.add_argument("--corner", default=DEFAULT_BADGE_CORNER,
                           choices=["top_right", "top_left", "bottom_right", "bottom_left"],
                           help=f"Badge corner (default: {DEFAULT_BADGE_CORNER})")
    p_preview.add_argument("--offset-x", type=int, default=DEFAULT_OFFSET_X,
                           help=f"Shift badge inward (toward center) by this many px (default: {DEFAULT_OFFSET_X})")
    p_preview.add_argument("--offset-y", type=int, default=DEFAULT_OFFSET_Y,
                           help=f"Shift badge vertically from corner (default: {DEFAULT_OFFSET_Y})")
    p_preview.set_defaults(func=cmd_preview)

    p_auth = sub.add_parser("auth", help="Run the one-time OAuth browser flow")
    p_auth.add_argument("--force", action="store_true")
    p_auth.set_defaults(func=cmd_auth)

    args = parser.parse_args()
    try:
        rc = args.func(args)
        sys.exit(rc or 0)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[!] Unhandled error: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
