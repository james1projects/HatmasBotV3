"""Regression tests for the 2026-08-05 god-detection hardening:

  * KDA-bar gate — the portrait matcher may only run on frames where
    the K/D/A bar actually parses.  Kills the god-select lobby false
    positive (busy bottom-center passed the variance gate, warm lobby
    art histogram-matched Hercules).
  * Re-verification — after identification the matcher keeps watching;
    a DIFFERENT god winning GOD_REVERIFY_CONFIRM_FRAMES consecutive
    frames corrects the identity (no stat reset) with a cooldown
    against ping-pong.  Overlay-source self-matches (the bot's own OBS
    art echoing back through the capture) are neutral evidence.
  * Structural veto — GodMatcher rejects histogram winners whose
    grayscale NCC against the winner's own art is below STRUCT_MIN_NCC
    (palette-alike junk crops).

The loop tests drive the REAL _detection_loop with scripted fakes, in
the style of test_killdetector_hardening.py.  Run:

    python tests/test_god_detection_hardening.py    (exit 0 = green)
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import plugins.killdetector as kd  # noqa: E402

kd.SCREENSHOT_INTERVAL = 0.004

FRAME = Image.new("RGB", (64, 64), (30, 30, 30))


class ScriptedReader:
    """Stands in for KdaReader: returns a scripted KDA per frame.
    The run ends when the script is exhausted."""

    is_ready = True

    def __init__(self, script):
        self.script = list(script)
        self.detector = None

    def read_kda(self, img, crop_origin=(0, 0)):
        if not self.script:
            self.detector._running = False
            return None
        return self.script.pop(0)

    def is_gameplay_screen(self, img_array, crop_origin=(0, 0)):
        return True

    def is_overlay_open(self, img_array, crop_origin=(0, 0)):
        return False

    def enroll_last_read(self):
        return 0

    def discard_last_read(self):
        pass


class ScriptedMatcher:
    """Stands in for GodMatcher.  Script entries are (name, conf) or
    (name, conf, source); None entries mean below-threshold.  The last
    entry repeats once the script is exhausted."""

    is_loaded = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.last_winner_source = None

    def identify(self, img, crop_origin=(0, 0)):
        self.calls += 1
        entry = self.script.pop(0) if self.script else self._last
        self._last = entry
        if entry is None:
            self.last_winner_source = None
            return None, 0.0
        name, conf = entry[0], entry[1]
        self.last_winner_source = entry[2] if len(entry) > 2 else "base"
        return name, conf

    _last = None

    def identify_top_n(self, img, n=3, crop_origin=(0, 0)):
        return []


def run_loop(kda_script, matcher_script, *, identified_as=None,
             is_dead=False, timeout=20.0):
    """Drive the real detection loop.  Returns (detector, matcher,
    identified_events, corrected_events)."""
    det = kd.KillDeathDetector(debug=False)
    reader = ScriptedReader(kda_script)
    reader.detector = det
    det._reader = reader
    det.bot = None
    det.obs_client = object()
    det._grab_screenshot = lambda: FRAME
    det._god_matcher = ScriptedMatcher(matcher_script)
    det._save_state = lambda: None
    det._load_state = lambda: False
    det._is_dead = is_dead
    if identified_as is not None:
        det._god_identified = True
        det._god_confirm_name = identified_as
        det._last_match_god = identified_as

    identified, corrected = [], []

    async def on_id(name):
        identified.append(name)

    async def on_corr(name):
        corrected.append(name)

    det.add_god_identified_listener(on_id)
    det.add_god_corrected_listener(on_corr)

    async def main():
        det._running = True
        try:
            await asyncio.wait_for(det._detection_loop(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError("detection loop never exhausted script")
        await asyncio.sleep(0.05)

    asyncio.run(main())
    return det, det._god_matcher, identified, corrected


# --- KDA-bar gate -------------------------------------------------------

def test_gate_blocks_god_id_in_lobby():
    # Reader never parses a bar (lobby / god select) — the matcher must
    # never be consulted, no matter how confident it would be.
    det, matcher, identified, _ = run_loop(
        kda_script=[None] * 8,
        matcher_script=[("Hercules", 0.95)],
    )
    assert matcher.calls == 0, matcher.calls
    assert identified == [] and not det._god_identified
    assert det._god_confirm_count == 0
    print("ok  lobby frames (no KDA parse) never reach the matcher")


def test_gate_open_allows_identification():
    # Bar parses -> matcher runs -> 3-frame confirm -> listener fires.
    det, matcher, identified, _ = run_loop(
        kda_script=[(0, 0, 0)] * 8,
        matcher_script=[("Ymir", 0.9)] * 3,
    )
    assert identified == ["Ymir"], identified
    assert det._god_identified and det._god_confirm_name == "Ymir"
    print("ok  parseable KDA bar lets identification proceed")


def test_gate_frames_do_not_touch_confirm_streak():
    # Two confirms, a gated (lobby) gap, then a third confirm: the gap
    # must not reset the streak (it is no evidence either way).
    det, matcher, identified, _ = run_loop(
        kda_script=[(0, 0, 0), (0, 0, 0), None, None, (0, 0, 0), (0, 0, 0)],
        matcher_script=[("Ymir", 0.9)] * 3,
    )
    assert identified == ["Ymir"], identified
    print("ok  gated frames leave the confirm streak untouched")


# --- Re-verification ----------------------------------------------------

def test_reverify_corrects_wrong_lockin():
    # Locked in as Hercules (the lobby-misfire scenario); in-game the
    # real portrait (Ymir) wins 6 consecutive frames -> correction, and
    # match stats survive (same match, corrected name).
    det, matcher, identified, corrected = run_loop(
        kda_script=[(0, 0, 0)] * 10,
        matcher_script=[("Ymir", 0.9)],
        identified_as="Hercules",
    )
    det.match_kills  # attribute exists
    assert corrected == ["Ymir"], corrected
    assert det._god_confirm_name == "Ymir"
    assert det._last_match_god == "Ymir"
    assert identified == []  # correction is not a fresh identification
    print("ok  re-verify corrects a wrong lock-in after 6 frames")


def test_reverify_streak_shorter_than_required_never_corrects():
    # 5 challenger frames then back to the current god: no correction.
    det, matcher, identified, corrected = run_loop(
        kda_script=[(0, 0, 0)] * 10,
        matcher_script=[("Ymir", 0.9)] * 5 + [("Hercules", 0.9)],
        identified_as="Hercules",
    )
    assert corrected == [], corrected
    assert det._god_confirm_name == "Hercules"
    print("ok  challenger streak below threshold never corrects")


def test_reverify_stats_not_reset_on_correction():
    det = kd.KillDeathDetector(debug=False)
    reader = ScriptedReader([(3, 1, 2)] * 10)
    reader.detector = det
    det._reader = reader
    det.bot = None
    det.obs_client = object()
    det._grab_screenshot = lambda: FRAME
    det._god_matcher = ScriptedMatcher([("Ymir", 0.9)])
    det._save_state = lambda: None
    det._load_state = lambda: False
    det._god_identified = True
    det._god_confirm_name = "Hercules"
    det._last_match_god = "Hercules"
    det.match_kills, det.match_deaths, det.match_assists = 3, 1, 2
    det._prev_kda = (3, 1, 2)
    det._startup_validated = True

    corrected = []

    async def on_corr(name):
        corrected.append(name)

    det.add_god_corrected_listener(on_corr)

    async def main():
        det._running = True
        await asyncio.wait_for(det._detection_loop(), 20.0)
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert corrected == ["Ymir"], corrected
    assert (det.match_kills, det.match_deaths, det.match_assists) == (3, 1, 2)
    assert det._prev_kda == (3, 1, 2)
    print("ok  correction preserves match stats and KDA baseline")


def test_reverify_cooldown_blocks_second_correction():
    # First correction fires (cooldown starts), then a new challenger
    # sustains a streak — cooldown must hold it back.
    det, matcher, identified, corrected = run_loop(
        kda_script=[(0, 0, 0)] * 20,
        matcher_script=[("Ymir", 0.9)] * 6 + [("Loki", 0.9)],
        identified_as="Hercules",
    )
    assert corrected == ["Ymir"], corrected
    assert det._god_confirm_name == "Ymir"
    # Loki's streak is alive and waiting for the cooldown, not applied.
    assert det._reverify_challenger == "Loki"
    print("ok  correction cooldown blocks a second flip")


def test_reverify_overlay_echo_is_neutral():
    # The bot's own custom art (source="overlay") matching the CURRENT
    # god must not reset a genuine challenger streak: alternate
    # challenger frames with overlay self-echo frames.
    script = []
    for _ in range(6):
        script.append(("Ymir", 0.9))            # challenger (base)
        script.append(("Hercules", 0.95, "overlay"))  # self-echo
    det, matcher, identified, corrected = run_loop(
        kda_script=[(0, 0, 0)] * 16,
        matcher_script=script,
        identified_as="Hercules",
    )
    assert corrected == ["Ymir"], corrected
    print("ok  overlay self-echo does not defend a wrong identity")


def test_reverify_base_confirmation_resets_challenger():
    # Same alternation but the current god wins via BASE fingerprints —
    # genuine confirmations must keep resetting the challenger streak.
    script = []
    for _ in range(8):
        script.append(("Ymir", 0.9))
        script.append(("Hercules", 0.95))  # source defaults to "base"
    det, matcher, identified, corrected = run_loop(
        kda_script=[(0, 0, 0)] * 20,
        matcher_script=script,
        identified_as="Hercules",
    )
    assert corrected == [], corrected
    assert det._god_confirm_name == "Hercules"
    print("ok  genuine confirmations defend the current identity")


def test_reverify_skipped_while_dead():
    # Three stable reads let startup validation complete but end the
    # run BEFORE the loop's respawn logic clears _is_dead (which it
    # rightly does after stable post-death reads) — so every re-verify
    # opportunity here happens while dead, and none may run.
    det, matcher, identified, corrected = run_loop(
        kda_script=[(0, 1, 0)] * 3,
        matcher_script=[("Ymir", 0.9)],
        identified_as="Hercules",
        is_dead=True,
    )
    assert matcher.calls == 0, matcher.calls
    assert corrected == []
    print("ok  re-verify pauses while dead (desaturated portrait)")


# --- Structural veto (real GodMatcher, synthetic icons) -----------------

def test_struct_veto_rejects_palette_alike_junk():
    from core import god_matcher as gm

    with tempfile.TemporaryDirectory() as td:
        icons = Path(td)
        # God A: strong structure — left half black, right half white.
        art = np.zeros((128, 128, 3), dtype=np.uint8)
        art[:, 64:] = 255
        Image.fromarray(art).save(icons / "testgod.png")

        matcher = gm.GodMatcher(icons_dir=icons)
        assert matcher.load_icons()

        x1, y1, x2, y2 = gm.PORTRAIT_REGION

        # Genuine crop: the same art in the portrait region.
        frame = Image.new("RGB", (1920, 1080), (10, 10, 10))
        frame.paste(Image.fromarray(art).resize((x2 - x1, y2 - y1)),
                    (x1, y1))
        name, conf = matcher.identify(frame)
        assert name == "Testgod", (name, conf)
        assert matcher.last_struct_check is not None
        assert not matcher.last_struct_check[3]

        # Junk crop: identical 50/50 black-white PALETTE, scrambled
        # structure (checkerboard) — histogram says Testgod, NCC ~0.
        yy, xx = np.mgrid[0:128, 0:128]
        checker = ((xx // 8 + yy // 8) % 2 * 255).astype(np.uint8)
        junk = np.stack([checker] * 3, axis=-1)
        frame2 = Image.new("RGB", (1920, 1080), (10, 10, 10))
        frame2.paste(Image.fromarray(junk).resize((x2 - x1, y2 - y1)),
                     (x1, y1))
        name2, conf2 = matcher.identify(frame2)
        assert name2 is None, (name2, conf2)
        assert matcher.last_struct_check is not None
        assert matcher.last_struct_check[3] is True  # vetoed
        # The histogram DID accept it — proving the veto is what saved us.
        assert conf2 >= gm.MIN_CONFIDENCE_MARGIN, conf2

    print("ok  structural veto rejects palette-alike junk crops")


def test_struct_veto_flat_crop_rejected():
    from core import god_matcher as gm

    with tempfile.TemporaryDirectory() as td:
        icons = Path(td)
        # A dark icon so a black crop histogram-matches it (the
        # real-world Hachiman-on-black-frame false accept).
        art = np.full((128, 128, 3), 12, dtype=np.uint8)
        Image.fromarray(art).save(icons / "darkgod.png")
        matcher = gm.GodMatcher(icons_dir=icons)
        assert matcher.load_icons()

        frame = Image.new("RGB", (1920, 1080), (0, 0, 0))
        name, conf = matcher.identify(frame)
        assert name is None, (name, conf)
    print("ok  flat/black crops can no longer histogram-match a dark god")


if __name__ == "__main__":
    test_gate_blocks_god_id_in_lobby()
    test_gate_open_allows_identification()
    test_gate_frames_do_not_touch_confirm_streak()
    test_reverify_corrects_wrong_lockin()
    test_reverify_streak_shorter_than_required_never_corrects()
    test_reverify_stats_not_reset_on_correction()
    test_reverify_cooldown_blocks_second_correction()
    test_reverify_overlay_echo_is_neutral()
    test_reverify_base_confirmation_resets_challenger()
    test_reverify_skipped_while_dead()
    test_struct_veto_rejects_palette_alike_junk()
    test_struct_veto_flat_crop_rejected()
    print("\nALL GREEN")
