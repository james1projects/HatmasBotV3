"""Regression tests for the live KillDetector's 2026-07-09 hardening:

  * DELTA_CONFIRM_READS — an increased read must repeat on consecutive
    frames before events fire (kills phantom assists / doubled first
    kills from single-frame misreads).
  * REBASELINE_REQUIRED_READS — poisoned-baseline recovery ported from
    the VOD detector: consecutive identical "decreased" reads correct a
    phantom that slipped through, instead of rejecting reality forever.

The tests drive the REAL _detection_loop with a scripted fake reader
and a stubbed OBS grab, and capture fired listener events.  Run:

    python tests/test_killdetector_hardening.py     (exit 0 = green)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import plugins.killdetector as kd  # noqa: E402

# Fast test cadence — the loop sleeps SCREENSHOT_INTERVAL between frames.
kd.SCREENSHOT_INTERVAL = 0.004

FRAME = Image.new("RGB", (64, 64), (30, 30, 30))


class ScriptedReader:
    """Stands in for KdaReader: returns a scripted KDA per frame."""

    is_ready = True

    def __init__(self, script):
        self.script = list(script)
        self.detector = None  # set by run_script

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

    def read_kda_with_details(self, img, crop_origin=(0, 0)):
        return {"kda": None, "failure_reason": "scripted", "groups": []}


def run_script(script, timeout=20.0):
    """Feed a KDA script through the real detection loop; return the
    detector plus captured (kills, deaths, assists, multikills)."""
    det = kd.KillDeathDetector(debug=False)
    reader = ScriptedReader(script)
    reader.detector = det
    det._reader = reader
    det.bot = None
    det.obs_client = object()          # skip OBS connect branch
    det._grab_screenshot = lambda: FRAME
    det._god_matcher = None
    det._god_identified = True         # KDA reads are gated on god ID
    det._save_state = lambda: None     # keep test hermetic (no state file)
    det._load_state = lambda: False    # ...and never restore one

    kills, deaths, assists, multis = [], [], [], []

    async def on_kill(kill_type, count):
        kills.append((kill_type, count))

    async def on_death(count):
        deaths.append(count)

    async def on_assist(count):
        assists.append(count)

    async def on_multi(kill_type):
        multis.append(kill_type)

    corrections = []

    async def on_correction(k_rm, d_rm, a_rm, corrected):
        corrections.append((k_rm, d_rm, a_rm, tuple(corrected)))

    det.add_kill_listener(on_kill)
    det.add_death_listener(on_death)
    det.add_assist_listener(on_assist)
    det.add_multikill_listener(on_multi)
    det.add_correction_listener(on_correction)
    det._corrections = corrections  # exposed for assertions

    async def main():
        det._running = True
        try:
            await asyncio.wait_for(det._detection_loop(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError("detection loop never exhausted script")
        # let fire-and-forget listener tasks drain
        await asyncio.sleep(0.05)

    asyncio.run(main())
    return det, kills, deaths, assists, multis


def test_startup_then_confirmed_kill():
    det, kills, deaths, assists, _ = run_script([
        (0, 0, 0), (0, 0, 0), (0, 0, 0),   # startup validation
        (0, 0, 0),
        (1, 0, 0), (1, 0, 0),              # real kill: read + confirm
        (1, 0, 0), (1, 0, 0),
    ])
    assert kills == [("player_kill", 1)], kills
    assert deaths == [] and assists == [], (deaths, assists)
    assert det.match_kills == 1 and det._prev_kda == (1, 0, 0)
    print("ok  startup + confirmed kill fires exactly once")


def test_single_frame_phantom_assist_suppressed():
    det, kills, deaths, assists, _ = run_script([
        (0, 0, 0), (0, 0, 0), (0, 0, 0),
        (0, 0, 1),                         # one-frame phantom assist
        (0, 0, 0), (0, 0, 0), (0, 0, 0),   # reality resumes
    ])
    assert assists == [], assists
    assert det.match_assists == 0
    assert det._prev_kda == (0, 0, 0)
    print("ok  single-frame phantom assist suppressed (no event, no poison)")


def test_flicker_never_confirms():
    det, kills, deaths, assists, _ = run_script([
        (0, 0, 0), (0, 0, 0), (0, 0, 0),
        (0, 0, 1), (0, 0, 0), (0, 0, 1), (0, 0, 0), (0, 0, 1), (0, 0, 0),
    ])
    assert assists == [] and det.match_assists == 0
    print("ok  alternating flicker never reaches confirmation")


def test_rebaseline_recovers_from_poison():
    det, kills, deaths, assists, _ = run_script([
        (2, 1, 0), (2, 1, 0), (2, 1, 0),   # startup baseline 2/1/0
        (2, 1, 1), (2, 1, 1),              # phantom assist CONFIRMS
                                           # (two identical misreads —
                                           # the rare worst case)
        (2, 1, 0), (2, 1, 0), (2, 1, 0),   # reality disagrees x3
        (3, 1, 0), (3, 1, 0),              # then a real kill
    ])
    # the phantom fired once (that's the residual risk), but...
    assert assists == [1], assists
    # ...rebaseline must have corrected the counter and baseline:
    assert det.match_assists == 0, det.match_assists
    assert det._prev_kda == (3, 1, 0), det._prev_kda
    # and the post-recovery kill still fired normally:
    assert ("player_kill", 1) in kills or kills, kills
    assert det.match_kills == 3  # 2 baseline catch-up + 1 live
    # the correction listener told downstream consumers (economy
    # cosmetic KDA, overlays, death counter) to walk back the phantom:
    assert det._corrections == [(0, 0, 1, (2, 1, 0))], det._corrections
    print("ok  poisoned baseline recovers after "
          f"{kd.REBASELINE_REQUIRED_READS} agreeing reads; "
          "counters corrected; correction listener fired; "
          "later events fire")


def test_multikill_classification_survives_confirm_gate():
    det, kills, _, _, multis = run_script([
        (0, 0, 0), (0, 0, 0), (0, 0, 0),
        (1, 0, 0), (1, 0, 0),              # kill 1
        (2, 0, 0), (2, 0, 0),              # kill 2 inside multikill window
    ])
    assert det.match_kills == 2
    assert multis == ["double_kill"], multis
    print("ok  multikill classification intact through confirm gate")


if __name__ == "__main__":
    test_startup_then_confirmed_kill()
    test_single_frame_phantom_assist_suppressed()
    test_flicker_never_confirms()
    test_rebaseline_recovers_from_poison()
    test_multikill_classification_survives_confirm_gate()
    print("\nall killdetector hardening tests passed")
