"""
Test script for OBS god image + background source swapping.
Connects to OBS WebSocket and swaps the image source to a god's funny image,
and sets the appropriate background based on team/role.

Usage:
  python test_god_image.py Atlas                — Set god image + default (chaos) background
  python test_god_image.py Atlas chaos           — Set god image + chaos background
  python test_god_image.py Atlas order           — Set god image + order background
  python test_god_image.py Atlas carry           — Set god image + carry (gold) background
  python test_god_image.py Atlas support         — Set god image + support (emerald) background
  python test_god_image.py Atlas mid             — Set god image + mid (blue) background
  python test_god_image.py Atlas jungle          — Set god image + jungle (forest) background
  python test_god_image.py Atlas solo            — Set god image + solo (orange) background
  python test_god_image.py clear                 — Hide god image + background
  python test_god_image.py list                  — List all available god images
  python test_god_image.py backgrounds           — List all available backgrounds
"""

import sys
import os
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
    print("obsws-python not installed! Install with:")
    print("  pip install obsws-python")
    sys.exit(1)


# Background file mapping (mirrors SmitePlugin.BG_MAP)
BG_MAP = {
    "chaos": "bg_chaos_red.png",
    "order": "bg_order_blue.png",
    "carry": "bg_carry_gold.png",
    "support": "bg_support_emerald.png",
    "mid": "bg_mid_blue.png",
    "jungle": "bg_jungle_forest.png",
    "solo": "bg_solo_orange.png",
}


def find_god_image(god_name):
    """Look for a matching god image file, trying several name formats."""
    god_dir = Path(SMITE2_GOD_IMAGES_DIR)
    if not god_dir.exists():
        print(f"ERROR: God images directory not found: {god_dir}")
        return None

    slug = god_name.lower().replace(" ", "-").replace("'", "")
    names = [god_name, god_name.lower(), slug]
    extensions = [".gif", ".png"]

    for ext in extensions:
        for name in names:
            path = god_dir / f"{name}{ext}"
            if path.exists():
                return path
    return None


def list_gods():
    """List all image files in the god images directory."""
    god_dir = Path(SMITE2_GOD_IMAGES_DIR)
    if not god_dir.exists():
        print(f"ERROR: God images directory not found: {god_dir}")
        return

    images = sorted([
        f for f in god_dir.iterdir()
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
    ])

    if not images:
        print(f"No images found in: {god_dir}")
        return

    print(f"\nGod images in: {god_dir}")
    print(f"{'='*50}")
    for img in images:
        print(f"  {img.stem:<25} ({img.suffix})")
    print(f"\n{len(images)} images total")


def list_backgrounds():
    """List all background images."""
    bg_dir = Path(SMITE2_GOD_BG_DIR) if SMITE2_GOD_BG_DIR else None
    if not bg_dir or not bg_dir.exists():
        print(f"ERROR: Background directory not found: {bg_dir}")
        return

    print(f"\nBackgrounds in: {bg_dir}")
    print(f"{'='*50}")
    for key, filename in sorted(BG_MAP.items()):
        path = bg_dir / filename
        exists = "OK" if path.exists() else "MISSING"
        print(f"  {key:<10} → {filename:<30} [{exists}]")


def connect_obs():
    """Connect to OBS WebSocket."""
    try:
        client = obs.ReqClient(
            host=OBS_WS_HOST,
            port=OBS_WS_PORT,
            password=OBS_WS_PASSWORD,
        )
        print(f"Connected to OBS at {OBS_WS_HOST}:{OBS_WS_PORT}")
        return client
    except Exception as e:
        print(f"ERROR: Could not connect to OBS: {e}")
        print("Make sure OBS is running with WebSocket server enabled.")
        return None


def set_source_visible(client, source_name, visible, scene=None, group=None):
    """Set a source visible/hidden, handling grouped sources."""
    if group:
        item_id = client.get_scene_item_id(group, source_name).scene_item_id
        client.set_scene_item_enabled(group, item_id, visible)
    else:
        if not scene:
            scene = client.get_current_program_scene().scene_name
        scene_item_id = client.get_scene_item_id(scene, source_name).scene_item_id
        client.set_scene_item_enabled(scene, scene_item_id, visible)


def set_god(client, god_name, bg_key="chaos"):
    """Set the OBS image source to the god's image and set background."""
    image_path = find_god_image(god_name)
    if not image_path:
        print(f"\nNo image found for '{god_name}' — hiding source")
        clear_god(client)
        return

    abs_path = str(image_path.resolve())
    scene = OBS_GOD_IMAGE_SCENE or None
    group = OBS_GOD_IMAGE_GROUP or None

    # Set god image
    print(f"\n--- God Image ---")
    print(f"  Source: {OBS_SOURCE_GOD_IMAGE}")
    print(f"  File:   {abs_path}")
    try:
        client.set_input_settings(OBS_SOURCE_GOD_IMAGE, {"file": abs_path}, overlay=True)
        set_source_visible(client, OBS_SOURCE_GOD_IMAGE, True, scene=scene, group=group)
        print(f"  Status: VISIBLE")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Set background
    if SMITE2_GOD_BG_DIR:
        bg_dir = Path(SMITE2_GOD_BG_DIR)
        bg_file = BG_MAP.get(bg_key, "bg_chaos_red.png")
        bg_path = bg_dir / bg_file

        print(f"\n--- Background ---")
        print(f"  Source: {OBS_SOURCE_GOD_BG}")
        print(f"  Key:    {bg_key}")
        print(f"  File:   {bg_path}")

        if bg_path.exists():
            try:
                client.set_input_settings(OBS_SOURCE_GOD_BG, {"file": str(bg_path.resolve())}, overlay=True)
                set_source_visible(client, OBS_SOURCE_GOD_BG, True, scene=scene, group=group)
                print(f"  Status: VISIBLE")
            except Exception as e:
                print(f"  ERROR: {e}")
                print(f"  Make sure '{OBS_SOURCE_GOD_BG}' exists in your OBS group.")
        else:
            print(f"  ERROR: Background file not found: {bg_path}")
    else:
        print(f"\n  (No background dir configured — skipping)")

    print(f"\n  God: {god_name} ({image_path.name}) | BG: {bg_key}")


def clear_god(client):
    """Hide the OBS god image and background sources."""
    scene = OBS_GOD_IMAGE_SCENE or None
    group = OBS_GOD_IMAGE_GROUP or None

    print(f"\nHiding god image and background...")

    for source_name in [OBS_SOURCE_GOD_IMAGE, OBS_SOURCE_GOD_BG]:
        try:
            set_source_visible(client, source_name, False, scene=scene, group=group)
            print(f"  '{source_name}' — hidden")
        except Exception as e:
            print(f"  '{source_name}' — ERROR: {e}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1]

    # Config check
    print(f"Config:")
    print(f"  SMITE2_GOD_IMAGES_DIR = {SMITE2_GOD_IMAGES_DIR or '(not set)'}")
    print(f"  SMITE2_GOD_BG_DIR     = {SMITE2_GOD_BG_DIR or '(not set)'}")
    print(f"  OBS_SOURCE_GOD_IMAGE  = {OBS_SOURCE_GOD_IMAGE}")
    print(f"  OBS_SOURCE_GOD_BG     = {OBS_SOURCE_GOD_BG}")
    print(f"  OBS_GOD_IMAGE_SCENE   = {OBS_GOD_IMAGE_SCENE or '(current scene)'}")
    print(f"  OBS_GOD_IMAGE_GROUP   = {OBS_GOD_IMAGE_GROUP or '(none)'}")
    print(f"  OBS WebSocket         = {OBS_WS_HOST}:{OBS_WS_PORT}")

    if action.lower() == "list":
        list_gods()
        return

    if action.lower() == "backgrounds":
        list_backgrounds()
        return

    if not SMITE2_GOD_IMAGES_DIR:
        print("\nERROR: SMITE2_GOD_IMAGES_DIR is not set in config_local.py")
        return

    client = connect_obs()
    if not client:
        return

    try:
        if action.lower() == "clear":
            clear_god(client)
        else:
            # Parse: god_name [bg_key]
            # Last arg might be a bg key (chaos, order, carry, support, mid, jungle, solo)
            all_args = sys.argv[1:]
            bg_key = "chaos"  # default

            if len(all_args) > 1 and all_args[-1].lower() in BG_MAP:
                bg_key = all_args[-1].lower()
                god_name = " ".join(all_args[:-1])
            else:
                god_name = " ".join(all_args)

            set_god(client, god_name, bg_key)
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
