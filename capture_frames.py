"""
Frame Capture Tool for Kill/Death Detection Tuning
====================================================
Connects to OBS WebSocket and saves a screenshot every 0.8s.

Usage:
    python capture_frames.py              # Saves to data/captured_frames/
    python capture_frames.py --duration 60  # Run for 60 seconds then stop

Play normally — get some kills, die once or twice, open the store, etc.
Press Ctrl+C to stop when done.  The captured frames will be analyzed
to build reliable detection logic.
"""

import time
import io
import base64
import argparse
from pathlib import Path

# --- Config (matches your bot config) ---
OBS_HOST = "localhost"
OBS_PORT = 4455
OBS_PASSWORD = "HatmasBot"
OBS_SOURCE = "Smite 2"
INTERVAL = 0.8  # seconds between captures

OUTPUT_DIR = Path(__file__).parent / "data" / "captured_frames"


def main():
    parser = argparse.ArgumentParser(description="Capture OBS screenshots for analysis")
    parser.add_argument("--duration", type=int, default=0,
                        help="Max seconds to run (0 = until Ctrl+C)")
    args = parser.parse_args()

    # Import here so the script fails fast with a clear message
    try:
        import obsws_python as obs
    except ImportError:
        print("ERROR: obsws-python not installed.  Run: pip install obsws-python")
        return

    from PIL import Image

    # Connect to OBS
    print(f"Connecting to OBS at {OBS_HOST}:{OBS_PORT}...")
    try:
        client = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD)
    except Exception as e:
        print(f"ERROR: Could not connect to OBS: {e}")
        return
    print("Connected!")

    # Prepare output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clean old captures
    old = list(OUTPUT_DIR.glob("frame_*.png"))
    if old:
        print(f"Cleaning {len(old)} old captures...")
        for f in old:
            f.unlink()

    print(f"\nCapturing from '{OBS_SOURCE}' every {INTERVAL}s → {OUTPUT_DIR}/")
    print("Play normally — get kills, die, open store, etc.")
    print("Press Ctrl+C to stop.\n")

    frame_num = 0
    start = time.time()

    try:
        while True:
            if args.duration and (time.time() - start) > args.duration:
                print(f"\nDuration limit ({args.duration}s) reached.")
                break

            t0 = time.time()
            try:
                resp = client.get_source_screenshot(
                    name=OBS_SOURCE,
                    img_format="png",
                    width=1920,
                    height=1080,
                    quality=-1,
                )
                img_data = resp.image_data
                if "," in img_data:
                    img_data = img_data.split(",", 1)[1]
                img_bytes = base64.b64decode(img_data)
                img = Image.open(io.BytesIO(img_bytes))

                frame_num += 1
                elapsed = time.time() - start
                filename = f"frame_{frame_num:04d}_{elapsed:.1f}s.png"
                img.save(str(OUTPUT_DIR / filename))

                if frame_num % 10 == 1:
                    print(f"  [{elapsed:>6.1f}s] Captured frame {frame_num}")

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  Screenshot error: {e}")

            # Sleep the remainder of the interval
            dt = time.time() - t0
            if dt < INTERVAL:
                time.sleep(INTERVAL - dt)

    except KeyboardInterrupt:
        pass

    elapsed = time.time() - start
    print(f"\nDone! Captured {frame_num} frames over {elapsed:.0f}s")
    print(f"Saved to: {OUTPUT_DIR}/")

    try:
        client.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()
