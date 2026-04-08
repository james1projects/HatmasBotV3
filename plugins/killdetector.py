"""
Kill/Death Detector Plugin
===========================
Detects kills and deaths in Smite 2 by analyzing OBS screenshots.

Uses OBS WebSocket's GetSourceScreenshot to grab frames from the game capture,
then reads the K/D/A numbers from the HUD bar above the god portrait.  When
the kill count goes up → kill event (with multi-kill classification based on
timing).  When the death count goes up → death event.

The KDA bar is always visible when alive or dead; hidden only when the store
or scoreboard overlay is open (detected via dark pixel ratio).

Requires:
  - OBS WebSocket (obsws-python) — already configured for the bot
  - Tesseract OCR — `pip install pytesseract` + Tesseract binary installed
  - Pillow + OpenCV — `pip install Pillow opencv-python`

Detection runs every ~0.8s during active matches (driven by smite plugin state).
"""

import asyncio
import time
import io
import base64
import re
from pathlib import Path

import numpy as np
from PIL import Image

from core.config import (
    OBS_WS_HOST, OBS_WS_PORT, OBS_WS_PASSWORD,
    DATA_DIR, TESSERACT_PATH,
)

# --- Detection config (1920x1080) ---

# KDA HUD region — small bar above the god portrait showing sword/K skull/D hand/A.
# Always visible when alive or dead; hidden only when store/scoreboard is open.
# Position: (625, 905, 725, 932) — a 100x27 pixel rectangle.
# The KDA numbers sit inside a semi-transparent dark bar.
# We read this with Otsu/high-pass OCR to track kills and deaths by number changes.
KDA_REGION = (625, 905, 725, 932)

# Store / scoreboard detection (left 60% of screen).
# When the store or scoreboard is open the KDA bar is hidden, so we skip
# reading those frames to avoid bad OCR reads corrupting the previous KDA.
OVERLAY_CHECK_REGION = (0, 100, 1152, 900)
OVERLAY_DARK_THRESHOLD = 0.65   # Dark ratio above this → overlay is open

# Screenshot interval
SCREENSHOT_INTERVAL = 0.8  # Seconds between screenshot grabs

# Multi-kill timing: kills within this window count as multi-kills.
# Smite 2 multi-kill windows: ~10s for double, extends per kill.
MULTIKILL_WINDOW = 10.0  # Seconds

# Max plausible KDA jump per frame.  At ~0.8s intervals, even a penta kill
# won't produce more than 5 new kills in a single read.  Anything larger is
# almost certainly an OCR misread (e.g. "19" → "49").
MAX_KDA_JUMP = 5


class KillDeathDetector:
    """
    Analyzes OBS game screenshots for kill/death events via KDA number tracking.

    Lifecycle:
      1. Created and registered as a plugin
      2. Connects to OBS WebSocket on_ready
      3. Detection loop runs while smite plugin reports in_match
      4. Reads KDA numbers each frame; fires callbacks when K or D increases
    """

    def __init__(self, debug=False):
        self.bot = None
        self.obs_client = None
        self._running = False
        self._task = None
        self._debug = debug

        # Debug output directory
        self._debug_dir = DATA_DIR / "killdetect_debug"
        if self._debug:
            self._debug_dir.mkdir(exist_ok=True)

        # KDA tracking — the sole detection method.
        # We read K/D/A numbers from the HUD each frame and detect changes.
        self._prev_kda = None       # (kills, deaths, assists) from last successful read
        self._kda_read_failures = 0  # consecutive failed reads
        self._last_kda_read_time = 0  # timestamp of last successful OCR read
        self._recent_kill_times = []  # timestamps for multi-kill detection
        self._is_dead = False
        self._manual_mode = False
        self._announce_chat = False  # Announce K/D/A changes in Twitch chat

        # Stats for current match
        self.match_kills = 0
        self.match_deaths = 0
        self.match_assists = 0
        self.match_kill_types = {}  # {"double_kill": 2, "triple_kill": 1, ...}

        # Callbacks — set by the bot/webserver during integration
        self.on_kill = None         # async def on_kill(kill_type: str)
        self.on_death = None        # async def on_death()
        self.on_multikill = None    # async def on_multikill(kill_type: str)
        self.on_assist = None       # async def on_assist()

        # OCR availability flag
        self._ocr_available = False
        self._check_ocr()

    def _check_ocr(self):
        """Check if pytesseract + Tesseract binary are available."""
        try:
            import pytesseract
            print(f"[KillDetector] pytesseract imported OK")
        except ImportError as e:
            self._ocr_available = False
            print(f"[KillDetector] pytesseract not installed: {e}")
            print("[KillDetector] Run: pip install pytesseract")
            return

        try:
            tess_path = TESSERACT_PATH
            print(f"[KillDetector] TESSERACT_PATH = {tess_path!r}")
            if tess_path:
                import os
                if not os.path.exists(tess_path):
                    print(f"[KillDetector] WARNING: Tesseract binary not found at: {tess_path}")
                    print("[KillDetector] Update TESSERACT_PATH in config_local.py")
                    self._ocr_available = False
                    return
                pytesseract.pytesseract.tesseract_cmd = tess_path

            version = pytesseract.get_tesseract_version()
            self._ocr_available = True
            print(f"[KillDetector] Tesseract OCR available (v{version})")
        except Exception as e:
            self._ocr_available = False
            print(f"[KillDetector] Tesseract binary not working: {e}")
            print(f"[KillDetector] Tried path: {TESSERACT_PATH!r}")
            print("[KillDetector] Install from: https://github.com/UB-Mannheim/tesseract/wiki")
            print("[KillDetector] Then set TESSERACT_PATH in config_local.py")

    def setup(self, bot):
        self.bot = bot

    async def on_ready(self):
        """Connect to OBS WebSocket."""
        await self._connect_obs()

    async def _connect_obs(self):
        """Create a dedicated OBS WebSocket connection for screenshots."""
        try:
            import obsws_python as obs
            self.obs_client = obs.ReqClient(
                host=OBS_WS_HOST,
                port=OBS_WS_PORT,
                password=OBS_WS_PASSWORD,
            )
            print("[KillDetector] Connected to OBS WebSocket")
        except Exception as e:
            print(f"[KillDetector] OBS connection failed: {e}")
            self.obs_client = None

    # === SCREENSHOT CAPTURE ===

    def _grab_screenshot(self) -> Image.Image | None:
        """Grab a screenshot of the Smite 2 game source from OBS."""
        if not self.obs_client:
            return None
        try:
            resp = self.obs_client.get_source_screenshot(
                name="Smite 2",
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
            return img.convert("RGB")
        except Exception as e:
            print(f"[KillDetector] Screenshot failed: {e}")
            return None

    # === REGION ANALYSIS ===

    def _is_overlay_open(self, img_array: np.ndarray) -> bool:
        """Check if the store or scoreboard overlay is open (high dark pixel ratio)."""
        x1, y1, x2, y2 = OVERLAY_CHECK_REGION
        region = img_array[y1:y2, x1:x2]
        dark_ratio = np.mean(region < 40)
        return dark_ratio > OVERLAY_DARK_THRESHOLD

    def _read_kda(self, img: Image.Image) -> tuple[int, int, int] | None:
        """
        Read the K/D/A numbers from the HUD using OCR.

        The KDA bar shows: [sword K] [skull D] [hand A].
        The icons (sword, skull, hand) confuse Tesseract, so we:
          1. Binarize with Otsu on 8x-upscaled grayscale crop
          2. Find connected components
          3. Separate icons (height > 88px at 8x scale) from digits
          4. Group digits into K/D/A by the two largest x-gaps
          5. OCR each group individually (clean digit-only image)

        Returns (kills, deaths, assists) or None if unreadable.
        """
        if not self._ocr_available:
            return None

        try:
            import pytesseract
            import cv2
            if TESSERACT_PATH:
                pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

            crop = img.crop(KDA_REGION)
            gray = np.array(crop.convert("L"))

            # Scale up 8x for better OCR
            gray_big = cv2.resize(gray, (gray.shape[1] * 8, gray.shape[0] * 8),
                                  interpolation=cv2.INTER_CUBIC)

            # Otsu binarization
            _, bw = cv2.threshold(gray_big, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            bordered = cv2.copyMakeBorder(bw, 20, 20, 20, 20,
                                          cv2.BORDER_CONSTANT, value=0)

            if self._debug:
                crop.save(str(self._debug_dir / "last_kda_crop.png"))
                Image.fromarray(bordered).save(
                    str(self._debug_dir / "last_kda_binary.png"))

            # --- Connected component analysis ---
            # Find all blobs, then separate icons from digits by height.
            # Icons (sword/skull/hand) are taller: h > 88 at 8x scale.
            # Digits are shorter: h ~73-79.  Noise/borders: area < 150 or w < 20.
            num_labels, labels, stats, centroids = \
                cv2.connectedComponentsWithStats(bordered, connectivity=8)

            digits = []
            for i in range(1, num_labels):
                x, y, w, h, area = stats[i]
                if area < 150 or w < 20:
                    continue  # noise or thin border lines
                if h > 88:
                    continue  # icon (sword, skull, or hand)
                digits.append((x, y, w, h, area, i))

            if len(digits) < 3:
                if self._debug:
                    print(f"[KillDetector] KDA: too few digit components ({len(digits)})")
                return None

            digits.sort(key=lambda c: c[0])

            # Group digits into K, D, A by the two largest gaps.
            # Icons create ~100-180px gaps between digit groups;
            # digits within a multi-digit number are only ~4-30px apart.
            gaps = []
            for j in range(len(digits) - 1):
                right_edge = digits[j][0] + digits[j][2]
                left_edge = digits[j + 1][0]
                gaps.append((left_edge - right_edge, j))

            if len(gaps) < 2:
                if self._debug:
                    print(f"[KillDetector] KDA: not enough gaps to split ({len(gaps)})")
                return None

            gaps.sort(reverse=True)
            split_indices = sorted([gaps[0][1], gaps[1][1]])

            groups = [
                digits[:split_indices[0] + 1],                        # K
                digits[split_indices[0] + 1:split_indices[1] + 1],    # D
                digits[split_indices[1] + 1:],                        # A
            ]

            if not all(groups):
                if self._debug:
                    print(f"[KillDetector] KDA: empty group after split")
                return None

            # OCR each group on a clean digit-only image
            kda_values = []
            for group in groups:
                # Create image with only this group's components
                clean = np.zeros_like(bordered)
                for d in group:
                    clean[labels == d[5]] = 255

                # Crop tight to the group with padding
                min_x = min(d[0] for d in group) - 10
                max_x = max(d[0] + d[2] for d in group) + 10
                min_y = min(d[1] for d in group) - 5
                max_y = max(d[1] + d[3] for d in group) + 5

                cropped = clean[max(0, min_y):max_y, max(0, min_x):max_x]
                cropped = cv2.copyMakeBorder(
                    cropped, 20, 20, 20, 20,
                    cv2.BORDER_CONSTANT, value=0
                )

                # Invert to black-on-white (Tesseract strongly prefers this)
                inverted = 255 - cropped

                # OCR the isolated digit(s) — PSM 7 = single text line
                text = pytesseract.image_to_string(
                    inverted,
                    config="--psm 7 -c tessedit_char_whitelist=0123456789"
                ).strip()

                if not text:
                    if self._debug:
                        print(f"[KillDetector] KDA: OCR returned empty for a group")
                    return None

                try:
                    kda_values.append(int(text))
                except ValueError:
                    if self._debug:
                        print(f"[KillDetector] KDA: non-numeric OCR result '{text}'")
                    return None

            if len(kda_values) != 3:
                return None

            k, d, a = kda_values
            if self._debug:
                print(f"[KillDetector] KDA read: {k}/{d}/{a}")
            return (k, d, a)

        except Exception as e:
            if self._debug:
                print(f"[KillDetector] KDA OCR error: {e}")
            return None

    def _classify_multikill(self, now: float) -> str:
        """
        Classify the type of multi-kill based on recent kill timestamps.
        Call this after recording a new kill time.
        """
        recent = [t for t in self._recent_kill_times
                  if now - t <= MULTIKILL_WINDOW]
        self._recent_kill_times = recent

        count = len(recent)
        if count >= 5:
            return "penta_kill"
        elif count == 4:
            return "quadra_kill"
        elif count == 3:
            return "triple_kill"
        elif count == 2:
            return "double_kill"
        else:
            return "player_kill"

    # === DETECTION LOOP ===

    async def start_detection(self, manual=False):
        """Start the detection loop.
        manual=True bypasses the in-match check (for testing in jungle practice, etc.)
        """
        if self._running:
            return
        if not self._ocr_available:
            print("[KillDetector] ERROR: Cannot start — Tesseract OCR not available")
            return
        self._running = True
        self._manual_mode = manual
        self._task = asyncio.create_task(self._detection_loop())
        mode = "MANUAL" if manual else "AUTO"
        print(f"[KillDetector] Detection started ({mode})")

    async def stop_detection(self):
        """Stop the detection loop."""
        self._running = False
        self._manual_mode = False
        if self._task:
            self._task.cancel()
            self._task = None
        print("[KillDetector] Detection stopped")

    def reset_match_stats(self):
        """Reset stats for a new match."""
        self.match_kills = 0
        self.match_deaths = 0
        self.match_assists = 0
        self.match_kill_types = {}
        self._is_dead = False
        self._prev_kda = None
        self._kda_read_failures = 0
        self._last_kda_read_time = 0
        self._recent_kill_times = []

    async def _detection_loop(self):
        """Main loop: grab screenshots, read KDA, detect changes."""
        print("[KillDetector] Detection loop running (KDA-only mode)")
        _frame_count = 0
        while self._running:
            try:
                # In auto mode, check if smite plugin says we're in a match
                if not self._manual_mode and not self._is_in_match():
                    await asyncio.sleep(5)
                    continue

                # Grab screenshot (blocking I/O — run in executor)
                img = await asyncio.get_event_loop().run_in_executor(
                    None, self._grab_screenshot
                )
                if img is None:
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue

                img_array = np.array(img)
                _frame_count += 1

                # Log every 20 frames so we know it's working
                if _frame_count % 20 == 1:
                    print(f"[KillDetector] Scanning... (frame {_frame_count})")

                # Debug: save full screenshot periodically
                if self._debug and _frame_count % 50 == 1:
                    img.save(str(self._debug_dir / f"frame_{_frame_count}.png"))

                # Skip if store or scoreboard is open — KDA bar is hidden.
                if self._is_overlay_open(img_array):
                    if self._debug:
                        print(f"[KillDetector] Overlay detected, skipping")
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue

                now = time.time()

                # --- KDA NUMBER TRACKING ---
                # Read K/D/A from the HUD.  When K goes up → kill.
                # When D goes up → death.  Multi-kills detected by timing.
                kda = self._read_kda(img)
                if kda is not None:
                    self._kda_read_failures = 0
                    self._last_kda_read_time = now

                    if self._prev_kda is not None:
                        prev_k, prev_d, prev_a = self._prev_kda
                        cur_k, cur_d, cur_a = kda

                        # Sanity check: KDA should only go up during a match.
                        # If any value decreased, the OCR misread — skip this frame.
                        if cur_k < prev_k or cur_d < prev_d or cur_a < prev_a:
                            if self._debug:
                                print(
                                    f"[KillDetector] KDA decreased "
                                    f"({self._prev_kda} → {kda}), "
                                    f"likely OCR misread — skipping"
                                )
                            # Don't update _prev_kda — keep the last good read
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                        # Sanity check: reject implausible jumps (e.g. 19→49).
                        # At ~0.8s intervals a single read can't gain many kills.
                        dk = cur_k - prev_k
                        dd = cur_d - prev_d
                        da = cur_a - prev_a
                        if dk > MAX_KDA_JUMP or dd > MAX_KDA_JUMP or da > MAX_KDA_JUMP:
                            if self._debug:
                                print(
                                    f"[KillDetector] KDA jump too large "
                                    f"({self._prev_kda} → {kda}, "
                                    f"Δ={dk}/{dd}/{da}), "
                                    f"likely OCR misread — skipping"
                                )
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                        # Kill increase
                        if cur_k > prev_k:
                            new_kills = cur_k - prev_k
                            for _ in range(new_kills):
                                self._recent_kill_times.append(now)
                                self.match_kills += 1

                            kill_type = self._classify_multikill(now)
                            self.match_kill_types[kill_type] = (
                                self.match_kill_types.get(kill_type, 0) + 1
                            )
                            print(
                                f"[KillDetector] KILL: {prev_k}→{cur_k} "
                                f"(+{new_kills}, type={kill_type}, "
                                f"total={self.match_kills})"
                            )

                            # Fire callback
                            if kill_type in ("double_kill", "triple_kill",
                                             "quadra_kill", "penta_kill"):
                                if self.on_multikill:
                                    asyncio.create_task(self.on_multikill(kill_type))
                            else:
                                if self.on_kill:
                                    asyncio.create_task(self.on_kill(kill_type))

                        # Death increase
                        if cur_d > prev_d:
                            new_deaths = cur_d - prev_d
                            self.match_deaths += new_deaths
                            self._is_dead = True
                            print(
                                f"[KillDetector] DEATH: {prev_d}→{cur_d} "
                                f"(+{new_deaths}, total={self.match_deaths})"
                            )
                            if self.on_death:
                                asyncio.create_task(self.on_death())

                        # Assist increase
                        if cur_a > prev_a:
                            new_assists = cur_a - prev_a
                            self.match_assists += new_assists
                            print(
                                f"[KillDetector] ASSIST: {prev_a}→{cur_a} "
                                f"(+{new_assists}, total={self.match_assists})"
                            )
                            if self.on_assist:
                                asyncio.create_task(self.on_assist())

                        # Chat announcements
                        if self._announce_chat and self.bot:
                            kda_str = f"{cur_k}/{cur_d}/{cur_a}"
                            if cur_k > prev_k:
                                label = kill_type.replace("_", " ").title()
                                asyncio.create_task(
                                    self.bot.send_chat(
                                        f"{label}! KDA: {kda_str}"
                                    )
                                )
                            elif cur_d > prev_d:
                                asyncio.create_task(
                                    self.bot.send_chat(
                                        f"Hatmaster died. KDA: {kda_str}"
                                    )
                                )
                            elif cur_a > prev_a:
                                asyncio.create_task(
                                    self.bot.send_chat(
                                        f"Assist! KDA: {kda_str}"
                                    )
                                )

                        # If deaths didn't increase but previously was dead,
                        # we're still alive (respawned between reads)
                        if cur_d == prev_d and self._is_dead:
                            self._is_dead = False
                            print("[KillDetector] Respawned (KDA stable)")

                    self._prev_kda = kda

                else:
                    # KDA read failed
                    self._kda_read_failures += 1
                    if self._debug and self._kda_read_failures % 10 == 1:
                        print(
                            f"[KillDetector] KDA read failed "
                            f"(consecutive: {self._kda_read_failures})"
                        )

                await asyncio.sleep(SCREENSHOT_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[KillDetector] Error in detection loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(SCREENSHOT_INTERVAL * 2)

        print("[KillDetector] Detection loop stopped")

    def _is_in_match(self) -> bool:
        """Check if the smite plugin reports an active match."""
        if self.bot and "smite" in self.bot.plugins:
            return self.bot.plugins["smite"].is_in_match
        return False

    # === PUBLIC API ===

    def get_match_stats(self) -> dict:
        """Return current match kill/death/assist stats."""
        return {
            "kills": self.match_kills,
            "deaths": self.match_deaths,
            "assists": self.match_assists,
            "is_dead": self._is_dead,
            "kill_types": dict(self.match_kill_types),
            "kda_failures": self._kda_read_failures,
        }

    def set_debug(self, enabled: bool):
        """Toggle debug mode (saves OCR crops to killdetect_debug/)."""
        self._debug = enabled
        if self._debug:
            self._debug_dir.mkdir(exist_ok=True)

    def set_announce_chat(self, enabled: bool):
        """Toggle chat announcements for kill/death/assist events."""
        self._announce_chat = enabled
        print(f"[KillDetector] Chat announcements {'enabled' if enabled else 'disabled'}")

    async def cleanup(self):
        """Shutdown the detector."""
        await self.stop_detection()
        if self.obs_client:
            try:
                self.obs_client.disconnect()
            except Exception:
                pass
