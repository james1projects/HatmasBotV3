"""
Tests for vodsearch/trim.py + the Store's trim tables (the /vod/trim page).

Self-running script per house convention: exit 0 only on full pass.
Hermetic: temp SQLite + temp folders, no ffmpeg/ffprobe (render_job gets
a fake runner), no GPU, no network.
"""

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vodsearch import trim  # noqa: E402
from vodsearch.store import Store  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────

def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="trim_test_"))


def _store(root: Path) -> Store:
    return Store(root / "t.db")


def _seed(store: Store, root: Path):
    """Two recordings: a 30 GB Atlas game with a single kill, a double
    kill and a death; a small replay clip with one kill. Files exist on
    disk so trash can move them."""
    atlas = root / "Atlas" / "Atlas-3.mp4"
    atlas.parent.mkdir(parents=True)
    atlas.write_bytes(b"x" * 100)
    atlas.with_name("Atlas-3.events.json").write_text("{}", encoding="utf-8")
    a = store.upsert_recording(atlas, god="Atlas", gods_seen=["Atlas"], folder="Atlas",
                               stem="Atlas-3", duration_s=1500, size_bytes=30_000_000_000,
                               mtime=5.0, recorded_at="2026-08-10T17:07:52", model="large-v3",
                               status="done")
    store.replace_events(a, [
        {"timestamp_sec": 300.0, "type": "kill", "note": "", "pre_sec": 7, "post_sec": 6},
        {"timestamp_sec": 900.0, "type": "kill", "note": "double kill", "pre_sec": 7, "post_sec": 14,
         "merged": [{"type": "kill", "timestamp_sec": 905.0, "note": "double kill"}]},
        {"timestamp_sec": 1200.0, "type": "death", "note": "", "pre_sec": 7, "post_sec": 6},
        {"timestamp_sec": 0.0, "type": "game_start", "note": ""},
    ], god="Atlas")
    replay = root / "unknown" / "2026-08-10_19-30-31_Replay.mp4"
    replay.parent.mkdir(parents=True)
    replay.write_bytes(b"y" * 50)
    r = store.upsert_recording(replay, god=None, gods_seen=[], folder="unknown",
                               stem="2026-08-10_19-30-31_Replay", duration_s=60,
                               size_bytes=500_000_000, mtime=6.0,
                               recorded_at="2026-08-10T19:30:31", model="large-v3", status="done")
    store.replace_events(r, [{"timestamp_sec": 40.0, "type": "kill", "note": "", "pre_sec": 7, "post_sec": 6}])
    return a, r


def _rows(store: Store):
    rows = store.trim_list()
    for row in rows:
        trim.annotate(row, 2)
    return {row["id"]: row for row in rows}


# ── tests ─────────────────────────────────────────────────────────────

def test_trim_list_is_largest_first_with_kills_only():
    root = _tmp(); store = _store(root); a, r = _seed(store, root)
    rows = store.trim_list()
    assert [x["id"] for x in rows] == [a, r], "largest recording must come first"
    atlas = rows[0]
    assert [m["ts_s"] for m in atlas["moments"]] == [300.0, 900.0], "kills only, in time order (no death, no game_start)"
    assert atlas["moments"][1]["tier"] == 2 and atlas["moments"][1]["label"] == "Double kill"
    assert atlas["clips_rendered"] == 0


def test_default_decisions_keep_multikills_only():
    root = _tmp(); store = _store(root); a, r = _seed(store, root)
    atlas = _rows(store)[a]
    eff = {m["ts_s"]: m["effective"] for m in atlas["moments"]}
    assert eff == {300.0: "skip", 900.0: "keep"}, eff
    assert atlas["kept"] == 1 and atlas["kept_rendered"] == 0 and atlas["pending_render"] == 1
    assert atlas["can_trash"] is False, "a kept moment without a clip blocks trashing"


def test_explicit_decision_overrides_default_and_persists():
    root = _tmp(); store = _store(root); a, r = _seed(store, root)
    atlas = _rows(store)[a]
    single = next(m for m in atlas["moments"] if m["ts_s"] == 300.0)
    double = next(m for m in atlas["moments"] if m["ts_s"] == 900.0)
    assert store.trim_decide([single["event_id"]], "keep") == 1
    assert store.trim_decide([double["event_id"]], "skip") == 1
    atlas = _rows(store)[a]
    eff = {m["ts_s"]: m["effective"] for m in atlas["moments"]}
    assert eff == {300.0: "keep", 900.0: "skip"}, eff
    # flipping again updates in place (upsert), and unknown ids are ignored
    assert store.trim_decide([single["event_id"], 999999], "skip") == 1
    assert _rows(store)[a]["kept"] == 0
    try:
        store.trim_decide([single["event_id"]], "maybe")
        assert False, "bad decision must raise"
    except ValueError:
        pass


def test_nothing_kept_means_trashable_and_keep_whole_blocks_it():
    root = _tmp(); store = _store(root); a, r = _seed(store, root)
    atlas = _rows(store)[a]
    store.trim_decide([m["event_id"] for m in atlas["moments"]], "skip")
    assert _rows(store)[a]["can_trash"] is True, "no kept moments = nothing to render = trashable"
    store.trim_keep_whole(a, True)
    row = _rows(store)[a]
    assert row["keep_whole"] == 1 and row["can_trash"] is False
    store.trim_keep_whole(a, False)
    assert _rows(store)[a]["can_trash"] is True


def test_replay_clips_are_never_trashable():
    root = _tmp(); store = _store(root); a, r = _seed(store, root)
    rep = _rows(store)[r]
    store.trim_decide([m["event_id"] for m in rep["moments"]], "skip")
    rep = _rows(store)[r]
    assert rep["replay"] is True and rep["can_trash"] is False


def test_moment_window_and_filename():
    m = {"ts_s": 900.0, "tier": 2, "pre_s": 7.0, "post_s": 14.0}
    start, end = trim.moment_window(m, 1500.0, 15.0, 10.0, 180.0)
    assert start == 885.0, start                      # 15 s of context beats the detector's 7
    assert end == 900.0 + 14.0 + trim.TIER_BONUS_S, end  # detector's 14 beats the 10 default, +5 per extra kill
    # clamps at the ends of the recording and at max length
    assert trim.moment_window({"ts_s": 3.0, "tier": 1}, 100.0, 15.0, 10.0, 180.0) == (0.0, 13.0)
    s, e = trim.moment_window({"ts_s": 500.0, "tier": 5, "post_s": 400.0}, 1500.0, 15.0, 10.0, 180.0)
    assert e - s == 180.0, (s, e)
    assert trim.clip_filename("Atlas-3", 900.0, 2) == "Atlas-3_15m00s_double-kill.mp4"
    assert trim.clip_filename("Hou Yi-12", 61.9, 1) == "Hou Yi-12_01m01s_kill.mp4"
    assert trim.clip_filename("bad:name?", 0, 5) == "bad_name__00m00s_penta-kill.mp4"


def test_plan_jobs_only_for_kept_unrendered_moments():
    root = _tmp(); store = _store(root); a, r = _seed(store, root)
    atlas = _rows(store)[a]
    jobs = trim.plan_jobs(atlas, root / "clips", 15.0, 10.0, 180.0, 2)
    assert len(jobs) == 1 and jobs[0]["ts_s"] == 900.0
    assert jobs[0]["out"] == root / "clips" / "Atlas" / "Atlas-3_15m00s_double-kill.mp4"
    assert jobs[0]["start"] == 885.0 and jobs[0]["src"] == atlas["path"]


def test_build_trim_cmd_keeps_resolution_and_copies_audio():
    cmd = trim.build_trim_cmd("ffmpeg", "in.mp4", 885.0, 919.0, "out.mp4", "hevc_nvenc", 22, "30M", hwaccel="cuda")
    assert "-vf" not in cmd, "full resolution: no scale filter"
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert "0:a?" in cmd and "0:v:0" in cmd
    assert cmd[cmd.index("-ss") + 1] == "885.000" and cmd[cmd.index("-t") + 1] == "34.000"
    assert "hevc_nvenc" in cmd and "-cq" in cmd and cmd[cmd.index("-cq") + 1] == "22"
    assert cmd[cmd.index("-hwaccel") + 1] == "cuda"
    soft = trim.build_trim_cmd("ffmpeg", "in.mp4", 0, 10, "o.mp4", "libx264")
    assert "libx264" in soft and "-hwaccel" not in soft and "-crf" in soft


def test_render_job_falls_back_and_verifies():
    root = _tmp()
    job = {"event_id": 1, "recording_id": 1, "src": "in.mp4", "start": 10.0, "end": 40.0,
           "out": root / "clips" / "Atlas" / "a.mp4", "tier": 2, "label": "Double kill",
           "ts_s": 25.0, "god": "Atlas", "stem": "Atlas-3", "recorded_at": "2026-08-10",
           "recording_path": "in.mp4"}
    calls = []

    def runner(cmd):
        calls.append(cmd)
        if "hevc_nvenc" in cmd:
            return 1, "nvenc unavailable"           # both GPU attempts fail
        Path(cmd[-1]).write_bytes(b"clip")           # libx264 succeeds
        return 0, ""

    res = trim.render_job(job, "ffmpeg", "", "hevc_nvenc", 22, "30M", runner=runner)
    assert res["ok"] is True and res["encoder"] == "libx264" and res["size_bytes"] == 4
    assert Path(job["out"]).exists() and not Path(job["out"]).with_name("a.part.mp4").exists()
    assert len(calls) == 3, "cuda+nvenc, cpu+nvenc, then libx264"

    def never(cmd):
        return 1, "boom"
    bad = dict(job, out=root / "clips" / "b.mp4")
    res = trim.render_job(bad, "ffmpeg", "", "hevc_nvenc", 22, "30M", runner=never)
    assert res["ok"] is False and "boom" in res["error"] and not Path(bad["out"]).exists()


def test_record_clip_then_trash_moves_files_and_drops_index_row():
    root = _tmp(); store = _store(root); a, r = _seed(store, root)
    atlas = _rows(store)[a]
    job = trim.plan_jobs(atlas, root / "clips", 15.0, 10.0, 180.0, 2)[0]
    res = dict(job, ok=True, size_bytes=1234, error=None)
    trim.record_clip(store, res)
    assert store.trim_clips(a)[0]["clip_path"] == str(job["out"])
    atlas = _rows(store)[a]
    assert atlas["kept_rendered"] == 1 and atlas["clips_rendered"] == 1 and atlas["can_trash"] is True
    assert atlas["moments"][1]["clip_path"] == str(job["out"])
    src = Path(atlas["path"])
    trash = root / "_trash"
    out = trim.trash_recording(store, atlas, trash)
    assert out["moved_bytes"] == 100 + 2
    assert not src.exists() and not src.with_name("Atlas-3.events.json").exists()
    assert (trash / "Atlas" / "Atlas-3.mp4").exists() and (trash / "Atlas" / "Atlas-3.events.json").exists()
    assert store.get_recording(a) is None, "trashed recording leaves the index"
    assert store.trim_clips()[0]["recording_path"] == str(src), "clip record survives the recording"
    files, nbytes = trim.trash_contents(trash)
    assert (files, nbytes) == (2, 102)
    assert trim.empty_trash(trash) == (2, 102)
    assert trim.trash_contents(trash) == (0, 0)


def test_trash_refuses_unready_recording():
    root = _tmp(); store = _store(root); a, r = _seed(store, root)
    atlas = _rows(store)[a]
    try:
        trim.trash_recording(store, atlas, root / "_trash")
        assert False, "must refuse while a kept moment has no clip"
    except PermissionError:
        pass
    assert Path(atlas["path"]).exists() and store.get_recording(a) is not None


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
