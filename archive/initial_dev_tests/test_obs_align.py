"""
OBS God Portrait Alignment Helper
===================================
Sets exact position and size for the God Portrait and Background sources
so they line up perfectly over the in-game god portrait.

Usage:
  python test_obs_align.py                — Show current transform values
  python test_obs_align.py set            — Apply calculated alignment
  python test_obs_align.py set X Y W H    — Apply custom position (x, y, width, height)
  python test_obs_align.py nudge dx dy    — Nudge position by dx, dy pixels

The in-game Smite 2 god portrait at 1920x1080 is approximately:
  - Position: x≈462, y≈728  (top-left corner of portrait frame)
  - Size: ~88x88 px (the bordered portrait frame)

These values may need fine-tuning depending on your game resolution and
any UI scaling you have enabled.
"""

import sys
sys.path.insert(0, ".")

from core.config import (
    OBS_SOURCE_GOD_IMAGE, OBS_SOURCE_GOD_BG,
    OBS_GOD_IMAGE_SCENE, OBS_GOD_IMAGE_GROUP,
    OBS_WS_HOST, OBS_WS_PORT, OBS_WS_PASSWORD,
)

try:
    import obsws_python as obs
except ImportError:
    print("obsws-python not installed!")
    sys.exit(1)


# Default alignment values for Smite 2 at 1920x1080
# Adjust these if your portrait doesn't line up
DEFAULT_X = 458
DEFAULT_Y = 724
DEFAULT_W = 94   # Slightly larger than the 88px portrait for a border effect
DEFAULT_H = 94


def connect():
    try:
        client = obs.ReqClient(host=OBS_WS_HOST, port=OBS_WS_PORT, password=OBS_WS_PASSWORD)
        print(f"Connected to OBS")
        return client
    except Exception as e:
        print(f"ERROR: {e}")
        return None


def get_transform(client, source_name):
    """Get current transform for a source in the group."""
    group = OBS_GOD_IMAGE_GROUP or OBS_GOD_IMAGE_SCENE
    scene = OBS_GOD_IMAGE_SCENE

    try:
        item_id = client.get_scene_item_id(group, source_name).scene_item_id
        transform = client.get_scene_item_transform(group, item_id)
        t = transform.scene_item_transform
        return {
            "item_id": item_id,
            "x": t.get("positionX", 0),
            "y": t.get("positionY", 0),
            "width": t.get("width", 0),
            "height": t.get("height", 0),
            "source_width": t.get("sourceWidth", 0),
            "source_height": t.get("sourceHeight", 0),
            "scale_x": t.get("scaleX", 1),
            "scale_y": t.get("scaleY", 1),
            "bounds_type": t.get("boundsType", ""),
            "bounds_width": t.get("boundsWidth", 0),
            "bounds_height": t.get("boundsHeight", 0),
        }
    except Exception as e:
        print(f"  Could not get transform for '{source_name}': {e}")
        return None


def set_transform(client, source_name, x, y, w, h):
    """Set exact position and size for a source using bounds."""
    group = OBS_GOD_IMAGE_GROUP or OBS_GOD_IMAGE_SCENE

    try:
        item_id = client.get_scene_item_id(group, source_name).scene_item_id

        # Use OBS_BOUNDS_STRETCH to force exact dimensions
        # This scales the source to exactly WxH regardless of source resolution
        client.set_scene_item_transform(group, item_id, {
            "positionX": float(x),
            "positionY": float(y),
            "boundsType": "OBS_BOUNDS_STRETCH",
            "boundsWidth": float(w),
            "boundsHeight": float(h),
            "boundsAlignment": 0,  # Center
        })
        print(f"  '{source_name}' → x={x}, y={y}, {w}x{h}")
        return True
    except Exception as e:
        print(f"  ERROR setting transform for '{source_name}': {e}")
        return False


def show_current(client):
    """Show current transform values for both sources."""
    print(f"\n{'='*50}")
    print(f"Current Transform Values")
    print(f"{'='*50}")

    for source in [OBS_SOURCE_GOD_IMAGE, OBS_SOURCE_GOD_BG]:
        t = get_transform(client, source)
        if t:
            print(f"\n  {source}:")
            print(f"    Position:    x={t['x']:.1f}, y={t['y']:.1f}")
            print(f"    Size:        {t['width']:.1f} x {t['height']:.1f}")
            print(f"    Source:      {t['source_width']} x {t['source_height']}")
            print(f"    Scale:       {t['scale_x']:.4f} x {t['scale_y']:.4f}")
            print(f"    Bounds:      {t['bounds_type']} {t['bounds_width']:.1f}x{t['bounds_height']:.1f}")
        else:
            print(f"\n  {source}: (not found)")


def apply_alignment(client, x, y, w, h):
    """Apply alignment to both god portrait and background."""
    print(f"\nApplying alignment: x={x}, y={y}, {w}x{h}")
    print(f"{'='*50}")

    # Background should be same position and size (it sits behind the portrait)
    set_transform(client, OBS_SOURCE_GOD_BG, x, y, w, h)
    # God portrait on top, same position
    set_transform(client, OBS_SOURCE_GOD_IMAGE, x, y, w, h)

    print(f"\nDone! Check OBS to see if it lines up.")
    print(f"Use 'python test_obs_align.py nudge dx dy' to fine-tune.")


def main():
    client = connect()
    if not client:
        return

    print(f"Scene: {OBS_GOD_IMAGE_SCENE}")
    print(f"Group: {OBS_GOD_IMAGE_GROUP}")
    print(f"Sources: {OBS_SOURCE_GOD_IMAGE}, {OBS_SOURCE_GOD_BG}")

    try:
        if len(sys.argv) < 2:
            show_current(client)
            print(f"\nSuggested alignment for Smite 2 (1920x1080):")
            print(f"  python test_obs_align.py set {DEFAULT_X} {DEFAULT_Y} {DEFAULT_W} {DEFAULT_H}")
            return

        action = sys.argv[1].lower()

        if action == "set":
            if len(sys.argv) >= 6:
                x, y, w, h = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
            else:
                x, y, w, h = DEFAULT_X, DEFAULT_Y, DEFAULT_W, DEFAULT_H
            apply_alignment(client, x, y, w, h)
            show_current(client)

        elif action == "nudge":
            if len(sys.argv) < 4:
                print("Usage: python test_obs_align.py nudge dx dy")
                print("  Example: nudge -2 3  (move left 2px, down 3px)")
                return

            dx, dy = float(sys.argv[2]), float(sys.argv[3])
            # Get current position of the god image
            t = get_transform(client, OBS_SOURCE_GOD_IMAGE)
            if not t:
                print("Could not get current position")
                return

            new_x = t["x"] + dx
            new_y = t["y"] + dy
            w = t["bounds_width"] if t["bounds_width"] > 0 else t["width"]
            h = t["bounds_height"] if t["bounds_height"] > 0 else t["height"]

            if w == 0 or h == 0:
                w, h = DEFAULT_W, DEFAULT_H

            apply_alignment(client, new_x, new_y, w, h)

        elif action == "show":
            show_current(client)

        else:
            print(f"Unknown action: {action}")
            print("Use: set, nudge, show")

    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
