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
PORTRAIT_REGION = (635, 942, 715, 1022)  # (x1, y1, x2, y2)

# Icon comparison size — all icons and portrait crops are resized to this.
MATCH_SIZE = (64, 64)

# Minimum correlation to accept a match (absolute threshold).
# 0.90+ = very high confidence (same god for sure)
# 0.80-0.90 = likely correct but verify
# Below 0.80 = probably wrong (different lighting, skin, overlay, etc.)
MIN_CONFIDENCE = 0.80

# Margin-based acceptance: if absolute score is below MIN_CONFIDENCE but
# above MIN_CONFIDENCE_MARGIN and the gap to the second-best match exceeds
# MARGIN_GAP, accept anyway.  This handles gods whose color histograms
# don't correlate as strongly (e.g. Ymir's ice-blue palette scores ~0.73
# but with a 0.47 gap to the runner-up — clearly correct).
MIN_CONFIDENCE_MARGIN = 0.60   # Floor for margin-based acceptance
MARGIN_GAP = 0.20              # Required gap between #1 and #2

# Number of histogram bins per channel for comparison.
HIST_BINS = 16


class GodMatcher:
    """
    Matches in-game god portraits against reference icon images.

    Preloads and caches histogram fingerprints for all reference icons
    so each frame comparison is fast (~1ms for 64 icons).
    """

    def __init__(self, icons_dir=None):
        if icons_dir is None:
            icons_dir = Path(__file__).parent.parent / "data" / "god_icons"
        self._icons_dir = Path(icons_dir)
        self._icon_hists = {}  # {god_name: histogram}
        self._loaded = False

    def load_icons(self):
        """Load all reference icons and precompute their histograms."""
        self._icon_hists.clear()

        if not self._icons_dir.exists():
            print(f"[GodMatcher] Icons directory not found: {self._icons_dir}")
            print("[GodMatcher] Run: python download_god_icons.py")
            return False

        count = 0
        # Load both .png (from wiki) and .jpg (from CDN fallback)
        icon_files = (
            list(self._icons_dir.glob("*.png")) + list(self._icons_dir.glob("*.jpg"))
        )
        # Deduplicate: if both .png and .jpg exist for same slug,
        # prefer the higher-resolution file (256x256 S2 art > 128x128 S1 art).
        by_stem = {}
        for p in icon_files:
            if p.stem not in by_stem:
                by_stem[p.stem] = p
            else:
                # Compare file sizes as a proxy for quality/resolution
                existing = by_stem[p.stem]
                if p.stat().st_size > existing.stat().st_size:
                    by_stem[p.stem] = p
        for path in sorted(by_stem.values(), key=lambda p: p.stem):
            try:
                img = cv2.imread(str(path))
                if img is None:
                    continue

                # Resize to standard comparison size
                img = cv2.resize(img, MATCH_SIZE, interpolation=cv2.INTER_AREA)

                # Compute color histogram fingerprint
                hist = self._compute_hist(img)

                # God name from filename: "hou-yi.jpg" → "Hou Yi"
                god_name = self._slug_to_name(path.stem)
                self._icon_hists[god_name] = hist
                count += 1

            except Exception as e:
                print(f"[GodMatcher] Error loading {path.name}: {e}")

        self._loaded = count > 0
        print(f"[GodMatcher] Loaded {count} god icon fingerprints")
        return self._loaded

    def identify(self, screenshot: Image.Image) -> tuple[Optional[str], float]:
        """
        Identify the god from a full 1920x1080 game screenshot.

        Args:
            screenshot: PIL Image of the full game screen (1920x1080).

        Returns:
            (god_name, confidence) — the best match and its correlation score.
            Returns (None, 0.0) if no match found above threshold.
        """
        if not self._loaded or not self._icon_hists:
            return None, 0.0

        # Crop the portrait region
        portrait = screenshot.crop(PORTRAIT_REGION)

        # Convert to OpenCV format and resize
        portrait_cv = cv2.cvtColor(np.array(portrait), cv2.COLOR_RGB2BGR)
        portrait_cv = cv2.resize(portrait_cv, MATCH_SIZE, interpolation=cv2.INTER_AREA)

        # Compute histogram for the portrait
        portrait_hist = self._compute_hist(portrait_cv)

        # Compare against all reference icons — track top 2 for margin check
        best_name = None
        best_score = -1.0
        second_score = -1.0

        for god_name, icon_hist in self._icon_hists.items():
            score = cv2.compareHist(portrait_hist, icon_hist, cv2.HISTCMP_CORREL)
            if score > best_score:
                second_score = best_score
                best_score = score
                best_name = god_name
            elif score > second_score:
                second_score = score

        # Accept if above absolute threshold
        if best_score >= MIN_CONFIDENCE:
            return best_name, best_score

        # Accept if above margin floor AND gap to runner-up is large enough
        # (handles gods with lower histogram correlation like Ymir's ice palette)
        if (best_score >= MIN_CONFIDENCE_MARGIN
                and (best_score - second_score) >= MARGIN_GAP):
            return best_name, best_score

        return None, best_score

    def identify_top_n(self, screenshot: Image.Image, n=3) -> list[tuple[str, float]]:
        """
        Return top N matches for debugging/verification.

        Returns:
            List of (god_name, confidence) tuples, sorted by confidence descending.
        """
        if not self._loaded or not self._icon_hists:
            return []

        portrait = screenshot.crop(PORTRAIT_REGION)
        portrait_cv = cv2.cvtColor(np.array(portrait), cv2.COLOR_RGB2BGR)
        portrait_cv = cv2.resize(portrait_cv, MATCH_SIZE, interpolation=cv2.INTER_AREA)
        portrait_hist = self._compute_hist(portrait_cv)

        scores = []
        for god_name, icon_hist in self._icon_hists.items():
            score = cv2.compareHist(portrait_hist, icon_hist, cv2.HISTCMP_CORREL)
            scores.append((god_name, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:n]

    @property
    def is_loaded(self):
        return self._loaded

    @property
    def icon_count(self):
        return len(self._icon_hists)

    # === INTERNAL ===

    @staticmethod
    def _compute_hist(img_bgr):
        """
        Compute a normalized 3D color histogram for an BGR image.
        Uses HSV color space for better lighting invariance.
        """
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist(
            [hsv], [0, 1, 2], None,
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


# === STANDALONE TEST ===
if __name__ == "__main__":
    import sys
    import glob

    matcher = GodMatcher()
    if not matcher.load_icons():
        print("No icons loaded. Run download_god_icons.py first.")
        sys.exit(1)

    # Test against captured frames if available
    frames_dir = Path(__file__).parent.parent / "data" / "captured_frames"
    if frames_dir.exists():
        frames = sorted(frames_dir.glob("frame_*.png"))
        if frames:
            print(f"\nTesting against {len(frames)} captured frames:")
            for frame_path in frames[:5]:
                img = Image.open(frame_path)
                top3 = matcher.identify_top_n(img, n=3)
                god, conf = matcher.identify(img)
                result = f"{god} ({conf:.3f})" if god else f"No match (best: {top3[0][1]:.3f})"
                print(f"  {frame_path.name}: {result}")
                if top3:
                    for name, score in top3:
                        print(f"    {name}: {score:.4f}")
        else:
            print("No captured frames found for testing.")
    else:
        print("No captured_frames directory. Use capture_frames.py to grab test data.")
