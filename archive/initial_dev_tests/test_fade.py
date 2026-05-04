"""
Test god portrait fade in/out effect.

Usage:
  python test_fade.py Atlas              — Set god + background, fade in (1s)
  python test_fade.py Atlas order        — Set god + order background, fade in
  python test_fade.py fadeout            — Fade out current portrait + background
  python test_fade.py Atlas fast         — Fade in quickly (0.3s)
  python test_fade.py Atlas slow         — Fade in slowly (2s)
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from core.config import (
    SMITE2_GOD_IMAGES_DIR, SMITE2_GOD_BG_DIR,
    OBS_SOURCE_GOD_IMAGE, OBS_SOURCE_GOD_BG,
    OBS_GOD_IMAGE_SCENE, OBS_GOD_IMAGE_GROUP,
    OBS_WS_HOST, OBS_WS_PORT, OBS_WS_PASSWORD,
)

try:
    import obsws_python as obs
except ImportError:
    print("obsws-python not installed!")
    sys.exit(1)

BG_MAP = {
    "chaos": "bg_chaos_red.png",
    "order": "bg_order_blue.png",
    "carry": "bg_carry_gold.png",
    "support": "bg_support_emerald.png",
    "mid": "bg_mid_blue.png",
    "jungle": "bg_jungle_forest.png",
    "solo": "bg_solo_orange.png",
}

SPEED_MAP = {
    "fast": 0.3,
    "slow": 2.0,
}


def find_god_image(god_name):
    god_dir = Path(SMITE2_GOD_IMAGES_DIR)
    slug = god_name.lower().replace(" ", "-").replace("'", "")
    names = [god_name, god_name.lower(), slug]
    for ext in [".gif", ".png"]:
        for name in names:
            path = god_dir / f"{name}{ext}"
            if path.exists():
                return path
    return None


def connect():
    try:
        client = obs.ReqClient(host=OBS_WS_HOST, port=OBS_WS_PORT, password=OBS_WS_PASSWORD)
        print(f"Connected to OBS")
        return client
    except Exception as e:
        print(f"ERROR: {e}")
        return None


COLOR_FILTER_KINDS = ["color_filter", "color_filter_v4", "color_correction_filter"]

def ensure_fade_filter(client, source_name, filter_name="FadeFilter"):
    """Create a Color Correction filter if it doesn't exist.
    Tries multiple filter kind strings for OBS version compatibility."""
    try:
        filters = client.get_source_filter_list(source_name)
        for f in filters.filters:
            if f.get("filterName") == filter_name:
                return True
        for kind in COLOR_FILTER_KINDS:
            try:
                client.create_source_filter(
                    source_name, filter_name, kind, {"opacity": 0}
                )
                print(f"  Created FadeFilter on '{source_name}' (kind={kind})")
                return True
            except Exception:
                continue
        print(f"  Could not create filter on '{source_name}' — no compatible kind found")
        return False
    except Exception as e:
        print(f"  Could not create filter on '{source_name}': {e}")
        return False


def set_source_visible(client, source_name, visible):
    group = OBS_GOD_IMAGE_GROUP or None
    scene = OBS_GOD_IMAGE_SCENE or None
    try:
        if group:
            item_id = client.get_scene_item_id(group, source_name).scene_item_id
            client.set_scene_item_enabled(group, item_id, visible)
        else:
            if not scene:
                scene = client.get_current_program_scene().scene_name
            item_id = client.get_scene_item_id(scene, source_name).scene_item_id
            client.set_scene_item_enabled(scene, item_id, visible)
    except Exception as e:
        print(f"  Visibility error ({source_name}): {e}")


def fade(client, sources, fade_in=True, duration=1.0, steps=20):
    """Fade sources in or out synchronously."""
    step_delay = duration / steps

    for source in sources:
        ensure_fade_filter(client, source)

    # Set initial opacity (OBS color_filter uses 0-100 integer scale)
    start_opacity = 0 if fade_in else 100
    for source in sources:
        client.set_source_filter_settings(source, "FadeFilter", {"opacity": start_opacity}, overlay=True)

    # If fading in, make sources visible first (at opacity 0)
    if fade_in:
        for source in sources:
            set_source_visible(client, source, True)

    direction = "in" if fade_in else "out"
    print(f"\n  Fading {direction} ({duration}s)...")

    for i in range(steps + 1):
        if fade_in:
            opacity = int((i / steps) * 100)
        else:
            opacity = int((1.0 - (i / steps)) * 100)
        for source in sources:
            client.set_source_filter_settings(source, "FadeFilter", {"opacity": opacity}, overlay=True)
        time.sleep(step_delay)

    # If faded out, hide sources
    if not fade_in:
        for source in sources:
            set_source_visible(client, source, False)

    print(f"  Fade {direction} complete!")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1].lower()
    client = connect()
    if not client:
        return

    sources = [OBS_SOURCE_GOD_IMAGE, OBS_SOURCE_GOD_BG]

    try:
        if action == "fadeout":
            fade(client, sources, fade_in=False, duration=1.0)
            return

        # Parse args: god_name [bg_key|speed]
        all_args = sys.argv[1:]
        bg_key = "chaos"
        duration = 1.0
        god_parts = []

        for arg in all_args:
            lower = arg.lower()
            if lower in BG_MAP:
                bg_key = lower
            elif lower in SPEED_MAP:
                duration = SPEED_MAP[lower]
            else:
                god_parts.append(arg)

        god_name = " ".join(god_parts)
        if not god_name:
            print("No god name provided")
            return

        # Find and set the god image
        image_path = find_god_image(god_name)
        if not image_path:
            print(f"No image found for '{god_name}'")
            return

        print(f"\n  God: {god_name} ({image_path.name})")
        print(f"  Background: {bg_key}")
        print(f"  Fade duration: {duration}s")

        # Set image files
        client.set_input_settings(OBS_SOURCE_GOD_IMAGE, {"file": str(image_path.resolve())}, overlay=True)

        if SMITE2_GOD_BG_DIR:
            bg_path = Path(SMITE2_GOD_BG_DIR) / BG_MAP.get(bg_key, "bg_chaos_red.png")
            if bg_path.exists():
                client.set_input_settings(OBS_SOURCE_GOD_BG, {"file": str(bg_path.resolve())}, overlay=True)

        # Fade in
        fade(client, sources, fade_in=True, duration=duration)

    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
