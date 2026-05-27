"""
God Portrait Matcher
=====================
Identifies which Smite 2 god the player is using by comparing
the in-game god portrait (cropped from OBS screenshots) against
a library of reference icons downloaded from the tracker.gg CDN.

Detection method:
  1. Crop the god portrait region from the 1920x1080 game screenshot
  2. Resize reference icons to match the portrait crop size
  3. Compare using OpenCV histogram correlation (fast, lighting-robust)
  4. Return the best match above a confidence threshold

The portrait region sits in the bottom-center HUD, directly below the
K/D/A bar that the KillDeathDetector already reads.

Usage:
    matcher = GodMatcher(icons_dir="data/god_icons")
    matcher.load_icons()
    god_name, confidence = matcher.identify(screenshot_image)
"""

import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image

# --- Portrait crop region (1920x1080 game source) ---
# The god portrait icon sits below the KDA bar.
# KDA bar: (625, 905, 725, 932).
# God portrait: approximately (635, 942) top-left, 80x80 px.
# We use a slightly padded region and let histogram matching
# handle minor alignment differences.
# Loaded from data/detector_regions.json (fallback to historical default).
from core.detector_regions import load_regions as _load_detector_regions
PORTRAIT_REGION = _load_detector_regions()["portrait"]  # (x1, y1, x2, y2)

# Icon comparison size — all icons and portrait crops are resized to this.
MATCH_SIZE = (64, 64)

# Minimum correlation to accept a match (absolute threshold).
# 0.90+ = very high confidence (same god for sure)
# 0.80-0.90 = likely correct but verify
# Below 0.80 = probably wrong (different lighting, skin, overlay, etc.)
MIN_CONFIDENCE = 0.80

# Margin-based acceptance: if absolute score is below MIN_CONFIDENCE but
# above MIN_CONFIDENCE_MARGIN and the gap to the second-best match exceeds
# MARGIN_GAP, accept anyway.  Handles gods whose color histograms
# don't correlate as strongly against the in-game art:
#   - Ymir's ice-blue palette scores ~0.73 vs a 0.47 gap to runner-up
#   - Atlas with animated gold/glow effects scores ~0.55 vs a 0.31
#     gap to runner-up (May 2026 — bumped floor from 0.60 → 0.50 to
#     let animated gods through; MARGIN_GAP still guards against
#     false positives by requiring a clear winner over runner-up).
MIN_CONFIDENCE_MARGIN = 0.50   # Floor for margin-based acceptance
MARGIN_GAP = 0.20              # Required gap between #1 and #2

# Number of histogram bins per channel for comparison.
HIST_BINS = 16


class GodMatcher:
    """
    Matches in-game god portraits against reference icon images.

    Each god can carry multiple fingerprints — typically one from the
    in-game tracker.gg / wiki CDN art (the original "what does the god
    look like in the bottom-of-HUD portrait" reference) and, optionally,
    one from the OBS overlay's custom god art that may sit on top of the
    in-game portrait during recordings.  ``identify()`` picks the best
    score per god and then ranks gods, so margin-based acceptance still
    behaves correctly when both fingerprints are present for the same
    god.

    Preloads and caches histogram fingerprints for all reference icons
    so each frame comparison is fast (~1ms for 64 icons; overlays add
    a similar amount of work per call).
    """

    def __init__(
        self,
        icons_dir=None,
        overlay_icons_dir=None,
        reference_icons_dir=None,
    ):
        """
        Args:
            icons_dir: directory of in-game-portrait reference icons.
                Defaults to ``<repo>/data/god_icons`` — what the live
                killdetector reads (game capture source, pre-overlay).
            overlay_icons_dir: optional directory of OBS-overlay custom
                god icons.  When set (typically for offline VOD scans),
                files in this folder whose stem matches a known god
                from ``icons_dir`` are also loaded as alternate
                fingerprints for that god.  Skin variants / unknown
                stems are ignored.  Pass ``None`` to skip — that's the
                live plugin's behavior.
            reference_icons_dir: optional directory of pixel-accurate
                reference portrait crops captured from real recordings
                (typically by ``tools/capture_god_reference.py``).
                These come straight out of the same decode pipeline
                that produces the recording, so they correlate near
                1.0 against future scans of that god — the cure for
                CUDA-vs-software decode score drift on borderline
                gods.  Same skin-filter rule as overlay_icons_dir:
                only files whose stem matches a known base-library
                god are kept.  Pass ``None`` to skip.
        """
        if icons_dir is None:
            icons_dir = Path(__file__).parent.parent / "data" / "god_icons"
        self._icons_dir = Path(icons_dir)
        self._overlay_icons_dir = (
            Path(overlay_icons_dir) if overlay_icons_dir is not None else None
        )
        self._reference_icons_dir = (
            Path(reference_icons_dir) if reference_icons_dir is not None else None
        )
        # Storage: {god_name: [hist, hist, ...]}.  Each god gets a list
        # of fingerprints; one in-game-CDN entry, optionally one
        # custom-overlay entry, optionally one capture-from-recording
        # reference entry.  identify() takes the max correlation
        # across each god's list before ranking gods.
        self._icon_hists: dict = {}
        # Parallel storage tagging each fingerprint with its source
        # library: "base" (data/god_icons), "overlay" (Custom God Icons),
        # or "reference" (Portrait_Source). Indices line up 1:1 with
        # ``_icon_hists`` so the source for the i-th fingerprint of god
        # X is ``_icon_sources[X][i]``. Used by
        # ``identify_top_n_with_sources()`` so the debug page can show
        # which library produced the winning score.
        self._icon_sources: dict = {}
        self._loaded = False

    def load_icons(self):
        """Load all reference icons and precompute their histograms.

        Loads two libraries in order:

          1. The base library at ``self._icons_dir`` (in-game portrait
             references).  Always loaded.
          2. The overlay library at ``self._overlay_icons_dir`` if it
             was passed and exists.  Only files whose stem maps to a god
             we already have in the base library are added — that
             filters skin variants and any unrelated files in the
             folder without us needing a separate skin-to-god mapping.

        Either library missing is non-fatal: the load succeeds as long
        as the base library produces at least one fingerprint.
        """
        self._icon_hists.clear()
        self._icon_sources.clear()

        if not self._icons_dir.exists():
            print(f"[GodMatcher] Icons directory not found: {self._icons_dir}")
            print("[GodMatcher] Run: python download_god_icons.py")
            return False

        # --- 1) Base library (in-game CDN / wiki art) ------------------
        base_count = self._load_dir_into_hists(
            self._icons_dir,
            allowed_god_names=None,  # accept any
            source="base",
        )

        # --- 2) Overlay library (custom OBS art) -----------------------
        # Filter to gods we've already seen in the base library so skins
        # ("Ymir Frostbringer.png") and other extras get silently
        # skipped.  No skin-to-base-god mapping required.
        overlay_count = 0
        if (
            self._overlay_icons_dir is not None
            and self._overlay_icons_dir.exists()
        ):
            known = set(self._icon_hists.keys())
            overlay_count = self._load_dir_into_hists(
                self._overlay_icons_dir,
                allowed_god_names=known,
                source="overlay",
            )

        # --- 3) Reference library (capture-from-recording fingerprints) -
        # Same skin-filter rule.  These are pixel-accurate portrait
        # crops from real recordings, captured via
        # ``tools/capture_god_reference.py`` or
        # ``tools/sort_unknowns.py``, and live in their own folder
        # (default ``<repo>/Portrait_Source``) so they don't mix with
        # the user's decorative ``Custom God Icons/`` overlay art.
        #
        # Two valid layouts inside the reference folder:
        #
        #   Portrait_Source/Vulcan.png
        #     ↳ flat: stem maps to god name (one fingerprint per god)
        #
        #   Portrait_Source/Vulcan/some-recording.png
        #   Portrait_Source/Vulcan/another-recording.png
        #     ↳ nested: subfolder name is the god, every image inside
        #       becomes another fingerprint.  Lets a single god carry
        #       both an in-game-portrait variant AND each custom-overlay
        #       variant the user may have used at different times.
        #
        # Subfolders whose name starts with ``_`` (e.g. ``_capture_audit``)
        # are intentionally skipped so audit / scratch data sitting
        # alongside the references doesn't get loaded as icons.
        reference_count = 0
        if (
            self._reference_icons_dir is not None
            and self._reference_icons_dir.exists()
        ):
            known = set(self._icon_hists.keys())
            reference_count = self._load_reference_dir(
                self._reference_icons_dir,
                allowed_god_names=known,
            )

        self._loaded = base_count > 0
        bits = [f"{base_count} base"]
        if overlay_count > 0:
            bits.append(f"{overlay_count} overlay")
        if reference_count > 0:
            bits.append(f"{reference_count} reference")
        print(
            f"[GodMatcher] Loaded "
            f"{' + '.join(bits)} god icon fingerprints"
        )
        return self._loaded

    def _load_reference_dir(
        self,
        directory: Path,
        allowed_god_names: Optional[set],
    ) -> int:
        """Load the Portrait_Source-style reference library.

        Top-level files are loaded with the same stem-to-god-name rule
        as the base / overlay libraries.  In addition, every direct
        subfolder whose name maps to a known god is walked and ALL
        images inside become extra fingerprints for that god — letting
        a single god carry multiple reference variants (e.g. an
        in-game-portrait capture *and* one or more custom-overlay
        captures).  Subfolders prefixed with ``_`` are skipped so audit
        / scratch folders don't pollute the library.
        """
        loaded = self._load_dir_into_hists(
            directory, allowed_god_names, source="reference",
        )

        for sub in sorted(
            (p for p in directory.iterdir() if p.is_dir()),
            key=lambda p: p.name.lower(),
        ):
            if sub.name.startswith("_"):
                continue
            god_name = self._slug_to_name(sub.name)
            if (
                allowed_god_names is not None
                and god_name not in allowed_god_names
            ):
                continue
            # Every image inside the subfolder becomes another
            # fingerprint for ``god_name``.  Use a one-name allow-set
            # for the inner load so any unrelated stems would be
            # silently ignored — but also, since we explicitly
            # set the god_name from the FOLDER not the FILE, we just
            # iterate files directly here.
            for path in sorted(sub.iterdir(), key=lambda p: p.name.lower()):
                if not path.is_file() or path.suffix.lower() not in (".png", ".jpg"):
                    continue
                try:
                    img_raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
                    if img_raw is None:
                        continue
                    img, alpha_mask = self._split_bgr_alpha(img_raw)
                    self._icon_hists.setdefault(god_name, []).append(
                        self._compute_hist(img, alpha_mask=alpha_mask)
                    )
                    self._icon_sources.setdefault(god_name, []).append(
                        "reference"
                    )
                    loaded += 1
                except Exception as e:
                    print(f"[GodMatcher] Error loading {path.name}: {e}")
        return loaded

    def _load_dir_into_hists(
        self,
        directory: Path,
        allowed_god_names: Optional[set],
        source: str = "base",
    ) -> int:
        """Load every .png/.jpg in ``directory`` and append a fingerprint
        per file to ``self._icon_hists``.

        ``allowed_god_names``: if set, only files whose stem maps (via
        ``_slug_to_name``) to a name in this set are kept; everything
        else is silently skipped.  Used to filter the overlay library
        down to known gods only.

        ``source``: tag stored in parallel in ``_icon_sources`` so
        debug callers can tell which library each fingerprint came
        from. One of "base" | "overlay" | "reference".

        Returns the count of fingerprints actually loaded (skipped files
        and decode failures don't count).
        """
        # Load both .png and .jpg.
        icon_files = (
            list(directory.glob("*.png")) + list(directory.glob("*.jpg"))
        )

        # Deduplicate by stem within this directory: if both formats
        # exist for the same name, prefer the larger file (proxy for
        # higher resolution / better quality source art).  This is a
        # per-directory dedupe — overlay files are independent of base
        # files at this stage, they're matched up later via god_name.
        by_stem: dict = {}
        for p in icon_files:
            if p.stem not in by_stem:
                by_stem[p.stem] = p
            else:
                existing = by_stem[p.stem]
                if p.stat().st_size > existing.stat().st_size:
                    by_stem[p.stem] = p

        loaded = 0
        for path in sorted(by_stem.values(), key=lambda p: p.stem):
            god_name = self._slug_to_name(path.stem)
            if (
                allowed_god_names is not None
                and god_name not in allowed_god_names
            ):
                continue
            try:
                img_raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
                if img_raw is None:
                    continue
                img, alpha_mask = self._split_bgr_alpha(img_raw)
                self._icon_hists.setdefault(god_name, []).append(
                    self._compute_hist(img, alpha_mask=alpha_mask)
                )
                self._icon_sources.setdefault(god_name, []).append(source)
                loaded += 1
            except Exception as e:
                print(f"[GodMatcher] Error loading {path.name}: {e}")

        return loaded

    def identify(
        self,
        screenshot: Image.Image,
        crop_origin: tuple[int, int] = (0, 0),
    ) -> tuple[Optional[str], float]:
        """
        Identify the god from a game screenshot.

        Args:
            screenshot: PIL Image of either the full 1920x1080 game screen
                (default) or a server-side-cropped strip of it.
            crop_origin: (x, y) offset that ``screenshot`` was cropped from
                in the original 1920x1080 frame.  Defaults to (0, 0) for
                the live-plugin path.  The VOD detector passes
                ``(VOD_CROP_X, VOD_CROP_Y)`` so PORTRAIT_REGION still
                indexes the right pixels inside the cropped strip.

        Returns:
            (god_name, confidence) — the best match and its correlation score.
            Returns (None, 0.0) if no match found above threshold.
        """
        if not self._loaded or not self._icon_hists:
            return None, 0.0

        # Crop the portrait region (translated by crop_origin so the same
        # absolute PORTRAIT_REGION coords work against either the full
        # frame or a pre-cropped strip).
        cx, cy = crop_origin
        region = (
            PORTRAIT_REGION[0] - cx,
            PORTRAIT_REGION[1] - cy,
            PORTRAIT_REGION[2] - cx,
            PORTRAIT_REGION[3] - cy,
        )
        portrait = screenshot.crop(region)

        # Convert to OpenCV format and resize
        portrait_cv = cv2.cvtColor(np.array(portrait), cv2.COLOR_RGB2BGR)
        portrait_cv = cv2.resize(portrait_cv, MATCH_SIZE, interpolation=cv2.INTER_AREA)

        # Compute histogram for the portrait
        portrait_hist = self._compute_hist(portrait_cv)

        # For each god, take the MAX correlation across all of that
        # god's fingerprints (base + optional overlay).  This is the
        # key piece that makes dual-library matching work cleanly —
        # the better-fitting fingerprint wins for that god, and we
        # still rank distinct gods against each other for margin.
        best_name = None
        best_score = -1.0
        second_score = -1.0

        for god_name, hist_list in self._icon_hists.items():
            god_score = -1.0
            for icon_hist in hist_list:
                s = cv2.compareHist(
                    portrait_hist, icon_hist, cv2.HISTCMP_CORREL,
                )
                if s > god_score:
                    god_score = s
            if god_score > best_score:
                second_score = best_score
                best_score = god_score
                best_name = god_name
            elif god_score > second_score:
                second_score = god_score

        # Accept if above absolute threshold
        if best_score >= MIN_CONFIDENCE:
            return best_name, best_score

        # Accept if above margin floor AND gap to runner-up is large enough
        # (handles gods with lower histogram correlation like Ymir's ice palette)
        if (best_score >= MIN_CONFIDENCE_MARGIN
                and (best_score - second_score) >= MARGIN_GAP):
            return best_name, best_score

        return None, best_score

    def identify_top_n(
        self,
        screenshot: Image.Image,
        n: int = 3,
        crop_origin: tuple[int, int] = (0, 0),
    ) -> list[tuple[str, float]]:
        """
        Return top N matches for debugging/verification.

        ``crop_origin`` works the same as in ``identify()``.

        Returns:
            List of (god_name, confidence) tuples, sorted by confidence descending.
        """
        if not self._loaded or not self._icon_hists:
            return []

        cx, cy = crop_origin
        region = (
            PORTRAIT_REGION[0] - cx,
            PORTRAIT_REGION[1] - cy,
            PORTRAIT_REGION[2] - cx,
            PORTRAIT_REGION[3] - cy,
        )
        portrait = screenshot.crop(region)
        portrait_cv = cv2.cvtColor(np.array(portrait), cv2.COLOR_RGB2BGR)
        portrait_cv = cv2.resize(portrait_cv, MATCH_SIZE, interpolation=cv2.INTER_AREA)
        portrait_hist = self._compute_hist(portrait_cv)

        # Same per-god max-then-rank strategy as identify().
        scores = []
        for god_name, hist_list in self._icon_hists.items():
            god_score = -1.0
            for icon_hist in hist_list:
                s = cv2.compareHist(
                    portrait_hist, icon_hist, cv2.HISTCMP_CORREL,
                )
                if s > god_score:
                    god_score = s
            scores.append((god_name, god_score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:n]

    def identify_top_n_with_sources(
        self,
        screenshot: Image.Image,
        n: int = 3,
        crop_origin: tuple[int, int] = (0, 0),
    ) -> list[tuple[str, float, str]]:
        """Like ``identify_top_n()`` but also returns the source library
        ("base" | "overlay" | "reference") that produced the winning
        score for each god.

        Returns a list of ``(god_name, score, source)`` tuples, sorted
        by score descending. Used by the detector debug page so the
        operator can tell whether the match came from the wiki art,
        their custom OBS art, or a captured recording fingerprint —
        useful for diagnosing "why is confidence 0.55 — is it matching
        the wrong library?"

        The original ``identify_top_n()`` is left untouched so any
        existing caller keeps its current return shape.
        """
        if not self._loaded or not self._icon_hists:
            return []

        cx, cy = crop_origin
        region = (
            PORTRAIT_REGION[0] - cx,
            PORTRAIT_REGION[1] - cy,
            PORTRAIT_REGION[2] - cx,
            PORTRAIT_REGION[3] - cy,
        )
        portrait = screenshot.crop(region)
        portrait_cv = cv2.cvtColor(np.array(portrait), cv2.COLOR_RGB2BGR)
        portrait_cv = cv2.resize(
            portrait_cv, MATCH_SIZE, interpolation=cv2.INTER_AREA,
        )
        portrait_hist = self._compute_hist(portrait_cv)

        scored: list[tuple[str, float, str]] = []
        for god_name, hist_list in self._icon_hists.items():
            sources_for_god = self._icon_sources.get(
                god_name, ["base"] * len(hist_list)
            )
            best_score = -1.0
            best_source = "base"
            for icon_hist, src in zip(hist_list, sources_for_god):
                s = cv2.compareHist(
                    portrait_hist, icon_hist, cv2.HISTCMP_CORREL,
                )
                if s > best_score:
                    best_score = s
                    best_source = src
            scored.append((god_name, best_score, best_source))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:n]

    @property
    def is_loaded(self):
        return self._loaded

    @property
    def icon_count(self):
        """Total fingerprints across all gods (base + overlay).

        Each god can hold multiple fingerprints — see ``__init__``.
        Use ``god_count`` if you want unique-god count instead.
        """
        return sum(len(v) for v in self._icon_hists.values())

    @property
    def god_count(self):
        """Number of distinct gods loaded.

        Independent of how many fingerprints each god has.  ``Ymir``
        with both an in-game and an overlay fingerprint counts as 1.
        """
        return len(self._icon_hists)

    # === INTERNAL ===

    # Cached circular masks keyed by (h, w).  Lazily built the first
    # time a new dimension is seen, then reused for every histogram
    # call — avoids rebuilding the same mask thousands of times across
    # a scan.
    _MASK_CACHE: dict[tuple[int, int], np.ndarray] = {}

    @classmethod
    def _get_circular_mask(cls, h: int, w: int) -> np.ndarray:
        """Return a (cached) ``h x w`` uint8 mask with a filled circle.

        The in-game HUD portrait is rendered inside a circle; the
        rectangular crop we compare wastes ~21% of its pixels on
        corners that contain wildly different content between the
        recording (bright game-world scene) and the CDN art (static
        backdrop / transparency).  Including those corners in the
        histogram drags correlation down for every god — most
        noticeably for borderline ones whose face palette is otherwise
        unique enough to win their margin.

        Mask is 255 inside a circle of radius ``min(w,h)/2`` centered
        on the image, 0 elsewhere.  The same mask gets applied to
        both reference icons and recording crops at hist-compute
        time, so paired comparisons stay symmetric.
        """
        key = (int(h), int(w))
        if key not in cls._MASK_CACHE:
            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.circle(
                mask,
                (w // 2, h // 2),
                min(w, h) // 2,
                255,
                thickness=-1,
            )
            cls._MASK_CACHE[key] = mask
        return cls._MASK_CACHE[key]

    @classmethod
    def _split_bgr_alpha(cls, img_raw):
        """Split a cv2.IMREAD_UNCHANGED result into (BGR resized to
        MATCH_SIZE, alpha_mask resized to MATCH_SIZE) — or (BGR resized,
        None) when no alpha channel is present.

        The alpha_mask is a 0/255 uint8 array marking pixels with
        ``alpha > 128`` as opaque (255).  Used by :meth:`_compute_hist`
        to skip transparent pixels.

        Centralized here so the multiple icon-loading paths (base /
        overlay / reference) all handle alpha identically.
        """
        if img_raw is None:
            return None, None
        if img_raw.ndim == 3 and img_raw.shape[-1] == 4:
            bgr = img_raw[:, :, :3]
            alpha = img_raw[:, :, 3]
            bgr = cv2.resize(bgr, MATCH_SIZE, interpolation=cv2.INTER_AREA)
            alpha = cv2.resize(
                alpha, MATCH_SIZE, interpolation=cv2.INTER_AREA,
            )
            alpha_mask = ((alpha > 128).astype(np.uint8) * 255)
            return bgr, alpha_mask
        # No alpha channel — straight BGR (or grayscale promoted to BGR
        # for safety).
        if img_raw.ndim == 2:
            bgr = cv2.cvtColor(img_raw, cv2.COLOR_GRAY2BGR)
        else:
            bgr = img_raw
        bgr = cv2.resize(bgr, MATCH_SIZE, interpolation=cv2.INTER_AREA)
        return bgr, None

    @classmethod
    def _compute_hist(cls, img_bgr, alpha_mask=None):
        """
        Compute a normalized 3D color histogram for a BGR image.
        Uses HSV color space (lighting-robust) and a circular mask so
        only the in-game-portrait-shaped region contributes to the
        histogram — see ``_get_circular_mask`` for why.

        ``alpha_mask`` (optional, same H×W as ``img_bgr``, values 0 or
        255): when supplied, only pixels where alpha_mask is non-zero
        AND inside the circular mask contribute to the histogram.
        Used for icons with transparency — without this, transparent
        pixels become pure black after a non-alpha cv2.imread and
        create a fake black-pixel spike in the fingerprint that
        doesn't exist in actual recordings (where those areas show
        whatever's behind the OBS overlay).
        """
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        h, w = hsv.shape[:2]
        mask = cls._get_circular_mask(h, w)
        if alpha_mask is not None:
            mask = cv2.bitwise_and(mask, alpha_mask)
        hist = cv2.calcHist(
            [hsv], [0, 1, 2], mask,
            [HIST_BINS, HIST_BINS, HIST_BINS],
            [0, 180, 0, 256, 0, 256]
        )
        cv2.normalize(hist, hist)
        return hist.flatten()

    @staticmethod
    def _slug_to_name(slug):
        """Convert CDN slug back to display name: 'hou-yi' → 'Hou Yi'."""
        # Special cases
        specials = {
            "the-morrigan": "The Morrigan",
            "morgan-le-fay": "Morgan Le Fay",
            "baron-samedi": "Baron Samedi",
            "ne-zha": "Ne Zha",
            "nu-wa": "Nu Wa",
            "hou-yi": "Hou Yi",
            "hun-batz": "Hun Batz",
            "sun-wukong": "Sun Wukong",
            "jing-wei": "Jing Wei",
            "da-ji": "Da Ji",
            "princess-bari": "Princess Bari",
            "guan-yu": "Guan Yu",
            "hua-mulan": "Hua Mulan",
            "ah-muzen-cab": "Ah Muzen Cab",
            "ah-puch": "Ah Puch",
            "ao-kuang": "Ao Kuang",
            "cu-chulainn": "Cu Chulainn",
            "erlang-shen": "Erlang Shen",
            "he-bo": "He Bo",
            "king-arthur": "King Arthur",
            "le-fay": "Le Fay",
            "xing-tian": "Xing Tian",
            "zhong-kui": "Zhong Kui",
        }
        if slug in specials:
            return specials[slug]
        # Default: capitalize each word
        return " ".join(word.capitalize() for word in slug.split("-"))
