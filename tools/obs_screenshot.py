"""
OBS Screenshot Tool
====================
Grabs a screenshot from the exact same OBS source ("Smite 2") that the
kill detector uses, saves it to data/obs_screenshot.png.

Usage:  python tools/obs_screenshot.py

Use this to determine KDA_REGION coordinates after moving the HUD.
The screenshot is the exact same 1920x1080 PNG the detector works with,
so pixel coordinates from this image map directly to KDA_REGION.
"""

import sys
import base64
import io
from pathlib import Path

# Add project root to path so we can import core.config
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import OBS_WS_HOST, OBS_WS_PORT, OBS_WS_PASSWORD


def main():
    try:
        import obsws_python as obs
    except ImportError:
        print("ERROR: obsws-python not installed. Run: pip install obsws-python")
        return

    from PIL import Image

    print(f"Connecting to OBS at {OBS_WS_HOST}:{OBS_WS_PORT}...")
    try:
        client = obs.ReqClient(
            host=OBS_WS_HOST,
            port=OBS_WS_PORT,
            password=OBS_WS_PASSWORD,
        )
    except Exception as e:
        print(f"ERROR: Could not connect to OBS: {e}")
        print("Make sure OBS is running and WebSocket is enabled.")
        return

    print("Grabbing screenshot from 'Smite 2' source (1920x1080)...")
    try:
        resp = client.get_source_screenshot(
            name="Smite 2",
            img_format="png",
            width=1920,
            height=1080,
            quality=-1,
        )
    except Exception as e:
        print(f"ERROR: Screenshot failed: {e}")
        print("Make sure a source named 'Smite 2' exists in OBS.")
        client.disconnect()
        return

    img_data = resp.image_data
    if "," in img_data:
        img_data = img_data.split(",", 1)[1]
    img_bytes = base64.b64decode(img_data)
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

    out_path = Path(__file__).parent.parent / "data" / "obs_screenshot.png"
    img.save(str(out_path))
    print(f"Saved: {out_path}")
    print(f"Size: {img.size[0]}x{img.size[1]}")

    # Also show the current KDA_REGION crop for reference
    from plugins.killdetector import KDA_REGION
    crop = img.crop(KDA_REGION)
    crop_path = Path(__file__).parent.parent / "data" / "obs_screenshot_kda_crop.png"
    crop.save(str(crop_path))
    print(f"Current KDA crop ({KDA_REGION}): {crop_path}")

    client.disconnect()
    print("Done! Open obs_screenshot.png and find the new KDA position.")


if __name__ == "__main__":
    main()
