"""
Tests for core/detector_profile.py and the "regions are instance state"
refactor that lets the offline detector read another streamer's HUD:
KdaReader / GodMatcher accept regions, VodDetector derives its crop box
and frame commands from a DetectorProfile, and the sorter can keep a
downloaded VOD's own filename.

Self-running script per house convention: exit 0 only on full pass.
Hermetic: no ffmpeg (argv is asserted, never run), no network, no GPU.
Uses the shipped digit templates and one committed KDA fixture frame.
"""

import dataclasses
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.detector_profile import (CANVAS, DetectorProfile, ProfileError,  # noqa: E402
                                   union_box)
from core.detector_regions import DEFAULTS  # noqa: E402
from core.god_matcher import GodMatcher, PORTRAIT_REGION  # noqa: E402
from core.kda_reader import KDA_FIELD_WINDOWS, KdaReader  # noqa: E402
from tools.process_recordings import move_and_emit  # noqa: E402
from tools.vod_detector import (VOD_CROP_H, VOD_CROP_W, VOD_CROP_X, VOD_CROP_Y,  # noqa: E402
                                VodDetector, VodDetectorOptions)

FIXTURE = REPO_ROOT / "data" / "test_fixtures" / "kda" / "atlas_4_0_0_live_1080p.png"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="detector_profile_test_"))


# ── profile ───────────────────────────────────────────────────────────

def test_default_profile_matches_globals():
    p = DetectorProfile.default()
    assert dict(p.regions) == dict(DEFAULTS)
    assert p.crop_box() == (VOD_CROP_X, VOD_CROP_Y, VOD_CROP_W, VOD_CROP_H) == (0, 100, 1152, 980)
    assert p.crop_origin() == (0, 100)
    assert p.group_mode == "fields" and p.overlay_icons and p.reference_icons and p.portrait_enabled


def test_union_box_rounding_and_clamp():
    assert union_box([(3, 7, 10, 21)]) == (2, 6, 8, 16)
    assert union_box([]) == (0, 0, CANVAS[0], CANVAS[1])
    x, y, w, h = union_box([(1900, 1070, 1920, 1080), (0, 0, 10, 10)])
    assert (x, y) == (0, 0) and x + w <= 1920 and y + h <= 1080


def test_load_partial_validate_roundtrip():
    d = _tmp() / "foo"
    d.mkdir()
    prof = d / "profile.json"
    prof.write_text(json.dumps({"schema": 1, "channel": "foo",
                                "regions": {"kda": [1500, 40, 1600, 60]},
                                "notes": "default HUD"}), encoding="utf-8")
    q = DetectorProfile.load(prof)
    assert q.name == "foo" and q.regions["kda"] == (1500, 40, 1600, 60)
    assert q.regions["portrait"] == DEFAULTS["portrait"], "missing keys fall back per key"
    assert q.group_mode == "gaps", "loaded profiles default to the layout-agnostic grouping"
    assert not q.overlay_icons and not q.reference_icons, "James's libraries never leak"
    assert q.crop_box()[1] == 40 and q.crop_box()[0] == 0
    q2 = dataclasses.replace(q, portrait_enabled=False)
    assert q2.crop_box() != q.crop_box() or q.regions["portrait"][2] <= q2.crop_box()[0] + q2.crop_box()[2]
    q.save(d / "out.json")
    r = DetectorProfile.load(d / "out.json")
    assert r.regions == q.regions and r.group_mode == "gaps" and r.name == "foo"
    assert not list(d.glob("*.tmp"))
    for bad in [{"regions": {"kda": [1, 2, 3]}}, {"regions": {"kda": [10, 10, 5, 20]}},
                {"regions": {"kda": [0, 0, 2000, 10]}}, {"regions": {"nope": [0, 0, 1, 1]}},
                {"group_mode": "nope"}, {"kda_field_windows": {"K": [1, 2]}},
                {"canvas": [1280, 720]}, {"portrait_enabled": "yes"}]:
        prof.write_text(json.dumps(bad), encoding="utf-8")
        try:
            DetectorProfile.load(prof)
        except ProfileError:
            continue
        raise AssertionError(f"expected ProfileError for {bad}")
    try:
        DetectorProfile.load(d / "missing.json")
        raise AssertionError("missing file must raise")
    except ProfileError:
        pass


def test_relative_template_dir_resolves_against_profile():
    d = _tmp() / "bar"
    d.mkdir()
    (d / "profile.json").write_text(json.dumps({"digit_templates_dir": "templates"}), encoding="utf-8")
    q = DetectorProfile.load(d / "profile.json")
    assert q.digit_templates_dir == d / "templates"


# ── KdaReader with translated regions ─────────────────────────────────

def _fixture():
    assert FIXTURE.exists(), FIXTURE
    frame = Image.open(FIXTURE).convert("RGB")
    assert frame.size == (1920, 1080)
    expected = tuple(json.loads(FIXTURE.with_suffix(".json").read_text(encoding="utf-8"))["expected"]["kda"])
    return frame, expected


def test_reader_reads_translated_kda_region():
    frame, expected = _fixture()
    x1, y1, x2, y2 = DEFAULTS["kda"]
    bar = frame.crop((x1, y1, x2, y2))
    canvas = Image.new("RGB", (1920, 1080), (0, 0, 0))
    canvas.paste(bar, (900, 500))
    moved = (900, 500, 900 + (x2 - x1), 500 + (y2 - y1))
    default_reader = KdaReader()
    assert default_reader.is_ready and default_reader.kda_region == DEFAULTS["kda"]
    assert default_reader.read_kda(frame) == expected, "sanity: fixture reads with the defaults"
    assert default_reader.read_kda(canvas) is None, "default region sees only black"
    moved_reader = KdaReader(regions={"kda": moved})
    assert moved_reader.kda_region == moved
    assert moved_reader.read_kda(canvas) == expected
    assert moved_reader.regions["hud_check"] == DEFAULTS["hud_check"], "unspecified keys keep the defaults"
    # the same read through a profile crop box + crop_origin
    prof = DetectorProfile.from_dict({"regions": {"kda": list(moved)}}, name="t")
    cx, cy, cw, ch = prof.crop_box()
    strip = canvas.crop((cx, cy, cx + cw, cy + ch))
    assert moved_reader.read_kda(strip, crop_origin=prof.crop_origin()) == expected


def test_reader_gameplay_check_uses_instance_region():
    rng = np.random.default_rng(1)
    arr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    arr[200:300, 1300:1500] = rng.integers(0, 255, size=(100, 200, 3), dtype=np.uint8)
    custom = KdaReader(regions={"hud_check": (1300, 200, 1500, 300),
                                "gameplay_check": (1300, 200, 1500, 300)})
    assert custom.is_gameplay_screen(arr) is True
    assert KdaReader().is_gameplay_screen(arr) is False
    assert custom.scene_classification_with_details(arr)["is_gameplay"] is True


def test_reader_field_windows_override():
    r = KdaReader(kda_field_windows={"K": (0, 10), "D": (20, 30), "A": (40, 50)})
    assert r._field_windows == {"K": (0, 10), "D": (20, 30), "A": (40, 50)}
    assert KdaReader()._field_windows == KDA_FIELD_WINDOWS


def test_reader_template_dir_override():
    empty = _tmp() / "templates"
    r = KdaReader(template_dir=empty)
    assert not r.has_digit_templates, "an empty per-profile library loads no templates"
    assert KdaReader().has_digit_templates


# ── GodMatcher with a custom portrait region ──────────────────────────

def _synthetic_icon_lib():
    d = _tmp() / "icons"
    d.mkdir()
    icon = np.zeros((80, 80, 3), dtype=np.uint8)
    icon[:, :40] = (255, 30, 30)
    icon[:, 40:] = (30, 30, 255)
    Image.fromarray(icon).save(d / "testgod.png")
    other = np.zeros((80, 80, 3), dtype=np.uint8)
    other[:, :] = (30, 255, 30)
    Image.fromarray(other).save(d / "greengod.png")
    return d, icon


def test_god_matcher_custom_portrait_region():
    d, icon = _synthetic_icon_lib()
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame[100:180, 100:180] = icon
    frame_img = Image.fromarray(frame)
    gm = GodMatcher(icons_dir=d, portrait_region=(100, 100, 180, 180))
    assert gm.load_icons() and gm.portrait_region == (100, 100, 180, 180)
    name, score = gm.identify(frame_img)
    assert name == "Testgod", (name, score)
    default = GodMatcher(icons_dir=d)
    assert default.load_icons() and default.portrait_region == tuple(PORTRAIT_REGION)
    assert default.identify(frame_img)[0] is None
    # translated by a crop origin, same answer
    strip = frame_img.crop((50, 50, 400, 400))
    assert gm.identify(strip, crop_origin=(50, 50))[0] == "Testgod"


# ── VodDetector + profile ─────────────────────────────────────────────

class _NoReader:
    is_ready = True
    kda_region = DEFAULTS["kda"]


def _detector(profile=None, **opt):
    opts = VodDetectorOptions(**opt)
    return VodDetector(_NoReader(), opts, profile=profile)


def test_detector_origin_and_crop_from_profile():
    det = _detector()
    assert (det._crop_x, det._crop_y, det._crop_w, det._crop_h) == (0, 100, 1152, 980)
    assert det._origin_for(Image.new("RGB", (1152, 980))) == (0, 100)
    assert det._origin_for(Image.new("RGB", (1920, 1080))) == (0, 0)
    prof = DetectorProfile.from_dict({"regions": {"kda": [1500, 40, 1600, 60]}}, name="t")
    det2 = _detector(prof)
    x, y, w, h = prof.crop_box()
    assert det2._origin_for(Image.new("RGB", (w, h))) == (x, y)
    vf, ow, oh = det2._build_coarse_vf(1920, 1080)
    assert vf == f"crop={w}:{h}:{x}:{y}" and (ow, oh) == (w, h)
    vf, ow, oh = det2._build_coarse_vf(1664, 936)
    assert vf == f"scale=1920:1080,crop={w}:{h}:{x}:{y}"
    vf, ow, oh = det2._build_coarse_vf(1280, 720, do_crop=False)
    assert vf == "scale=1920:1080" and (ow, oh) == (1920, 1080)
    assert det._build_coarse_vf(1920, 1080)[0] == "crop=1152:980:0:100", "default profile == old literals"


def test_extract_frame_cmd_scales_non_1080p():
    det = _detector(ffmpeg="ffmpeg", hwaccel="cuda")
    det._source_size = (1920, 1080)
    cmd = det._extract_frame_cmd("x.mp4", 12.3)
    assert "-vf" not in cmd and cmd[:3] == ["ffmpeg", "-v", "error"] and "-hwaccel" in cmd
    det._source_size = (1280, 720)
    cmd = det._extract_frame_cmd("x.mp4", 12.3)
    assert cmd[cmd.index("-vf") + 1] == "scale=1920:1080"
    assert cmd.index("-i") < cmd.index("-vf") < cmd.index("-f")
    det._source_size = None
    assert "-vf" not in det._extract_frame_cmd("x.mp4", 0)


def test_detector_profile_gates_portrait_and_libraries():
    prof = dataclasses.replace(DetectorProfile.default(), portrait_enabled=False)
    det = _detector(prof)
    det._ensure_god_matcher()
    assert det._god_matcher is None and det._god_matcher_init_attempted
    d, _ = _synthetic_icon_lib()
    prof2 = DetectorProfile.from_dict({}, name="t")     # overlay/reference default False
    det2 = _detector(prof2, god_icons_dir=d)
    det2._ensure_god_matcher()
    assert det2._god_matcher is not None
    assert det2._god_matcher._overlay_icons_dir is None and det2._god_matcher._reference_icons_dir is None
    assert det2._god_matcher.portrait_region == DEFAULTS["portrait"]


# ── sorter: keep_stem / no_move / sidecars ────────────────────────────

def test_move_and_emit_keep_stem_and_sidecars():
    root = _tmp()
    v = root / "v123.mp4"
    v.write_bytes(b"0")
    (root / "v123.twitch.json").write_text("{}", encoding="utf-8")
    nv, nj = move_and_emit(v, [], ["Ymir"], root, dry_run=True, keep_stem=True)
    assert nv == root / "Ymir" / "v123.mp4" and v.exists(), "dry run touches nothing"
    nv, nj = move_and_emit(v, [{"timestamp_sec": 1.0, "type": "kill", "note": "",
                                "pre_sec": 7.0, "post_sec": 6.0}], ["Ymir"], root,
                           dry_run=False, keep_stem=True)
    assert nv == root / "Ymir" / "v123.mp4" and nv.exists() and not v.exists()
    assert nj == root / "Ymir" / "v123.events.json" and nj.exists()
    assert (root / "Ymir" / "v123.twitch.json").exists(), "sidecar moved with the video"
    payload = json.loads(nj.read_text(encoding="utf-8"))
    assert payload["source_video"] == str(nv.resolve()) and payload["gods_seen"] == ["Ymir"]
    # collision: a second v123 must not be silently renamed
    v2 = root / "v123.mp4"
    v2.write_bytes(b"1")
    try:
        move_and_emit(v2, [], ["Ymir"], root, dry_run=False, keep_stem=True)
        raise AssertionError("collision must raise")
    except FileExistsError:
        pass
    assert v2.exists()
    # no_move: JSON beside the source, file untouched
    nv, nj = move_and_emit(v2, [], ["Loki"], root, dry_run=False, no_move=True)
    assert nv == v2 and v2.exists() and nj == root / "v123.events.json" and nj.exists()
    # default naming still works
    v3 = root / "2026-01-01 10-00-00.mp4"
    v3.write_bytes(b"2")
    nv, nj = move_and_emit(v3, [], ["Ymir"], root, dry_run=False)
    assert nv == root / "Ymir" / "Ymir-1.mp4" and nv.exists()


# ── harness ───────────────────────────────────────────────────────────

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if not TESTS:
        print("FAIL  no tests were collected")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
