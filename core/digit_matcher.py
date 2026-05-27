"""
Digit Template Matcher
=======================
Recognizes digits 0-9 from binarized game HUD screenshots using template
matching.  Designed as a replacement for Tesseract OCR in the KillDeathDetector,
where the semi-transparent KDA bar background varies with camera movement and
causes frequent OCR failures.

How it works:
  1. Templates are stored as binary PNG files in data/digit_templates/
     Named: {digit}_{index}.png (e.g. 0_0.png, 0_1.png, 4_0.png)
  2. Each candidate digit is resized to a standard 60x80 size.
  3. Structural pre-filter: count enclosed holes in the candidate.
     - 2 holes → must be 8
     - 1 hole → can only match 0, 4, 6, 9
     - 0 holes → can only match 1, 2, 3, 5, 7
  4. XOR pixel distance against all valid templates — lowest distance wins.
  5. Returns (digit, confidence) or None if no match above threshold.

Templates are auto-collected from confirmed OCR reads: when Tesseract and
the sanity checks agree on a value, the digit crop is saved as a new template.
This lets the library grow over multiple sessions.
"""

import cv2
import numpy as np
from pathlib import Path

# Standard size all digits are resized to before matching.
# Larger = more discriminative but slower.  60x80 gives good results
# at the 8x-scaled KDA bar resolution (~60x77 per digit at 8x).
TEMPLATE_W = 60
TEMPLATE_H = 80

# Maximum XOR distance to accept a match (fraction of total pixels).
# Above this threshold, the match is rejected and Tesseract is used.
MAX_MATCH_DISTANCE = 0.30

# Minimum margin between best and second-best match.  If the margin
# is below this, the match is ambiguous and we fall back to Tesseract.
MIN_MATCH_MARGIN = 0.02

# Maximum number of templates to store per digit.  Oldest are removed
# when this limit is reached.  More templates = better coverage of
# different game backgrounds, but diminishing returns past ~20.
MAX_TEMPLATES_PER_DIGIT = 20

# Which digits can have which hole counts.
# This is a hard structural filter — eliminates impossible matches.
HOLES_TO_DIGITS = {
    0: {"1", "2", "3", "5", "7"},
    1: {"0", "4", "6", "9"},
    2: {"8"},
}


class DigitMatcher:
    """Template-based digit recognizer for game HUD numbers."""

    def __init__(self, template_dir: Path | str):
        self.template_dir = Path(template_dir)
        self.template_dir.mkdir(parents=True, exist_ok=True)

        # templates[digit] = list of (binary_image, n_holes)
        self.templates: dict[str, list[tuple[np.ndarray, int]]] = {}
        self._load_templates()

    def _load_templates(self):
        """Load all template PNGs from the template directory."""
        self.templates.clear()
        count = 0
        for f in sorted(self.template_dir.glob("*.png")):
            # Filename format: {digit}_{index}.png
            parts = f.stem.split("_")
            if len(parts) < 2 or len(parts[0]) != 1 or not parts[0].isdigit():
                continue
            digit = parts[0]
            img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            # Ensure correct size
            if img.shape != (TEMPLATE_H, TEMPLATE_W):
                img = cv2.resize(img, (TEMPLATE_W, TEMPLATE_H),
                                 interpolation=cv2.INTER_CUBIC)
                _, img = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)

            n_holes = self._count_holes(img)
            self.templates.setdefault(digit, []).append((img, n_holes))
            count += 1

        self._digit_count = count

    @property
    def is_loaded(self) -> bool:
        return self._digit_count > 0

    @property
    def template_count(self) -> int:
        return self._digit_count

    @property
    def digit_coverage(self) -> set[str]:
        """Which digits have at least one template."""
        return set(self.templates.keys())

    @staticmethod
    def _count_holes(binary_img: np.ndarray) -> int:
        """Count enclosed regions (holes) in a binary digit image."""
        contours, hierarchy = cv2.findContours(
            binary_img, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
        )
        if hierarchy is None:
            return 0
        return sum(1 for i in range(len(hierarchy[0]))
                   if hierarchy[0][i][3] >= 0)

    @staticmethod
    def prepare_candidate(binary_crop: np.ndarray) -> np.ndarray:
        """Resize a binary digit crop to the standard template size."""
        resized = cv2.resize(binary_crop, (TEMPLATE_W, TEMPLATE_H),
                             interpolation=cv2.INTER_CUBIC)
        _, clean = cv2.threshold(resized, 127, 255, cv2.THRESH_BINARY)
        return clean

    def match(self, candidate: np.ndarray) -> tuple[str, float] | None:
        """Match a standardized binary digit against templates.

        Args:
            candidate: Binary image of shape (TEMPLATE_H, TEMPLATE_W).

        Returns:
            (digit_str, distance) if a confident match is found, else None.
            distance is 0.0 (perfect) to 1.0 (no overlap).
        """
        if not self.templates:
            return None

        candidate_holes = self._count_holes(candidate)

        # Determine which digits are structurally possible
        possible_digits = HOLES_TO_DIGITS.get(candidate_holes)
        if possible_digits is None:
            # Unexpected hole count — allow all digits
            possible_digits = set(self.templates.keys())

        # Find best match across all valid templates
        scores = []  # (digit, distance)
        for digit, samples in self.templates.items():
            if digit not in possible_digits:
                continue
            best_dist = float("inf")
            for tmpl, tmpl_holes in samples:
                xor = cv2.bitwise_xor(candidate, tmpl)
                dist = np.sum(xor > 0) / (TEMPLATE_H * TEMPLATE_W)
                best_dist = min(best_dist, dist)
            scores.append((digit, best_dist))

        if not scores:
            return None

        scores.sort(key=lambda x: x[1])
        best_digit, best_dist = scores[0]

        # Reject if distance too high
        if best_dist > MAX_MATCH_DISTANCE:
            return None

        # Reject if margin too thin (ambiguous)
        if len(scores) > 1:
            margin = scores[1][1] - best_dist
            if margin < MIN_MATCH_MARGIN:
                return None

        return best_digit, best_dist

    def match_with_details(self, candidate: np.ndarray) -> dict:
        """Run the same matching logic as ``match()`` but return the full
        decision payload — every candidate's distance, the runner-up
        margin, and an explicit verdict string.

        Used by the detector debug page so the operator can see *why* a
        digit was accepted or rejected (and what the second-best option
        was, when it was close).

        The original ``match()`` is intentionally left untouched so the
        live KDA reading path keeps its exact existing behaviour; this
        method walks the same algorithm in parallel.

        Returns a dict with these keys:
            scores:          list[(digit, distance)], sorted ascending by
                             distance. Empty list if no templates / no
                             candidates passed the hole filter.
            best:            (digit, distance) | None. The winner.
            runner_up:       (digit, distance) | None. The second-best,
                             or None if there's only one candidate.
            margin:          float. Gap between best and runner-up
                             distances. 0.0 if no runner-up.
            candidate_holes: int. Hole count from the structural filter.
            verdict:         "accepted"
                             | "rejected_distance"  (best_dist > MAX_MATCH_DISTANCE)
                             | "rejected_margin"    (margin < MIN_MATCH_MARGIN)
                             | "no_templates"
                             | "no_candidates"      (hole filter cleared
                                                     the field, e.g. no
                                                     templates for that
                                                     hole count)
        """
        if not self.templates:
            return {
                "scores": [],
                "best": None,
                "runner_up": None,
                "margin": 0.0,
                "candidate_holes": 0,
                "verdict": "no_templates",
            }

        candidate_holes = self._count_holes(candidate)
        possible_digits = HOLES_TO_DIGITS.get(candidate_holes)
        if possible_digits is None:
            possible_digits = set(self.templates.keys())

        scores: list[tuple[str, float]] = []
        for digit, samples in self.templates.items():
            if digit not in possible_digits:
                continue
            best_dist = float("inf")
            for tmpl, _tmpl_holes in samples:
                xor = cv2.bitwise_xor(candidate, tmpl)
                dist = float(np.sum(xor > 0) / (TEMPLATE_H * TEMPLATE_W))
                if dist < best_dist:
                    best_dist = dist
            scores.append((digit, best_dist))

        if not scores:
            return {
                "scores": [],
                "best": None,
                "runner_up": None,
                "margin": 0.0,
                "candidate_holes": candidate_holes,
                "verdict": "no_candidates",
            }

        scores.sort(key=lambda x: x[1])
        best = scores[0]
        runner_up = scores[1] if len(scores) > 1 else None
        margin = (runner_up[1] - best[1]) if runner_up else 0.0

        if best[1] > MAX_MATCH_DISTANCE:
            verdict = "rejected_distance"
        elif runner_up is not None and margin < MIN_MATCH_MARGIN:
            verdict = "rejected_margin"
        else:
            verdict = "accepted"

        return {
            "scores": scores,
            "best": best,
            "runner_up": runner_up,
            "margin": margin,
            "candidate_holes": candidate_holes,
            "verdict": verdict,
        }

    def add_template(self, digit: str, binary_crop: np.ndarray) -> bool:
        """Add a confirmed digit crop as a new template.

        Call this after a read has been validated by the sanity checks
        in the detection loop.  The crop is resized and saved to disk.

        Returns True if the template was added, False if skipped
        (e.g. already at max templates for this digit).
        """
        if len(digit) != 1 or not digit.isdigit():
            return False

        samples = self.templates.get(digit, [])
        if len(samples) >= MAX_TEMPLATES_PER_DIGIT:
            return False

        # Prepare and save
        std = self.prepare_candidate(binary_crop)
        n_holes = self._count_holes(std)

        # Check structural consistency
        expected = HOLES_TO_DIGITS.get(n_holes, set())
        if digit not in expected:
            # Structural mismatch — don't save a bad template
            return False

        # Find next available index
        existing = list(self.template_dir.glob(f"{digit}_*.png"))
        idx = len(existing)
        path = self.template_dir / f"{digit}_{idx}.png"
        cv2.imwrite(str(path), std)

        self.templates.setdefault(digit, []).append((std, n_holes))
        self._digit_count += 1
        return True
