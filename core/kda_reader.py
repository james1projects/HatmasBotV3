"""
KDA Reader
==========
Stateless frame-analysis primitives for Smite 2 K/D/A detection.

This module is the shared engine used by:
  * plugins/killdetector.py — the live OBS-driven detector (async loop,
    match state, god-identification gate, callbacks, chat announcements,
    state persistence)
  * tools/vod_detector.py — the batch tool that replays the same analysis
    against recorded .mp4 files to emit .events.json highlight lists

Everything in here is pure: give it a PIL.Image, get back a (K, D, A)
tuple or None.  No async, no network, no disk writes beyond optional
digit-template enrollment.

Pipeline (inherited from the original detector, unchanged):
  1. Crop KDA_REGION (bottom-left HUD bar, 1920x1080 coords).
  2. Early-out if the crop is nearly uniform (no digits visible).
  3. 8x upscale + Otsu binarization (adaptive Gaussian as fallback).
  4. Connected components → separate icons from digit blobs, group into
     K / D / A by the two largest x-gaps.
  5. Per-component recognition: template match (core/digit_matcher.py)
     first, Tesseract OCR fallback on any component the templates miss.

The region constants match the live HUD layout (KDA bar moved to
bottom-left via the Smite 2 HUD editor so the store / scoreboard
overlays never cover it).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image


# --- Detection config (1920x1080) ------------------------------------------
#
# These are the same constants the live detector used — moved here so there
# is one source of truth shared by both callers.

# KDA HUD region — small bar showing sword/K, skull/D, hand/A.
# Bottom-left via the in-game HUD editor so it is always visible (never
# occluded by store or scoreboard overlays).  Recalibrated 2026-04-30
# against the locked-in HUD layout — the bar sits a few pixels above
# the screen's bottom edge.  Verified visually at (18, 1041) to
# (110, 1061) framing "sword K skull D hand A" cleanly with no
# clipping and no wasted padding around the icons or digits.
# 2026-05-16: trimmed x2 from 110 → 105 to drop a trailing artifact
# the detector kept picking up on the right edge of the bar.
# 2026-05-18: y-coords were drifting up over edits (had been at
# 1029-1053) — that crop captured ~12 px of HUD divider bars at the
# top (which the binarizer treated as noise components) AND clipped
# the bottom of the digits by ~2 px, distorting their shapes enough
# that the digit matcher kept falling through to the tesseract path
# with margins ~0.06.  Restored to the documented coords so the row
# is centered with safe margin on both sides.
# Region coords loaded from data/detector_regions.json on startup,
# with fallback to the historical defaults. Recalibrate by editing
# the JSON file + restarting the bot - see core/detector_regions.py.
from core.detector_regions import load_regions as _load_detector_regions
_DETECTOR_REGIONS = _load_detector_regions()
KDA_REGION = _DETECTOR_REGIONS["kda"]

# Store / scoreboard detection (left 60% of the screen).  When the store
# or scoreboard is open the KDA bar is hidden, so we skip those frames
# to avoid corrupting the last good read.
#
# Two-stage detection (added May 2026 after a stream where dark map
# lighting tripped the single-stage check at 0.75 dark_ratio while the
# K/D/A bar was perfectly readable):
#   Stage 1 — upper-left dark-pixel ratio above OVERLAY_DARK_THRESHOLD.
#             Necessary but no longer sufficient.
#   Stage 2 — confirm the K/D/A bar itself is uniformly dim (low std).
#             Real overlays produce bar_std ~0-15.
#             Visible bar (any gameplay) produces bar_std ~45-55.
# A frame counts as overlay only when BOTH stages agree.
OVERLAY_CHECK_REGION = _DETECTOR_REGIONS["overlay_check"]
OVERLAY_DARK_THRESHOLD = 0.65   # Dark-pixel ratio in upper region (stage 1)
OVERLAY_BAR_VISIBLE_STD = 25    # Bar std above this = bar visible = NOT overlay (stage 2)

# Non-gameplay screen detection (god select, post-match, lobby).
# During gameplay the bottom HUD is always visible with high colour
# variance.  On menus this area goes dark and flat.  We check two
# regions (ability bar + god portrait/health bar); EITHER passing
# counts as gameplay, so death screens still register.
HUD_CHECK_REGION = _DETECTOR_REGIONS["hud_check"]  # Bottom-center ability bar
HUD_MIN_STD = 25
HUD_MIN_MEAN = 40
GAMEPLAY_CHECK_REGION = _DETECTOR_REGIONS["gameplay_check"]  # Portrait + health/mana
GAMEPLAY_CHECK_MIN_STD = 25

# Saturation mask for KDA binarization (applied as a post-pass over
# the Otsu/adaptive output when an RGB crop is available).  KDA digits
# AND the icons are all rendered as pure white/gray — desaturated.
# The KDA bar is partially transparent, so when a SATURATED COLORED
# background bleeds through — red enemy effects, blue water, yellow
# cooldown rings — Otsu's auto-threshold happily includes those
# pixels as "bright = foreground" and produces phantom digits.
#
# We reject any pixel whose saturation (max - min channel) exceeds
# this threshold.  Whites and grays have saturation ~0 so they're
# unaffected (including all anti-aliased edges, which would have
# been hurt by an absolute-brightness whiteness check).  Saturated
# colors uniformly fail: red R=255,G=80,B=80 has saturation=175;
# yellow R=255,G=200,B=50 has 205; blue R=80,G=120,B=255 has 175.
#
# Calibration: when the bar sits over a heavily-saturated background
# (red enemy zone, blue water), the transparency blend pulls the
# white text toward the bg color.  A white pixel (255,255,255) blended
# 70/30 with red bg (200,50,50) lands at (238,193,193) — saturation 45.
# At threshold 40 those tinted-white pixels were getting rejected,
# shaving strokes off digits and producing misreads (10 → 1, etc.).
#
# 90 is the working number: it keeps moderately-tinted whites intact
# (saturation up to ~85 from extreme blends) while still hard-rejecting
# saturated bleed-through. Reference saturations on common noise:
#   red effect (255, 80, 80)   → saturation 175 (still rejected)
#   yellow (255, 200, 50)      → saturation 205 (still rejected)
#   blue water (80, 120, 255)  → saturation 175 (still rejected)
#   orange tint (220, 150, 80) → saturation 140 (still rejected)
#
# Lower it (e.g., 60) if subtle colored noise still survives in
# specific recordings; raise it (e.g., 120) if digits get thinned in
# even more heavily-tinted recordings (and accept some color leakage).
KDA_SATURATION_THRESHOLD = 90


# --- Internal ---------------------------------------------------------------

def _default_data_dir() -> Path:
    """Resolve the default data dir without forcing an import of core.config.

    core.config has side effects (creates directories, loads config_local)
    that a standalone CLI should not trigger unconditionally.  We mirror
    its value: <repo root>/data.
    """
    return Path(__file__).resolve().parent.parent / "data"


class KdaReader:
    """Pure image-to-KDA reader.  No state beyond its loaded matcher.

    Construction is cheap and idempotent.  Pass your own logger if you
    want the reader to emit to somewhere specific; otherwise it uses a
    logger named "KdaReader" that defaults to WARNING via the standard
    logging root.
    """

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        tesseract_path: Optional[str] = None,
        debug: bool = False,
        debug_dir: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._data_dir = Path(data_dir) if data_dir else _default_data_dir()
        self._tesseract_path = tesseract_path
        self._debug = debug
        self._debug_dir = (
            Path(debug_dir) if debug_dir else (self._data_dir / "killdetect_debug")
        )
        if self._debug:
            self._debug_dir.mkdir(parents=True, exist_ok=True)

        self._logger = logger or logging.getLogger("KdaReader")

        # Populated by check_ocr / load_digit_matcher.
        self._ocr_available = False
        self._digit_matcher = None

        # Per-read state — the digit crops saved by the last successful
        # read, for optional enrollment into the digit-matcher library.
        self._last_digit_crops: Optional[list[tuple[str, np.ndarray]]] = None

        # Throttle counter so diagnostic logs don't spam on menu frames.
        self._gameplay_diag_count = 0

        # Resolve matchers up-front so callers don't need to sequence calls.
        self.check_ocr()
        self.load_digit_matcher()

    # --- Properties --------------------------------------------------------

    @property
    def ocr_available(self) -> bool:
        """Whether pytesseract + Tesseract binary are both usable."""
        return self._ocr_available

    @property
    def digit_matcher(self):
        """The loaded DigitMatcher (or None if templates missing)."""
        return self._digit_matcher

    @property
    def has_digit_templates(self) -> bool:
        return self._digit_matcher is not None and self._digit_matcher.is_loaded

    @property
    def is_ready(self) -> bool:
        """True if any recognition path is usable (templates or Tesseract)."""
        return self._ocr_available or self.has_digit_templates

    @property
    def debug(self) -> bool:
        return self._debug

    @debug.setter
    def debug(self, enabled: bool):
        self._debug = enabled
        if enabled:
            self._debug_dir.mkdir(parents=True, exist_ok=True)

    # --- Setup -------------------------------------------------------------

    def check_ocr(self) -> bool:
        """Probe pytesseract + Tesseract binary.  Stores result internally."""
        try:
            import pytesseract
        except ImportError as e:
            self._ocr_available = False
            self._logger.info(f"pytesseract not installed: {e}")
            return False

        try:
            if self._tesseract_path:
                if not os.path.exists(self._tesseract_path):
                    self._logger.warning(
                        f"Tesseract binary not found at: {self._tesseract_path}"
                    )
                    self._ocr_available = False
                    return False
                pytesseract.pytesseract.tesseract_cmd = self._tesseract_path

            version = pytesseract.get_tesseract_version()
            self._ocr_available = True
            self._logger.info(f"Tesseract OCR available (v{version})")
            return True
        except Exception as e:
            self._ocr_available = False
            self._logger.warning(f"Tesseract not working: {e}")
            return False

    def load_digit_matcher(self):
        """Load the template-based digit matcher from DATA_DIR/digit_templates."""
        try:
            from core.digit_matcher import DigitMatcher
        except ImportError as e:
            self._logger.info(f"Digit matcher import failed: {e}")
            self._digit_matcher = None
            return

        try:
            template_dir = self._data_dir / "digit_templates"
            self._digit_matcher = DigitMatcher(template_dir)
            if self._digit_matcher.is_loaded:
                coverage = sorted(self._digit_matcher.digit_coverage)
                self._logger.info(
                    f"Digit matcher ready "
                    f"({self._digit_matcher.template_count} templates, "
                    f"digits: {','.join(coverage)})"
                )
            else:
                self._logger.info(
                    "Digit matcher has no templates — will rely on Tesseract OCR"
                )
        except Exception as e:
            self._logger.warning(f"Digit matcher load error: {e}")
            self._digit_matcher = None

    # --- Scene classification ---------------------------------------------

    def is_overlay_open(
        self,
        img_array: np.ndarray,
        crop_origin: tuple[int, int] = (0, 0),
    ) -> bool:
        """Store / scoreboard dimming check — two-stage so dark gameplay
        maps don't false-positive.

        Stage 1: dark-pixel ratio in the upper-left ``OVERLAY_CHECK_REGION``
        must exceed ``OVERLAY_DARK_THRESHOLD``. If the upper area is
        bright, definitely no overlay (fast path).

        Stage 2: even if upper is dim, confirm by checking that the
        K/D/A bar itself is uniformly dimmed. A real store/scoreboard
        overlay covers the bar (std drops to ~0-15). A dark gameplay
        map leaves the bar with sword/skull/trophy icons + digits
        clearly visible (std stays ~45-55). If the bar still has
        structured content, override stage 1 and treat as gameplay.

        Tuned May 2026 against actual stream data — gameplay bar_std
        was 47-52 on every sample, scoreboards drop near zero, so a
        threshold of 25 has huge margin both ways.

        ``crop_origin`` is where ``img_array``'s (0, 0) pixel sits in the
        original 1920x1080 coordinate system.  VOD scanning passes the
        ffmpeg-side crop origin so the region constants still work even
        though the image is a shipped-over strip rather than a full frame.
        The live path passes the default ``(0, 0)``.
        """
        ox, oy = crop_origin

        # Stage 1: upper-left dark-pixel ratio
        x1, y1, x2, y2 = OVERLAY_CHECK_REGION
        region = img_array[y1 - oy:y2 - oy, x1 - ox:x2 - ox]
        dark_ratio = float(np.mean(region < 40))
        if dark_ratio <= OVERLAY_DARK_THRESHOLD:
            return False

        # Stage 2: K/D/A bar must also be dim (real overlays cover it)
        kx1, ky1, kx2, ky2 = KDA_REGION
        kda_region = img_array[ky1 - oy:ky2 - oy, kx1 - ox:kx2 - ox]
        bar_std = float(np.std(kda_region))
        if bar_std >= OVERLAY_BAR_VISIBLE_STD:
            return False

        return True

    def is_gameplay_screen(
        self,
        img_array: np.ndarray,
        crop_origin: tuple[int, int] = (0, 0),
    ) -> bool:
        """Heuristic: are we currently looking at in-match gameplay?

        Checks two HUD regions.  Either region passing counts, because the
        ability bar can dim during death screens while the god portrait
        area stays populated (and vice versa on rare occlusions).

        ``crop_origin`` — see ``is_overlay_open``.
        """
        ox, oy = crop_origin
        h, w = img_array.shape[:2]

        # Check 1: ability bar
        x1, y1, x2, y2 = HUD_CHECK_REGION
        x1s, y1s, x2s, y2s = x1 - ox, y1 - oy, x2 - ox, y2 - oy
        if y2s <= h and x2s <= w and x1s >= 0 and y1s >= 0:
            region = img_array[y1s:y2s, x1s:x2s]
            hud_std = float(np.std(region))
            hud_mean = float(np.mean(region))
        else:
            hud_std = 0.0
            hud_mean = 0.0

        if hud_std >= HUD_MIN_STD and hud_mean >= HUD_MIN_MEAN:
            self._gameplay_diag_count = 0
            return True

        # Check 2: god portrait + health/mana
        x1, y1, x2, y2 = GAMEPLAY_CHECK_REGION
        x1s, y1s, x2s, y2s = x1 - ox, y1 - oy, x2 - ox, y2 - oy
        if y2s <= h and x2s <= w and x1s >= 0 and y1s >= 0:
            gp_region = img_array[y1s:y2s, x1s:x2s]
            gp_std = float(np.std(gp_region))
        else:
            gp_std = 0.0

        if gp_std >= GAMEPLAY_CHECK_MIN_STD:
            self._gameplay_diag_count = 0
            return True

        # Log the first few failures so misconfigured HUD layouts get noticed.
        if self._gameplay_diag_count < 5:
            self._logger.debug(
                f"Gameplay check FAILED — img={w}x{h}, "
                f"ability_bar(std={hud_std:.1f}, mean={hud_mean:.1f}, "
                f"need std>={HUD_MIN_STD} & mean>={HUD_MIN_MEAN}), "
                f"portrait(std={gp_std:.1f}, need>={GAMEPLAY_CHECK_MIN_STD})"
            )
            self._gameplay_diag_count += 1

        return False

    # --- KDA reading -------------------------------------------------------

    def read_kda(
        self,
        img: Image.Image,
        crop_origin: tuple[int, int] = (0, 0),
    ) -> Optional[tuple[int, int, int]]:
        """Read (kills, deaths, assists) from a full 1920x1080 game image.

        Returns None if the HUD is unreadable (menu, store, too noisy,
        digit matcher rejected, OCR failed, etc.)  The caller is expected
        to apply temporal sanity checks (no-decrease, max-jump) against
        their own prior reads — this function is memoryless.

        On a successful read the per-digit crops are stashed on
        ``self._last_digit_crops`` so callers can opt to enroll them via
        ``enroll_last_read()`` after their own validation passes.

        ``crop_origin`` — see ``is_overlay_open``.  When VOD scanning
        passes an ffmpeg-side cropped strip, ``KDA_REGION`` coords get
        offset by ``crop_origin`` before the PIL crop so the bar still
        lines up inside the smaller image.
        """
        if not self.is_ready:
            return None

        t_start = time.time()

        try:
            import cv2
            import pytesseract
            if self._tesseract_path:
                pytesseract.pytesseract.tesseract_cmd = self._tesseract_path

            ox, oy = crop_origin
            x1, y1, x2, y2 = KDA_REGION
            crop = img.crop((x1 - ox, y1 - oy, x2 - ox, y2 - oy))
            gray = np.array(crop.convert("L"))

            # Early-out: blank / uniform crop means the bar is hidden.
            crop_std = np.std(gray)
            if crop_std < 5:
                if self._debug:
                    elapsed = (time.time() - t_start) * 1000
                    self._logger.debug(
                        f"KDA: blank region (std={crop_std:.1f}) [{elapsed:.0f}ms]"
                    )
                return None

            # 8x upscale — the raw bar is only 125x22 px and OCR wants more.
            scale = 8
            gray_big = cv2.resize(
                gray,
                (gray.shape[1] * scale, gray.shape[0] * scale),
                interpolation=cv2.INTER_CUBIC,
            )
            # Same upscale on the RGB so the binarizer can apply a
            # whiteness mask.  KDA digits are pure white by design;
            # the bar is partially transparent, so bright COLORED
            # background (red enemy effects, blue water, yellow
            # cooldown rings) bleeds through and Otsu happily picks
            # those up as foreground.  min(R,G,B) discriminates: real
            # white survives (all channels high), colored bright stuff
            # fails (at least one channel low).
            rgb = np.array(crop.convert("RGB"))
            rgb_big = cv2.resize(
                rgb,
                (rgb.shape[1] * scale, rgb.shape[0] * scale),
                interpolation=cv2.INTER_CUBIC,
            )

            if self._debug:
                crop.save(str(self._debug_dir / "last_kda_crop.png"))

            # Try Otsu first (best on clean contrast), then adaptive as a
            # fallback for frames where the semi-transparent bar background
            # breaks a global threshold.
            result = None
            for method in ("otsu", "adaptive"):
                found = self._binarize_and_find_digits(
                    gray_big, method, rgb_big=rgb_big)
                if found is None:
                    continue
                bordered, labels, stats, digits = found

                if self._debug and method == "otsu":
                    Image.fromarray(bordered).save(
                        str(self._debug_dir / "last_kda_binary.png")
                    )

                result = self._ocr_from_binarized(
                    bordered, labels, stats, digits, t_start
                )
                if result is not None:
                    if self._debug and method != "otsu":
                        self._logger.debug("KDA: Otsu failed, adaptive succeeded")
                    break

            if result is None:
                if self._debug:
                    elapsed = (time.time() - t_start) * 1000
                    self._logger.debug(f"KDA: all methods failed [{elapsed:.0f}ms]")
                return None

            k, d, a = result
            elapsed = (time.time() - t_start) * 1000
            self._logger.info(f"KDA read: {k}/{d}/{a} [{elapsed:.0f}ms]")
            return result

        except Exception as e:
            if self._debug:
                self._logger.debug(f"KDA OCR error: {e}")
            return None

    # --- Detector debug observability -------------------------------------
    #
    # These ``*_with_details`` helpers mirror the live read / scene-classify
    # methods above but return rich payloads instead of just bool / tuple
    # answers. Used by the /detector debug page so the operator can see
    # every intermediate value the detector is making decisions from.
    #
    # The originals (``read_kda``, ``is_gameplay_screen``, ``is_overlay_open``)
    # are intentionally left untouched so the live KDA path keeps its exact
    # current behaviour and timing characteristics.

    def scene_classification_with_details(
        self,
        img_array: np.ndarray,
        crop_origin: tuple[int, int] = (0, 0),
    ) -> dict:
        """Run the gameplay/overlay heuristics and return all the
        intermediate numbers they used. Doesn't mutate state.

        Returns dict with:
            is_gameplay:         bool
            overlay_open:        bool
            hud_std, hud_mean:   ability-bar variance/mean
            portrait_std:        portrait + health-bar variance
            overlay_dark_ratio:  upper-region dark-pixel fraction (stage 1)
            overlay_bar_std:     KDA-bar std used in overlay stage 2
            kda_crop_std:        std of the KDA crop (early-out signal)
            thresholds:          dict of the constants in use, so the UI
                                 can render "<value> / <threshold>" pills.
        """
        ox, oy = crop_origin
        h, w = img_array.shape[:2]

        # --- Ability bar (HUD_CHECK_REGION) ---
        x1, y1, x2, y2 = HUD_CHECK_REGION
        x1s, y1s, x2s, y2s = x1 - ox, y1 - oy, x2 - ox, y2 - oy
        if y2s <= h and x2s <= w and x1s >= 0 and y1s >= 0:
            r = img_array[y1s:y2s, x1s:x2s]
            hud_std = float(np.std(r))
            hud_mean = float(np.mean(r))
        else:
            hud_std = 0.0
            hud_mean = 0.0

        # --- Portrait + health bar (GAMEPLAY_CHECK_REGION) ---
        x1, y1, x2, y2 = GAMEPLAY_CHECK_REGION
        x1s, y1s, x2s, y2s = x1 - ox, y1 - oy, x2 - ox, y2 - oy
        if y2s <= h and x2s <= w and x1s >= 0 and y1s >= 0:
            r = img_array[y1s:y2s, x1s:x2s]
            portrait_std = float(np.std(r))
        else:
            portrait_std = 0.0

        is_gameplay = (
            (hud_std >= HUD_MIN_STD and hud_mean >= HUD_MIN_MEAN)
            or portrait_std >= GAMEPLAY_CHECK_MIN_STD
        )

        # --- Overlay (store/scoreboard) two-stage check ---
        x1, y1, x2, y2 = OVERLAY_CHECK_REGION
        ovl = img_array[y1 - oy:y2 - oy, x1 - ox:x2 - ox]
        overlay_dark_ratio = float(np.mean(ovl < 40)) if ovl.size else 0.0

        kx1, ky1, kx2, ky2 = KDA_REGION
        kda_region = img_array[ky1 - oy:ky2 - oy, kx1 - ox:kx2 - ox]
        overlay_bar_std = float(np.std(kda_region)) if kda_region.size else 0.0
        kda_crop_std = overlay_bar_std  # same region — alias for clarity

        overlay_open = (
            overlay_dark_ratio > OVERLAY_DARK_THRESHOLD
            and overlay_bar_std < OVERLAY_BAR_VISIBLE_STD
        )

        return {
            "is_gameplay": bool(is_gameplay),
            "overlay_open": bool(overlay_open),
            "hud_std": hud_std,
            "hud_mean": hud_mean,
            "portrait_std": portrait_std,
            "overlay_dark_ratio": overlay_dark_ratio,
            "overlay_bar_std": overlay_bar_std,
            "kda_crop_std": kda_crop_std,
            "thresholds": {
                "hud_min_std": HUD_MIN_STD,
                "hud_min_mean": HUD_MIN_MEAN,
                "portrait_min_std": GAMEPLAY_CHECK_MIN_STD,
                "overlay_dark_threshold": OVERLAY_DARK_THRESHOLD,
                "overlay_bar_visible_std": OVERLAY_BAR_VISIBLE_STD,
            },
        }

    def read_kda_with_details(
        self,
        img: Image.Image,
        crop_origin: tuple[int, int] = (0, 0),
    ) -> dict:
        """Run the same KDA pipeline as ``read_kda()`` but collect every
        intermediate result for the detector debug page.

        Does NOT mutate ``self._last_digit_crops`` (so debug calls won't
        accidentally seed the template enrollment pool).

        Returns dict with:
            kda:             (k, d, a) | None
            crop:            PIL.Image of the raw KDA strip (24x92-ish)
            binary_8x:       PIL.Image of the binarized, bordered 8x crop,
                             or None if binarisation never produced enough
                             components.
            groups:          list of three dicts (K, D, A), each with
                             'label', 'digits', 'concatenated'. Each
                             digit entry has 'best', 'distance', 'method',
                             'verdict', 'top_n', 'margin'.
            failure_reason:  short string explaining None KDA, e.g.
                             'blank_crop', 'no_digits', 'group_failed',
                             'integer_parse_failed'. None on success.
            elapsed_ms:      total time spent inside the call.
        """
        result: dict = {
            "kda": None,
            "crop": None,
            "binary_8x": None,
            "groups": [],
            "failure_reason": None,
            "elapsed_ms": 0.0,
            # Geometry the dashboard needs to map bbox_binary_8x and
            # bbox_strip coords back to display pixels. Constant for now
            # but exposed so the client doesn't hardcode them.
            "binary_8x_meta": {
                "scale": 8,
                "border": 20,
            },
        }

        if not self.is_ready:
            result["failure_reason"] = "reader_not_ready"
            return result

        t_start = time.time()

        try:
            import cv2
            import pytesseract
            if self._tesseract_path:
                pytesseract.pytesseract.tesseract_cmd = self._tesseract_path

            ox, oy = crop_origin
            x1, y1, x2, y2 = KDA_REGION
            crop = img.crop((x1 - ox, y1 - oy, x2 - ox, y2 - oy))
            result["crop"] = crop
            gray = np.array(crop.convert("L"))

            crop_std = float(np.std(gray))
            if crop_std < 5:
                result["failure_reason"] = "blank_crop"
                result["elapsed_ms"] = (time.time() - t_start) * 1000
                return result

            # 8x upscale matches read_kda()'s pipeline.
            scale = 8
            gray_big = cv2.resize(
                gray,
                (gray.shape[1] * scale, gray.shape[0] * scale),
                interpolation=cv2.INTER_CUBIC,
            )
            # Same upscale on RGB so the binarizer can apply the
            # whiteness mask — see _binarize_with_diag for the rationale.
            rgb = np.array(crop.convert("RGB"))
            rgb_big = cv2.resize(
                rgb,
                (rgb.shape[1] * scale, rgb.shape[0] * scale),
                interpolation=cv2.INTER_CUBIC,
            )

            # Try Otsu first, adaptive fallback — same as read_kda().
            # Unlike read_kda we ALWAYS surface the binarization output
            # plus a per-component breakdown to the debug payload so the
            # detector page can see exactly what each thresholding pass
            # produced — invaluable when reads start failing.
            bordered = None
            digits = None
            chosen_method = None
            # Track per-method diagnostics so the page shows BOTH passes,
            # not just the one we ended up using.
            method_attempts = []
            for method in ("otsu", "adaptive"):
                attempt = self._binarize_with_diag(
                    gray_big, method, rgb_big=rgb_big)
                method_attempts.append(attempt)
                # First method that yields 3+ digits wins, same as read_kda.
                if (bordered is None
                        and attempt["bordered"] is not None
                        and len(attempt["digits"]) >= 3):
                    bordered = attempt["bordered"]
                    digits = attempt["digits"]
                    chosen_method = method

            # Expose the chosen method's binary image (or, if neither
            # passed, Otsu's so the page can still show what we got).
            display_attempt = next(
                (a for a in method_attempts if a["method"] == chosen_method),
                method_attempts[0] if method_attempts else None,
            )
            if display_attempt and display_attempt["bordered"] is not None:
                result["binary_8x"] = Image.fromarray(
                    display_attempt["bordered"])

            # Always include the full per-method diagnostic table.
            result["binarization"] = {
                "chosen_method": chosen_method,
                "methods": [
                    {
                        "method": a["method"],
                        "white_pixel_ratio": a["white_pixel_ratio"],
                        "n_components_total": a["n_components_total"],
                        "components": a["components"],
                    }
                    for a in method_attempts
                ],
            }

            if bordered is None or not digits:
                result["failure_reason"] = "no_digits"
                result["elapsed_ms"] = (time.time() - t_start) * 1000
                return result

            # Group components into K/D/A by widest x-gaps.
            digits.sort(key=lambda c: c[0])
            gaps = []
            for j in range(len(digits) - 1):
                right_edge = digits[j][0] + digits[j][2]
                left_edge = digits[j + 1][0]
                gaps.append((left_edge - right_edge, j))

            if len(gaps) < 2:
                result["failure_reason"] = "insufficient_gaps"
                result["elapsed_ms"] = (time.time() - t_start) * 1000
                return result

            gaps.sort(reverse=True)
            split_indices = sorted([gaps[0][1], gaps[1][1]])
            groups = [
                digits[: split_indices[0] + 1],
                digits[split_indices[0] + 1 : split_indices[1] + 1],
                digits[split_indices[1] + 1 :],
            ]
            if not all(groups):
                result["failure_reason"] = "empty_group"
                result["elapsed_ms"] = (time.time() - t_start) * 1000
                return result

            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            kda_values: list[int] = []
            had_failure = False

            for idx, group in enumerate(groups):
                label = ("K", "D", "A")[idx]

                # Union bbox of every component in this group, in the
                # bordered-8x binary's pixel coords. The dashboard uses
                # this to draw a colored overlay on top of binary_8x and
                # to crop a per-group preview without an extra server
                # round trip. We also emit the same bbox translated back
                # into the raw KDA-strip coord system (i.e. relative to
                # the unscaled, un-bordered crop) so the dashboard can
                # overlay on `crop` directly too.
                xs = [c[0] for c in group]
                ys = [c[1] for c in group]
                rights  = [c[0] + c[2] for c in group]
                bottoms = [c[1] + c[3] for c in group]
                bx_8x, by_8x = min(xs), min(ys)
                bw_8x = max(rights) - bx_8x
                bh_8x = max(bottoms) - by_8x
                # Strip-space mapping inverts the 8x upscale + 20px border.
                bx_s = (bx_8x - 20) / 8.0
                by_s = (by_8x - 20) / 8.0
                bw_s = bw_8x / 8.0
                bh_s = bh_8x / 8.0

                group_payload: dict = {
                    "label": label,
                    "digits": [],
                    "concatenated": None,
                    "bbox_binary_8x": [int(bx_8x), int(by_8x),
                                       int(bw_8x), int(bh_8x)],
                    "bbox_strip": [round(bx_s, 2), round(by_s, 2),
                                   round(bw_s, 2), round(bh_s, 2)],
                }

                matched_digits: list[Optional[str]] = []

                for comp in group:
                    cx, cy, cw, ch = comp[0], comp[1], comp[2], comp[3]
                    comp_crop = bordered[cy : cy + ch, cx : cx + cw]

                    digit_entry: dict = {
                        "best": None,
                        "distance": None,
                        "method": "failed",
                        "verdict": "failed",
                        "top_n": [],
                        "margin": 0.0,
                        "crop": None,
                        "time_ms": 0.0,
                    }

                    if comp_crop.size == 0:
                        matched_digits.append(None)
                        group_payload["digits"].append(digit_entry)
                        continue

                    digit_entry["crop"] = Image.fromarray(comp_crop)

                    digit_str: Optional[str] = None
                    _digit_t0 = time.perf_counter()

                    # --- Template match path ---
                    if self._digit_matcher and self._digit_matcher.is_loaded:
                        std_crop = self._digit_matcher.prepare_candidate(
                            comp_crop)
                        details = self._digit_matcher.match_with_details(
                            std_crop)
                        digit_entry["top_n"] = [
                            (d, round(dist, 4))
                            for d, dist in details["scores"][:5]
                        ]
                        digit_entry["margin"] = round(details["margin"], 4)
                        if details["best"] is not None:
                            digit_entry["best"] = details["best"][0]
                            digit_entry["distance"] = round(
                                details["best"][1], 4)
                        digit_entry["verdict"] = details["verdict"]

                        if details["verdict"] == "accepted":
                            digit_str = details["best"][0]
                            digit_entry["method"] = "template"

                    # --- Tesseract fallback ---
                    if digit_str is None and self._ocr_available:
                        comp_padded = cv2.copyMakeBorder(
                            comp_crop, 20, 20, 20, 20,
                            cv2.BORDER_CONSTANT, value=0,
                        )
                        thickened = cv2.dilate(
                            comp_padded, kernel, iterations=1)
                        inv_dilated = 255 - thickened
                        inv_original = 255 - comp_padded
                        for img_variant, psm in (
                            (inv_dilated, 10), (inv_dilated, 8),
                            (inv_original, 10), (inv_original, 8),
                        ):
                            candidate = pytesseract.image_to_string(
                                Image.fromarray(img_variant),
                                config=(
                                    f"--psm {psm} "
                                    "-c tessedit_char_whitelist=0123456789"
                                ),
                            ).strip()
                            if candidate.isdigit() and len(candidate) == 1:
                                digit_str = candidate
                                break
                            if candidate and candidate[0].isdigit():
                                digit_str = candidate[0]
                                break
                        if digit_str is not None:
                            digit_entry["best"] = digit_str
                            digit_entry["method"] = "tesseract"
                            digit_entry["verdict"] = "tesseract_fallback"

                    digit_entry["time_ms"] = round(
                        (time.perf_counter() - _digit_t0) * 1000.0, 2
                    )
                    matched_digits.append(digit_str)
                    group_payload["digits"].append(digit_entry)

                # Concatenate matched digits or record failure.
                if any(d is None for d in matched_digits):
                    had_failure = True
                else:
                    try:
                        value = int("".join(matched_digits))
                        kda_values.append(value)
                        group_payload["concatenated"] = "".join(matched_digits)
                    except (ValueError, TypeError):
                        had_failure = True

                result["groups"].append(group_payload)

            if had_failure or len(kda_values) != 3:
                result["failure_reason"] = "group_failed"
            else:
                result["kda"] = tuple(kda_values)

        except Exception as e:
            result["failure_reason"] = f"exception:{type(e).__name__}:{e}"

        result["elapsed_ms"] = (time.time() - t_start) * 1000
        return result

    def enroll_last_read(self) -> int:
        """Save the digit crops from the most recent successful read as new
        templates in the digit matcher library.  Callers should only invoke
        this after their own sanity checks (no-decrease, max-jump) have
        confirmed the read is real.

        Returns the number of new templates written.
        """
        if self._digit_matcher is None or not self._last_digit_crops:
            return 0

        added = 0
        for digit_str, crop in self._last_digit_crops:
            if self._digit_matcher.add_template(digit_str, crop):
                added += 1
        self._last_digit_crops = None
        return added

    def discard_last_read(self):
        """Clear the cached digit crops without enrolling them."""
        self._last_digit_crops = None

    # --- Debug helpers (for read_kda_with_details) -----------------------

    def _binarize_with_diag(
        self,
        gray_big: np.ndarray,
        method: str,
        rgb_big: Optional[np.ndarray] = None,
    ) -> dict:
        """Same binarization + CC pass as ``_binarize_and_find_digits``
        but returns the FULL diagnostic — every component with its
        classification (digit / icon / noise) — so the detector debug
        page can render a table even when no digits were kept.

        When ``rgb_big`` is supplied, the Otsu/adaptive output is ANDed
        with a whiteness mask (min(R,G,B) >= WHITENESS_THRESHOLD) so
        bright COLORED bleed-through from the partially-transparent
        KDA bar gets rejected.  KDA digits are pure white by design;
        anything that doesn't satisfy "all three channels are bright"
        is background noise.

        Returns:
            {
              "method": "otsu" | "adaptive",
              "bordered": np.ndarray | None,    # binarized 8x with 20px border
              "white_pixel_ratio": float,
              "n_components_total": int,
              "components": [
                  {"x", "y", "w", "h", "area", "aspect",
                   "classification": "digit" | "icon" | "noise"},
                  ...
              ],
              "digits": [(x, y, w, h, area, label_idx), ...]  # kept ones
            }
        """
        import cv2

        if method == "otsu":
            _, bw = cv2.threshold(
                gray_big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU,
            )
        else:
            bw = cv2.adaptiveThreshold(
                gray_big, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, blockSize=51, C=-10,
            )
        if rgb_big is not None:
            # Reject saturated colors only. KDA digits + icons are all
            # white/gray (saturation ~0), so this mask is a no-op on
            # them. Colored bleed-through (red effects, blue water, etc.)
            # has high saturation and gets nuked.
            ch_max = np.max(rgb_big, axis=2).astype(np.int16)
            ch_min = np.min(rgb_big, axis=2).astype(np.int16)
            saturation = ch_max - ch_min
            desaturated_mask = (
                saturation <= KDA_SATURATION_THRESHOLD
            ).astype(np.uint8) * 255
            bw = cv2.bitwise_and(bw, desaturated_mask)
        bordered = cv2.copyMakeBorder(
            bw, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=0,
        )
        white_pixel_ratio = float((bordered > 128).mean())

        num_labels, _labels, stats, _ = cv2.connectedComponentsWithStats(
            bordered, connectivity=8,
        )

        # Two-pass classification — KEEP IN SYNC with
        # _binarize_and_find_digits below.  First pass gathers
        # non-noise blobs, second pass classifies them.  We need the
        # two passes because the strongest icon discriminator uses
        # the max y across surviving blobs as a digit-baseline
        # reference (see the y_off prong in the live pipeline).
        components_raw: list[tuple] = []
        for i in range(1, num_labels):
            x, y, w, h, area = stats[i]
            aspect = (w / h) if h > 0 else 0.0
            if area < 150 or w < 20 or h < 40:
                # Noise gets recorded immediately so the debug panel
                # still shows it.
                components_raw.append((int(x), int(y), int(w), int(h),
                                       int(area), float(aspect),
                                       int(i), "noise"))
            else:
                components_raw.append((int(x), int(y), int(w), int(h),
                                       int(area), float(aspect),
                                       int(i), None))  # classify later

        # Baseline = max y_top across non-noise blobs.  Icons sit
        # ~10-15 px above this in 8x space.
        baseline_max_y = max(
            (c[1] for c in components_raw if c[7] is None),
            default=0,
        )

        components = []
        digits = []
        for (x, y, w, h, area, aspect, i, pre_cls) in components_raw:
            if pre_cls == "noise":
                cls = "noise"
            elif (
                h > 88
                or (h > 72 and aspect > 0.85)
                or ((baseline_max_y - y) > 8 and aspect > 0.75)
            ):
                cls = "icon"
            else:
                cls = "digit"
                digits.append((x, y, w, h, area, i))
            components.append({
                "x": x, "y": y,
                "w": w, "h": h,
                "area": area,
                "aspect": round(aspect, 3),
                "classification": cls,
            })

        # Order left-to-right so the page renders them in a meaningful order.
        components.sort(key=lambda c: c["x"])

        return {
            "method": method,
            "bordered": bordered,
            "white_pixel_ratio": round(white_pixel_ratio, 4),
            "n_components_total": num_labels - 1,
            "components": components,
            "digits": digits,
        }

    # --- Private helpers ---------------------------------------------------

    def _binarize_and_find_digits(
        self,
        gray_big: np.ndarray,
        method: str = "otsu",
        rgb_big: Optional[np.ndarray] = None,
    ) -> Optional[tuple]:
        """Binarize + connected-component analysis.

        When ``rgb_big`` is provided, the Otsu/adaptive output is ANDed
        with a whiteness mask (min(R,G,B) >= WHITENESS_THRESHOLD) before
        component extraction.  This rejects bright COLORED bleed-through
        from the partially-transparent KDA bar — red enemy effects, blue
        water, yellow cooldown rings — that would otherwise survive
        Otsu's threshold and produce false digits.  KDA digits are pure
        white by design, so requiring "all three channels are bright"
        is a sharp discriminator with no false negatives.

        Returns (bordered, labels, stats, digits) — a tuple suitable for
        handing to ``_ocr_from_binarized`` — or None if fewer than three
        digit-sized components were found.
        """
        import cv2

        if method == "otsu":
            _, bw = cv2.threshold(
                gray_big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
            )
        else:
            bw = cv2.adaptiveThreshold(
                gray_big,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                blockSize=51,
                C=-10,
            )

        if rgb_big is not None:
            # See _binarize_with_diag for full rationale. Mask out
            # saturated colored pixels (KDA content is white/gray).
            ch_max = np.max(rgb_big, axis=2).astype(np.int16)
            ch_min = np.min(rgb_big, axis=2).astype(np.int16)
            saturation = ch_max - ch_min
            desaturated_mask = (
                saturation <= KDA_SATURATION_THRESHOLD
            ).astype(np.uint8) * 255
            bw = cv2.bitwise_and(bw, desaturated_mask)

        bordered = cv2.copyMakeBorder(
            bw, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=0
        )

        num_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            bordered, connectivity=8
        )

        # Two-pass classification.  First pass: collect every non-noise
        # blob.  Second pass: drop icons.  We can't classify in one pass
        # because the strongest icon discriminator is the y-position of
        # the blob relative to the other surviving blobs in the same
        # frame — icons in the Smite 2 HUD sit visibly higher than
        # digits, and that gap is more reliable than aspect alone.
        candidates: list[tuple] = []
        for i in range(1, num_labels):
            x, y, w, h, area = stats[i]
            # Drop noise: too-small or too-narrow blobs.
            if area < 150 or w < 20 or h < 40:
                continue
            candidates.append((int(x), int(y), int(w), int(h),
                               int(area), int(i)))

        # Compute the "digit baseline" — the y_top of the lowest
        # surviving component (largest y, since y=0 is at the top).
        # Real digits sit on a common baseline; icons sit ~10-15 px
        # higher in the cell in 8x-upscaled space.
        max_y = max((c[1] for c in candidates), default=0)

        digits = []
        for (x, y, w, h, area, i) in candidates:
            # Drop icons (sword / skull / trophy / hand).  Three-prong filter:
            #   1. h > 88 — catches very tall icons in some compositions
            #      (~100 px in 8x space, ~12.5 raw px).
            #   2. h > 72 AND aspect > 0.85 — old safety net for
            #      borderline icons whose aspect is unambiguous.
            #   3. (max_y - y) > 8 AND aspect > 0.75 — the new
            #      discriminator that catches the recording-side icons
            #      that previously slipped through.  In 4K-downscaled
            #      VODs the HUD lays out as [sword] K [skull] D [trophy]
            #      A; the icons land at y=132-135 while digits land at
            #      y=145-147 (8x space), so they sit consistently ~10-15
            #      px above the digit baseline.  The aspect>0.75 guard
            #      stops us from dropping a "1" that happens to be a few
            #      px higher than "0" — "1" lives at aspect ~0.64, well
            #      below 0.75.
            #
            # Aspect 0.85 history: originally 0.78, raised to 0.85 after
            # the "4" glyph (aspect ~0.79, sits ON the baseline like
            # other digits) was being false-positively caught.  The new
            # y-position prong picks up where the aspect prong now
            # leaves off.
            aspect = w / h if h > 0 else 0
            is_icon = (
                h > 88
                or (h > 72 and aspect > 0.85)
                or ((max_y - y) > 8 and aspect > 0.75)
            )
            if is_icon:
                if self._debug and self._logger:
                    self._logger.debug(
                        f"KDA: dropped icon-shaped blob "
                        f"(x={x}, y={y}, w={w}, h={h}, aspect={aspect:.2f}, "
                        f"y_off={max_y - y})"
                    )
                continue
            digits.append((x, y, w, h, area, i))

        if len(digits) < 3:
            return None

        return bordered, labels, stats, digits

    def _ocr_from_binarized(
        self,
        bordered: np.ndarray,
        labels: np.ndarray,
        stats: np.ndarray,
        digits: list,
        t_start: float,
    ) -> Optional[tuple[int, int, int]]:
        """Group digit components into K / D / A and recognize each digit.

        Recognition is per-component: try the template matcher first
        (fast, handles ~95% of cases once the library is warm), then
        fall back to Tesseract PSM 10 (single char) for stragglers.
        If any single component can't be identified the whole read fails.
        """
        import cv2
        import pytesseract

        digits.sort(key=lambda c: c[0])

        # Group by the two widest x-gaps (icon gaps between K|D|A).
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
            digits[: split_indices[0] + 1],
            digits[split_indices[0] + 1 : split_indices[1] + 1],
            digits[split_indices[1] + 1 :],
        ]

        if not all(groups):
            return None

        kda_values: list[int] = []
        digit_crops: list[tuple[str, np.ndarray]] = []
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

        for idx, group in enumerate(groups):
            group_label = ("K", "D", "A")[idx]

            matched_digits = []
            for comp in group:
                cx, cy, cw, ch = comp[0], comp[1], comp[2], comp[3]
                comp_crop = bordered[cy : cy + ch, cx : cx + cw]
                if comp_crop.size == 0:
                    matched_digits.append(None)
                    continue

                # Template match first.
                digit_str: Optional[str] = None
                if self._digit_matcher and self._digit_matcher.is_loaded:
                    std_crop = self._digit_matcher.prepare_candidate(comp_crop)
                    result = self._digit_matcher.match(std_crop)
                    if result is not None:
                        digit_str, _dist = result
                        digit_crops.append((digit_str, comp_crop))

                # Tesseract on the single component for any holdout.
                if digit_str is None and self._ocr_available:
                    comp_padded = cv2.copyMakeBorder(
                        comp_crop, 20, 20, 20, 20,
                        cv2.BORDER_CONSTANT, value=0,
                    )
                    try:
                        text = pytesseract.image_to_string(
                            comp_padded,
                            config="--psm 10 -c tessedit_char_whitelist=0123456789",
                        ).strip()
                        if text and text.isdigit() and len(text) == 1:
                            digit_str = text
                    except Exception:
                        pass

                if digit_str is None:
                    return None
                matched_digits.append(digit_str)

            if None in matched_digits:
                return None

            try:
                kda_values.append(int("".join(matched_digits)))
            except (ValueError, TypeError):
                return None

        if len(kda_values) != 3:
            return None

        self._last_digit_crops = digit_crops
        return tuple(kda_values)
