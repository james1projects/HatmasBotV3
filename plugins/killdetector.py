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
import logging
import re
from pathlib import Path

import numpy as np
from PIL import Image

from core.config import (
    OBS_WS_HOST, OBS_WS_PORT, OBS_WS_PASSWORD,
    DATA_DIR, TESSERACT_PATH,
)

# --- File logger ---
# Writes all KillDetector output to data/killdetect.log for debugging.
# The log file is overwritten each session to avoid unbounded growth.
# Guard against duplicate handlers if module is imported more than once.
_log_path = DATA_DIR / "killdetect.log"
_kd_logger = logging.getLogger("KillDetector")
_kd_logger.setLevel(logging.DEBUG)
_kd_logger.propagate = False  # Don't duplicate to root logger
_file_handler = None

if not _kd_logger.handlers:
    # File handler — overwrites each session
    _file_handler = logging.FileHandler(str(_log_path), mode="w", encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    _kd_logger.addHandler(_file_handler)
    # Also print to console
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(logging.Formatter("%(message)s"))
    _kd_logger.addHandler(_console_handler)
else:
    # Already initialized — find the existing file handler
    for _h in _kd_logger.handlers:
        if isinstance(_h, logging.FileHandler):
            _file_handler = _h
            break


def _log(msg: str):
    """Log a KillDetector message to both console and file."""
    _kd_logger.info(msg)
    # Flush file handler immediately so logs survive hard kills
    if _file_handler is not None:
        _file_handler.flush()

# --- Detection config (1920x1080) ---

# KDA HUD region — small bar showing sword/K skull/D hand/A.
# Moved to bottom-left via the Smite 2 HUD editor so it's always visible
# (never occluded by store/scoreboard overlays).
# Position: (35, 1033, 160, 1055) — a 125x22 pixel rectangle.
# Old position (above god portrait): (625, 905, 725, 932)
# The KDA numbers sit inside a semi-transparent dark bar.
KDA_REGION = (35, 1033, 160, 1055)

# Store / scoreboard detection (left 60% of screen).
# When the store or scoreboard is open the KDA bar is hidden, so we skip
# reading those frames to avoid bad OCR reads corrupting the previous KDA.
OVERLAY_CHECK_REGION = (0, 100, 1152, 900)
OVERLAY_DARK_THRESHOLD = 0.65   # Dark ratio above this → overlay is open

# Non-gameplay screen detection (god select, results, lobby, etc.).
# During gameplay the bottom HUD (ability bar, health/mana) is always visible
# with high color variance from the ability icons.  On non-gameplay screens
# (god select, post-match results, lobby) this area is dark and flat.
# We check TWO regions: the ability bar and the KDA bar.  The ability bar
# can dim/disappear when dead, but the KDA bar stays visible during death.
# If EITHER region passes, we consider it gameplay.
HUD_CHECK_REGION = (600, 1000, 780, 1080)  # Bottom-center ability bar area
HUD_MIN_STD = 25       # Below this → no ability bar → not gameplay
HUD_MIN_MEAN = 40      # Below this → too dark → not gameplay
KDA_CHECK_MIN_STD = 30  # KDA bar visible if std above this
NON_GAMEPLAY_FRAMES = 3  # Consecutive non-gameplay frames before resetting

# Screenshot interval
SCREENSHOT_INTERVAL = 0.8  # Seconds between screenshot grabs

# Multi-kill timing: kills within this window count as multi-kills.
# Smite 2 multi-kill windows: ~10s for double, extends per kill.
MULTIKILL_WINDOW = 10.0  # Seconds

# Max plausible KDA jump per frame.  At ~0.8s intervals, even a penta kill
# won't produce more than 5 new kills in a single read.  Anything larger is
# almost certainly an OCR misread (e.g. "19" → "49").
# However, if OCR fails for many frames, the real KDA can advance past this
# limit.  We scale the max jump based on seconds since last successful read:
# base 5 + 1 per 3 seconds elapsed.
MAX_KDA_JUMP_BASE = 5
MAX_KDA_JUMP_PER_SEC = 1.0 / 3.0  # Allow 1 extra kill per 3 seconds of missed reads

# State persistence — survive bot restarts mid-match.
# If the saved state is older than this, treat the current read as a fresh match.
STATE_STALE_SECONDS = 30 * 60  # 30 minutes
STATE_FILE = DATA_DIR / "kda_state.json"

# Startup validation — require multiple consistent reads before accepting
# the first KDA to avoid a single misread poisoning the baseline.
STARTUP_REQUIRED_READS = 3  # Need this many identical reads to accept


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
        self._non_gameplay_count = 0  # Consecutive non-gameplay frames seen

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
        self.on_god_identified = None  # async def on_god_identified(god_name: str)

        # God portrait matcher — identifies the god from the in-game portrait
        # before tracker.gg API responds (which has a 2-5 min delay).
        self._god_matcher = None
        self._god_identified = False  # True once we've matched the god this match

        # Template-based digit matcher — primary digit recognition method.
        # Falls back to Tesseract OCR when templates don't match.
        self._digit_matcher = None
        self._last_digit_crops = None  # Stored per-read for auto-collection

        # Startup validation — multiple consistent reads before accepting
        self._startup_reads = []
        self._startup_validated = False

        # OCR availability flag
        self._ocr_available = False
        self._check_ocr()

    def _check_ocr(self):
        """Check if pytesseract + Tesseract binary are available."""
        try:
            import pytesseract
            _log(f"[KillDetector] pytesseract imported OK")
        except ImportError as e:
            self._ocr_available = False
            _log(f"[KillDetector] pytesseract not installed: {e}")
            _log("[KillDetector] Run: pip install pytesseract")
            return

        try:
            tess_path = TESSERACT_PATH
            _log(f"[KillDetector] TESSERACT_PATH = {tess_path!r}")
            if tess_path:
                import os
                if not os.path.exists(tess_path):
                    _log(f"[KillDetector] WARNING: Tesseract binary not found at: {tess_path}")
                    _log("[KillDetector] Update TESSERACT_PATH in config_local.py")
                    self._ocr_available = False
                    return
                pytesseract.pytesseract.tesseract_cmd = tess_path

            version = pytesseract.get_tesseract_version()
            self._ocr_available = True
            _log(f"[KillDetector] Tesseract OCR available (v{version})")
        except Exception as e:
            self._ocr_available = False
            _log(f"[KillDetector] Tesseract binary not working: {e}")
            _log(f"[KillDetector] Tried path: {TESSERACT_PATH!r}")
            _log("[KillDetector] Install from: https://github.com/UB-Mannheim/tesseract/wiki")
            _log("[KillDetector] Then set TESSERACT_PATH in config_local.py")

    def setup(self, bot):
        self.bot = bot

    async def on_ready(self):
        """Connect to OBS WebSocket, load god portrait matcher and digit matcher."""
        await self._connect_obs()
        await self._load_god_matcher()
        self._load_digit_matcher()

    def _load_digit_matcher(self):
        """Load the template-based digit matcher for KDA recognition."""
        try:
            from core.digit_matcher import DigitMatcher
            template_dir = DATA_DIR / "digit_templates"
            self._digit_matcher = DigitMatcher(template_dir)
            if self._digit_matcher.is_loaded:
                coverage = sorted(self._digit_matcher.digit_coverage)
                _log(f"[KillDetector] Digit matcher ready "
                      f"({self._digit_matcher.template_count} templates, "
                      f"digits: {','.join(coverage)})")
            else:
                _log("[KillDetector] Digit matcher has no templates — "
                      "will auto-collect from confirmed OCR reads")
        except ImportError as e:
            _log(f"[KillDetector] Digit matcher not available: {e}")
            self._digit_matcher = None
        except Exception as e:
            _log(f"[KillDetector] Digit matcher load error: {e}")
            self._digit_matcher = None

    async def _connect_obs(self):
        """Create a dedicated OBS WebSocket connection for screenshots."""
        try:
            import obsws_python as obs
            self.obs_client = obs.ReqClient(
                host=OBS_WS_HOST,
                port=OBS_WS_PORT,
                password=OBS_WS_PASSWORD,
            )
            _log("[KillDetector] Connected to OBS WebSocket")
        except Exception as e:
            _log(f"[KillDetector] OBS connection failed: {e}")
            self.obs_client = None

    async def _load_god_matcher(self):
        """Load the god portrait matcher for early god detection."""
        try:
            from core.god_matcher import GodMatcher
            self._god_matcher = GodMatcher()
            if self._god_matcher.load_icons():
                _log(f"[KillDetector] God portrait matcher ready "
                      f"({self._god_matcher.icon_count} icons)")
            else:
                _log("[KillDetector] God portrait matcher has no icons — "
                      "run download_god_icons.py")
                self._god_matcher = None
        except ImportError as e:
            _log(f"[KillDetector] God matcher not available: {e}")
            self._god_matcher = None
        except Exception as e:
            _log(f"[KillDetector] God matcher load error: {e}")
            self._god_matcher = None

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
            self._screenshot_failures = 0
            return img.convert("RGB")
        except Exception as e:
            self._screenshot_failures = getattr(self, '_screenshot_failures', 0) + 1
            if self._screenshot_failures <= 3 or self._screenshot_failures % 50 == 0:
                _log(f"[KillDetector] Screenshot failed: {e}")
            # If many failures in a row, OBS likely disconnected — reset client
            # so the loop will attempt reconnection
            if self._screenshot_failures >= 10:
                self.obs_client = None
                self._screenshot_failures = 0
                _log("[KillDetector] Too many screenshot failures — will retry OBS connection")
            return None

    # === REGION ANALYSIS ===

    def _is_overlay_open(self, img_array: np.ndarray) -> bool:
        """Check if the store or scoreboard overlay is open (high dark pixel ratio)."""
        x1, y1, x2, y2 = OVERLAY_CHECK_REGION
        region = img_array[y1:y2, x1:x2]
        dark_ratio = np.mean(region < 40)
        return dark_ratio > OVERLAY_DARK_THRESHOLD

    def _is_gameplay_screen(self, img_array: np.ndarray) -> bool:
        """Check if the screen shows actual gameplay.

        Checks two regions:
          1. Ability bar (bottom-center) — bright and colorful during gameplay,
             but can dim when dead.
          2. KDA bar — always visible during gameplay, even when dead.

        If EITHER region passes, we consider it gameplay.  On non-gameplay
        screens (god select, results, lobby) both regions are dark/flat.
        """
        # Check 1: ability bar
        x1, y1, x2, y2 = HUD_CHECK_REGION
        region = img_array[y1:y2, x1:x2]
        hud_std = np.std(region)
        hud_mean = np.mean(region)
        if hud_std >= HUD_MIN_STD and hud_mean >= HUD_MIN_MEAN:
            return True

        # Check 2: KDA bar — catches death screens where ability bar dims
        x1, y1, x2, y2 = KDA_REGION
        kda_region = img_array[y1:y2, x1:x2]
        kda_std = np.std(kda_region)
        if kda_std >= KDA_CHECK_MIN_STD:
            return True

        return False

    def _ocr_from_binarized(self, bordered: np.ndarray, labels: np.ndarray,
                             stats: np.ndarray, digits: list,
                             t_start: float) -> tuple[int, int, int] | None:
        """Run OCR on grouped digit components from a binarized image.

        Returns (K, D, A) tuple or None if any group fails.
        This is called by _read_kda for each binarization method attempted.
        """
        import pytesseract
        import cv2

        digits.sort(key=lambda c: c[0])

        # Group digits into K, D, A by the two largest gaps.
        gaps = []
        for j in range(len(digits) - 1):
            right_edge = digits[j][0] + digits[j][2]
            left_edge = digits[j + 1][0]
            gaps.append((left_edge - right_edge, j))

        if len(gaps) < 2:
            return None

        gaps.sort(reverse=True)
        split_indices = sorted([gaps[0][1], gaps[1][1]])

        groups = [
            digits[:split_indices[0] + 1],                        # K
            digits[split_indices[0] + 1:split_indices[1] + 1],    # D
            digits[split_indices[1] + 1:],                        # A
        ]

        if not all(groups):
            return None

        # Recognize each digit group.
        # Strategy: try template matching first (fast, no Tesseract overhead),
        # then fall back to Tesseract OCR with dilation + original variants.
        kda_values = []
        digit_crops = []  # Store per-digit binary crops for auto-collection
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        for idx, group in enumerate(groups):
            group_label = ["K", "D", "A"][idx]

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

            # --- Per-component digit recognition ---
            # Try template matching on each component first. For any that
            # fail, fall back to Tesseract on that individual component
            # (not the whole group — that causes missed digits in multi-
            # digit numbers like "12" being read as "1").
            matched_digits = []
            for comp in group:
                cx, cy, cw, ch = comp[0], comp[1], comp[2], comp[3]
                comp_crop = bordered[cy:cy+ch, cx:cx+cw]
                if comp_crop.size == 0:
                    matched_digits.append(None)
                    continue

                # Try template matching first
                digit_str = None
                if self._digit_matcher and self._digit_matcher.is_loaded:
                    std_crop = self._digit_matcher.prepare_candidate(comp_crop)
                    result = self._digit_matcher.match(std_crop)
                    if result is not None:
                        digit_str, dist = result
                        digit_crops.append((digit_str, comp_crop))

                # Fall back to Tesseract on this single component
                if digit_str is None:
                    # Pad and prepare the individual component for OCR
                    comp_padded = cv2.copyMakeBorder(
                        comp_crop, 20, 20, 20, 20,
                        cv2.BORDER_CONSTANT, value=0
                    )
                    thickened = cv2.dilate(comp_padded, kernel, iterations=1)
                    inv_dilated = 255 - thickened
                    inv_original = 255 - comp_padded

                    # Try multiple PSM modes on this single digit
                    for img_variant, psm in [
                        (inv_dilated, 10), (inv_dilated, 8),
                        (inv_original, 10), (inv_original, 8),
                    ]:
                        candidate = pytesseract.image_to_string(
                            img_variant,
                            config=f"--psm {psm} -c tessedit_char_whitelist=0123456789"
                        ).strip()
                        if candidate and len(candidate) == 1:
                            digit_str = candidate
                            digit_crops.append((digit_str, comp_crop))
                            break

                matched_digits.append(digit_str)

            # Check if all components in the group were recognized
            if all(d is not None for d in matched_digits):
                text = "".join(matched_digits)
                # Log which method(s) were used
                if self._debug:
                    _log(f"[KillDetector] KDA: template matched "
                          f"{group_label}='{text}'")
            else:
                text = ""
                if self._debug:
                    failed = [i for i, d in enumerate(matched_digits) if d is None]
                    _log(f"[KillDetector] KDA: {group_label} group failed "
                          f"on component(s) {failed}")

            if not text:
                if self._debug:
                    elapsed = (time.time() - t_start) * 1000
                    _log(f"[KillDetector] KDA: OCR returned empty for "
                          f"{group_label} group [{elapsed:.0f}ms]")
                return None

            try:
                val = int(text)
            except ValueError:
                if self._debug:
                    elapsed = (time.time() - t_start) * 1000
                    _log(f"[KillDetector] KDA: non-numeric OCR result "
                          f"'{text}' for {group_label} [{elapsed:.0f}ms]")
                return None

            # Sanity: individual KDA values above 99 are not plausible
            if val > 99:
                if self._debug:
                    _log(f"[KillDetector] KDA: value {val} too large "
                          f"for {group_label} group")
                return None

            kda_values.append(val)

        if len(kda_values) != 3:
            return None

        # Store digit crops for auto-collection after sanity checks pass
        self._last_digit_crops = digit_crops
        return tuple(kda_values)

    def _binarize_and_find_digits(self, gray_big: np.ndarray,
                                   method: str = "otsu") -> tuple | None:
        """Binarize the scaled KDA image and find digit components.

        Returns (bordered, labels, stats, digits) or None if < 3 digits found.
        method: "otsu" for global Otsu, "adaptive" for Gaussian adaptive.
        """
        import cv2

        if method == "otsu":
            _, bw = cv2.threshold(gray_big, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:
            bw = cv2.adaptiveThreshold(
                gray_big, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                blockSize=51, C=-10
            )

        bordered = cv2.copyMakeBorder(bw, 20, 20, 20, 20,
                                      cv2.BORDER_CONSTANT, value=0)

        num_labels, labels, stats, centroids = \
            cv2.connectedComponentsWithStats(bordered, connectivity=8)

        digits = []
        for i in range(1, num_labels):
            x, y, w, h, area = stats[i]
            if area < 150 or w < 20 or h < 40:
                continue
            if h > 88:
                continue
            digits.append((x, y, w, h, area, i))

        if len(digits) < 3:
            return None

        return bordered, labels, stats, digits

    def _read_kda(self, img: Image.Image) -> tuple[int, int, int] | None:
        """
        Read the K/D/A numbers from the HUD using OCR.

        The KDA bar shows: [sword K] [skull D] [hand A].
        Pipeline:
          1. Crop + scale 8x + binarize (Otsu, fallback adaptive)
          2. Connected component analysis — separate icons from digits
          3. Group digits into K/D/A by the two largest x-gaps
          4. OCR each group (PSM 8 → 7 → 10 fallback chain, with dilation)

        Returns (kills, deaths, assists) or None if unreadable.
        """
        if not self._ocr_available and not (
                self._digit_matcher and self._digit_matcher.is_loaded):
            return None

        t_start = time.time()

        try:
            import pytesseract
            import cv2
            if TESSERACT_PATH:
                pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

            crop = img.crop(KDA_REGION)
            gray = np.array(crop.convert("L"))

            # Early-out: if the KDA region is nearly uniform (std < 5),
            # there are no digits visible — skip expensive OCR processing.
            crop_std = np.std(gray)
            if crop_std < 5:
                elapsed = (time.time() - t_start) * 1000
                if self._debug:
                    _log(f"[KillDetector] KDA: blank region "
                          f"(std={crop_std:.1f}) [{elapsed:.0f}ms]")
                return None

            # Scale up 8x for OCR
            scale = 8
            gray_big = cv2.resize(gray, (gray.shape[1] * scale, gray.shape[0] * scale),
                                  interpolation=cv2.INTER_CUBIC)

            if self._debug:
                crop.save(str(self._debug_dir / "last_kda_crop.png"))

            # Try Otsu binarization first (best when contrast is good),
            # then adaptive thresholding as fallback (handles varying
            # backgrounds behind the semi-transparent HUD bar).
            result = None
            for method in ["otsu", "adaptive"]:
                found = self._binarize_and_find_digits(gray_big, method)
                if found is None:
                    continue

                bordered, labels, stats, digits = found

                if self._debug and method == "otsu":
                    Image.fromarray(bordered).save(
                        str(self._debug_dir / "last_kda_binary.png"))

                result = self._ocr_from_binarized(
                    bordered, labels, stats, digits, t_start
                )
                if result is not None:
                    if self._debug and method != "otsu":
                        _log(f"[KillDetector] KDA: Otsu failed, "
                              f"adaptive succeeded")
                    break

            if result is None:
                elapsed = (time.time() - t_start) * 1000
                if self._debug:
                    _log(f"[KillDetector] KDA: all methods failed [{elapsed:.0f}ms]")
                return None

            k, d, a = result
            elapsed = (time.time() - t_start) * 1000
            _log(f"[KillDetector] KDA read: {k}/{d}/{a} [{elapsed:.0f}ms]")
            return result

        except Exception as e:
            if self._debug:
                _log(f"[KillDetector] KDA OCR error: {e}")
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

    def _archive_debug_dir(self):
        """Archive previous session's debug images into a timestamped subfolder.

        Preserves all frames for offline testing / regression analysis.
        Only moves .png files from the top-level debug dir; existing
        archive subfolders are left untouched.
        """
        if not self._debug_dir.exists():
            return
        pngs = [f for f in self._debug_dir.iterdir()
                if f.suffix == ".png" and f.is_file()]
        if not pngs:
            return

        # Name the archive folder by the oldest file's modification time
        # so it reflects when that session actually ran.
        import datetime
        oldest_mtime = min(f.stat().st_mtime for f in pngs)
        ts = datetime.datetime.fromtimestamp(oldest_mtime).strftime("%Y%m%d_%H%M%S")
        archive = self._debug_dir / ts
        archive.mkdir(exist_ok=True)

        moved = 0
        for f in pngs:
            try:
                f.rename(archive / f.name)
                moved += 1
            except OSError:
                pass
        if moved:
            _log(f"[KillDetector] Archived {moved} debug images → {ts}/")

    async def start_detection(self, manual=False):
        """Start the detection loop.
        manual=True bypasses the in-match check (for testing in jungle practice, etc.)
        """
        if self._running:
            return
        has_matcher = self._digit_matcher and self._digit_matcher.is_loaded
        if not self._ocr_available and not has_matcher:
            _log("[KillDetector] ERROR: Cannot start — no Tesseract OCR or digit templates available")
            return
        # Clean stale debug images from previous session
        if self._debug:
            self._archive_debug_dir()
        self._running = True
        self._manual_mode = manual
        self._task = asyncio.create_task(self._detection_loop())
        mode = "MANUAL" if manual else "AUTO"
        _log(f"[KillDetector] Detection started ({mode})")

    async def stop_detection(self):
        """Stop the detection loop."""
        self._running = False
        self._manual_mode = False
        if self._task:
            self._task.cancel()
            self._task = None
        _log("[KillDetector] Detection stopped")

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
        self._non_gameplay_count = 0
        self._god_identified = False  # Reset for new match god detection
        self._last_digit_crops = None
        self._startup_reads = []  # Clear startup validation buffer
        self._startup_validated = False
        self._save_state()  # Persist the reset

    # === STATE PERSISTENCE ===

    def _save_state(self):
        """Save current KDA + match stats to disk for restart recovery."""
        import json
        state = {
            "kda": list(self._prev_kda) if self._prev_kda else None,
            "match_kills": self.match_kills,
            "match_deaths": self.match_deaths,
            "match_assists": self.match_assists,
            "match_kill_types": self.match_kill_types,
            "timestamp": time.time(),
        }
        try:
            STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        except OSError as e:
            if self._debug:
                _log(f"[KillDetector] Failed to save state: {e}")

    def _load_state(self) -> bool:
        """Load saved KDA state from disk if it exists and is recent enough.

        Returns True if state was restored, False if starting fresh.
        """
        import json
        if not STATE_FILE.exists():
            _log("[KillDetector] No saved state found — starting fresh")
            return False
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log(f"[KillDetector] Could not read saved state: {e}")
            return False

        saved_time = state.get("timestamp", 0)
        age = time.time() - saved_time
        if age > STATE_STALE_SECONDS:
            _log(f"[KillDetector] Saved state is {age/60:.0f} min old "
                  f"(>{STATE_STALE_SECONDS/60:.0f} min) — starting fresh")
            return False

        saved_kda = state.get("kda")
        if saved_kda is None:
            _log("[KillDetector] Saved state has no KDA — starting fresh")
            return False

        # Restore state — the detection loop will validate against
        # live reads before accepting.
        self._prev_kda = tuple(saved_kda)
        self.match_kills = state.get("match_kills", 0)
        self.match_deaths = state.get("match_deaths", 0)
        self.match_assists = state.get("match_assists", 0)
        self.match_kill_types = state.get("match_kill_types", {})
        self._last_kda_read_time = saved_time

        _log(f"[KillDetector] Restored state from {age:.0f}s ago: "
              f"KDA={saved_kda[0]}/{saved_kda[1]}/{saved_kda[2]}, "
              f"stats={self.match_kills}K/{self.match_deaths}D/"
              f"{self.match_assists}A")
        return True

    async def _detection_loop(self):
        """Main loop: grab screenshots, read KDA, detect changes.

        Always runs while active — does not require the Smite plugin to
        report an in-match state.  The _is_gameplay_screen() check ensures
        we only process frames from actual gameplay, so this safely idles
        when on menus, god select, lobby, etc.  This lets god portrait
        identification work instantly (even in jungle practice) without
        waiting for the tracker.gg API.
        """
        _log("[KillDetector] Detection loop running (always-on)")
        _frame_count = 0
        _obs_retry_interval = 10  # seconds between OBS reconnect attempts

        # Try to restore state from a previous session
        self._load_state()
        while self._running:
            try:
                # If OBS isn't connected, try to reconnect periodically
                if not self.obs_client:
                    await self._connect_obs()
                    if not self.obs_client:
                        await asyncio.sleep(_obs_retry_interval)
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

                _frame_start = time.time()

                # Log every 20 frames so we know it's working
                if _frame_count % 20 == 1:
                    _log(f"[KillDetector] Scanning... (frame {_frame_count})")

                # Debug: save full screenshot every 10 frames (was 50)
                if self._debug and _frame_count % 10 == 1:
                    img.save(str(self._debug_dir / f"frame_{_frame_count}.png"))

                # Detect non-gameplay screens (god select, results, lobby).
                # If we see enough consecutive non-gameplay frames, reset KDA
                # since the game has ended or we're between matches.
                if not self._is_gameplay_screen(img_array):
                    self._non_gameplay_count += 1
                    if self._debug:
                        _log(
                            f"[KillDetector] Non-gameplay screen "
                            f"(count: {self._non_gameplay_count}/"
                            f"{NON_GAMEPLAY_FRAMES})"
                        )
                    if (self._non_gameplay_count >= NON_GAMEPLAY_FRAMES
                            and self._prev_kda is not None):
                        prev = self._prev_kda
                        _log(
                            f"[KillDetector] Game ended (non-gameplay "
                            f"screen detected). Final KDA: "
                            f"{prev[0]}/{prev[1]}/{prev[2]}. Resetting."
                        )
                        self.reset_match_stats()
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue
                # Back in gameplay — reset the non-gameplay counter
                self._non_gameplay_count = 0

                # --- GOD PORTRAIT IDENTIFICATION ---
                # Try to identify the god from the in-game portrait each frame
                # until we get a match.  This fires ~2-5 minutes before the
                # tracker.gg API returns god data.
                if (not self._god_identified
                        and self._god_matcher
                        and self._god_matcher.is_loaded):
                    try:
                        god_name, confidence = self._god_matcher.identify(img)
                        if god_name:
                            self._god_identified = True
                            _log(f"[KillDetector] God identified from portrait: "
                                  f"{god_name} (confidence: {confidence:.3f})")
                            if self.on_god_identified:
                                await self.on_god_identified(god_name)
                    except Exception as e:
                        if self._debug:
                            _log(f"[KillDetector] God match error: {e}")

                # Don't attempt KDA reading until god portrait is identified.
                # This avoids noisy failed reads during lobby, god select, etc.
                if not self._god_identified:
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue

                # Skip if store or scoreboard is open — KDA bar is hidden.
                if self._is_overlay_open(img_array):
                    if self._debug:
                        _log(f"[KillDetector] Overlay detected, skipping")
                    await asyncio.sleep(SCREENSHOT_INTERVAL)
                    continue

                now = time.time()

                # --- KDA NUMBER TRACKING ---
                # Read K/D/A from the HUD.  When K goes up → kill.
                # When D goes up → death.  Multi-kills detected by timing.
                kda = self._read_kda(img)
                if kda is not None:
                    self._kda_read_failures = 0

                    # --- Startup validation ---
                    # On first reads (no _prev_kda, or restored from file
                    # but not yet validated), require multiple consistent
                    # reads before accepting to avoid a single misread
                    # poisoning the baseline.
                    if not self._startup_validated:
                        self._startup_reads.append(kda)
                        if len(self._startup_reads) < STARTUP_REQUIRED_READS:
                            if self._debug:
                                _log(f"[KillDetector] Startup read "
                                      f"{len(self._startup_reads)}/"
                                      f"{STARTUP_REQUIRED_READS}: {kda}")
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                        # Check if all startup reads agree
                        if all(r == self._startup_reads[0]
                               for r in self._startup_reads):
                            validated_kda = self._startup_reads[0]
                            self._startup_validated = True
                            self._startup_reads = []

                            # If we have saved state, validate against it
                            if self._prev_kda is not None:
                                saved = self._prev_kda
                                v_k, v_d, v_a = validated_kda
                                s_k, s_d, s_a = saved
                                if (v_k >= s_k and v_d >= s_d
                                        and v_a >= s_a):
                                    # Live reads >= saved state — consistent.
                                    # Calculate any events that happened
                                    # while the bot was down.
                                    dk = v_k - s_k
                                    dd = v_d - s_d
                                    da = v_a - s_a
                                    self.match_kills += dk
                                    self.match_deaths += dd
                                    self.match_assists += da
                                    self._prev_kda = validated_kda
                                    self._last_kda_read_time = now
                                    _log(
                                        f"[KillDetector] Startup validated "
                                        f"with saved state: "
                                        f"{s_k}/{s_d}/{s_a} → "
                                        f"{v_k}/{v_d}/{v_a} "
                                        f"(+{dk}K/+{dd}D/+{da}A while down)"
                                    )
                                    self._save_state()
                                else:
                                    # Live reads < saved — mismatch.
                                    # Could be a new match or bad saved data.
                                    _log(
                                        f"[KillDetector] Startup mismatch: "
                                        f"saved {s_k}/{s_d}/{s_a} but "
                                        f"read {v_k}/{v_d}/{v_a} — "
                                        f"starting fresh"
                                    )
                                    self.match_kills = 0
                                    self.match_deaths = 0
                                    self.match_assists = 0
                                    self.match_kill_types = {}
                                    self._prev_kda = validated_kda
                                    self._last_kda_read_time = now
                                    self._save_state()
                            else:
                                # No saved state — accept as fresh start
                                self._prev_kda = validated_kda
                                self._last_kda_read_time = now
                                _log(
                                    f"[KillDetector] Startup validated "
                                    f"(fresh): {validated_kda}"
                                )
                                self._save_state()
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue
                        else:
                            # Reads don't agree — drop oldest, keep trying
                            self._startup_reads.pop(0)
                            if self._debug:
                                _log(
                                    f"[KillDetector] Startup reads "
                                    f"inconsistent, retrying: "
                                    f"{self._startup_reads}"
                                )
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                    if self._prev_kda is not None:
                        prev_k, prev_d, prev_a = self._prev_kda
                        cur_k, cur_d, cur_a = kda

                        # Sanity check: KDA should only go up during a match.
                        # If any value decreased, the OCR misread — skip this frame.
                        if cur_k < prev_k or cur_d < prev_d or cur_a < prev_a:
                            if self._debug:
                                _log(
                                    f"[KillDetector] KDA decreased "
                                    f"({self._prev_kda} → {kda}), "
                                    f"likely OCR misread — skipping"
                                )
                            # Don't update _prev_kda — keep the last good read
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                        # Sanity check: reject implausible jumps (e.g. 19→49).
                        # The allowed jump scales with time since last read,
                        # because OCR can fail for many frames in a row and
                        # the real KDA advances during the gap.
                        dk = cur_k - prev_k
                        dd = cur_d - prev_d
                        da = cur_a - prev_a
                        time_since_read = now - self._last_kda_read_time
                        max_jump = int(MAX_KDA_JUMP_BASE
                                       + time_since_read * MAX_KDA_JUMP_PER_SEC)
                        if dk > max_jump or dd > max_jump or da > max_jump:
                            if self._debug:
                                _log(
                                    f"[KillDetector] KDA jump too large "
                                    f"({self._prev_kda} → {kda}, "
                                    f"Δ={dk}/{dd}/{da}, "
                                    f"max={max_jump} after {time_since_read:.1f}s"
                                    f") — skipping"
                                )
                            await asyncio.sleep(SCREENSHOT_INTERVAL)
                            continue

                        # Sanity checks passed — update the last read time
                        self._last_kda_read_time = now

                        # Kill increase
                        if cur_k > prev_k:
                            new_kills = cur_k - prev_k

                            # When OCR misses frames and catches up as a batch
                            # (e.g. 0→4), we don't know the real timing of each
                            # kill.  Only classify multi-kills when we see a
                            # single kill increment — that means OCR was keeping
                            # up and the timing is trustworthy.
                            if new_kills == 1:
                                self._recent_kill_times.append(now)
                                self.match_kills += 1
                                kill_type = self._classify_multikill(now)
                            else:
                                # Batch of missed kills — clear the multi-kill
                                # window since we lost timing information, and
                                # just record them as individual kills.
                                self._recent_kill_times.clear()
                                self.match_kills += new_kills
                                kill_type = "player_kill"

                            self.match_kill_types[kill_type] = (
                                self.match_kill_types.get(kill_type, 0) + 1
                            )
                            _log(
                                f"[KillDetector] KILL: {prev_k}→{cur_k} "
                                f"(+{new_kills}, type={kill_type}, "
                                f"total={self.match_kills})"
                            )

                            # Debug: save the frame that triggered a kill event
                            if self._debug:
                                img.save(str(self._debug_dir /
                                    f"event_kill_{self.match_kills}_f{_frame_count}.png"))

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
                            _log(
                                f"[KillDetector] DEATH: {prev_d}→{cur_d} "
                                f"(+{new_deaths}, total={self.match_deaths})"
                            )

                            # Debug: save the frame that triggered a death event
                            if self._debug:
                                img.save(str(self._debug_dir /
                                    f"event_death_{self.match_deaths}_f{_frame_count}.png"))

                            if self.on_death:
                                asyncio.create_task(self.on_death())

                        # Assist increase
                        if cur_a > prev_a:
                            new_assists = cur_a - prev_a
                            self.match_assists += new_assists
                            _log(
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
                            _log("[KillDetector] Respawned (KDA stable)")

                    # Update _prev_kda and persist state
                    self._prev_kda = kda
                    self._save_state()

                    # Auto-collect digit templates from confirmed reads.
                    # Only runs after sanity checks pass — ensures we're
                    # saving correctly-read digits as reference templates.
                    if (self._digit_matcher is not None
                            and self._last_digit_crops):
                        added = 0
                        for digit_str, crop in self._last_digit_crops:
                            if self._digit_matcher.add_template(digit_str, crop):
                                added += 1
                        if added and self._debug:
                            _log(f"[KillDetector] Auto-collected {added} new "
                                  f"digit template(s)")
                        self._last_digit_crops = None

                else:
                    # KDA read failed
                    self._kda_read_failures += 1
                    if self._debug and self._kda_read_failures % 10 == 1:
                        _log(
                            f"[KillDetector] KDA read failed "
                            f"(consecutive: {self._kda_read_failures})"
                        )

                # Log total frame processing time every 20 frames
                _frame_elapsed = (time.time() - _frame_start) * 1000
                if _frame_count % 20 == 1:
                    _log(f"[KillDetector] Frame {_frame_count} total: "
                          f"{_frame_elapsed:.0f}ms")

                await asyncio.sleep(SCREENSHOT_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _log(f"[KillDetector] Error in detection loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(SCREENSHOT_INTERVAL * 2)

        _log("[KillDetector] Detection loop stopped")

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
        _log(f"[KillDetector] Chat announcements {'enabled' if enabled else 'disabled'}")

    async def cleanup(self):
        """Shutdown the detector and flush logs."""
        _log("[KillDetector] Cleaning up...")
        await self.stop_detection()
        if self.obs_client:
            try:
                self.obs_client.disconnect()
            except Exception:
                pass
        # Flush the log so nothing is lost on shutdown
        for handler in _kd_logger.handlers:
            handler.flush()
