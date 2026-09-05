"""
Tests for the vodsearch package ("Ask the VOD").

Self-running script per house convention: exit 0 only on full pass.
Hermetic: temp SQLite files only, no ffmpeg/ffprobe, no GPU, no
faster_whisper import (transcribe.py is imported for its pure helpers).
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vodsearch.audio import peak_db, rms_db, same_audio, speech_fraction  # noqa: E402
from vodsearch.clips import (MAX_CLIP_S, MIN_CLIP_S, build_clip_cmd,  # noqa: E402
                             clip_name, clip_window)
from vodsearch.indexer import (discover, fmt_hms, god_for, load_sidecar,  # noqa: E402
                               recorded_at_for, sidecar_path)
from vodsearch.store import EVENT_WINDOW_S, Store, build_match, event_kind  # noqa: E402
from vodsearch.transcribe import Segment, clean_segments  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────

def _store():
    d = tempfile.mkdtemp(prefix="vodsearch_test_")
    return Store(Path(d) / "t.db")


def _seed(store):
    """One Ymir recording with two spoken lines and two events; one Loki
    recording with one line and no events. Returns (ymir_id, loki_id)."""
    y = store.upsert_recording(r"C:\rec\Ymir\Ymir-1.mp4", god="Ymir", gods_seen=["Ymir"],
                               duration_s=1500, size_bytes=10, mtime=5.0,
                               recorded_at="2026-07-15T17:10:48", model="large-v3",
                               status="done")
    store.replace_segments(y, [
        dict(track_index=1, speaker="hatmaster", start_s=100, end_s=104,
             text="That was a really nice trap, glad it didn't hit me"),
        dict(track_index=1, speaker="hatmaster", start_s=1300, end_s=1305,
             text="double kill let's go"),
        dict(track_index=3, speaker="friends", start_s=1306, end_s=1308,
             text="nice one"),
    ])
    store.replace_events(y, [
        {"timestamp_sec": 1302.7, "type": "kill", "note": "kill + kill", "pre_sec": 7, "post_sec": 14,
         "merged": [{"type": "kill", "timestamp_sec": 1309.1, "note": "double kill"}]},
        {"timestamp_sec": 625.4, "type": "death", "note": "", "pre_sec": 7, "post_sec": 6},
        {"timestamp_sec": 0.0, "type": "game_start", "note": ""},
    ], god="Ymir")
    lk = store.upsert_recording(r"C:\rec\Loki\Loki-2.mp4", god="Loki", gods_seen=["Loki"],
                                duration_s=900, size_bytes=20, mtime=6.0,
                                recorded_at="2026-07-16T18:00:00", model="large-v3",
                                status="done")
    store.replace_segments(lk, [
        dict(track_index=1, speaker="hatmaster", start_s=50, end_s=53,
             text="a trap in the jungle again"),
    ])
    return y, lk


# ── store: writes ─────────────────────────────────────────────────────

def test_upsert_recording_is_idempotent_on_path():
    s = _store()
    a = s.upsert_recording("x.mp4", god="Ymir", gods_seen=["Ymir"], status="pending")
    b = s.upsert_recording("x.mp4", status="done", duration_s=12.5)
    assert a == b
    rec = s.get_recording(a)
    assert rec["god"] == "Ymir"                 # earlier field kept
    assert rec["status"] == "done"              # later field updated
    assert json.loads(rec["gods_seen"]) == ["Ymir"]
    s.close()


def test_upsert_ignores_unknown_keys():
    s = _store()
    rid = s.upsert_recording("y.mp4", bogus="nope", god="Loki")
    assert s.get_recording(rid)["god"] == "Loki"
    s.close()


def test_is_current_requires_done_same_size_mtime_model():
    s = _store()
    s.upsert_recording("z.mp4", status="done", size_bytes=100, mtime=50.0, model="large-v3")
    assert s.is_current("z.mp4", 100, 50.4, "large-v3")
    assert not s.is_current("z.mp4", 101, 50.0, "large-v3")
    assert not s.is_current("z.mp4", 100, 52.0, "large-v3")
    assert not s.is_current("z.mp4", 100, 50.0, "small")
    assert not s.is_current("missing.mp4", 100, 50.0, "large-v3")
    s.set_status(s.find_recording("z.mp4")["id"], "error", "boom")
    assert not s.is_current("z.mp4", 100, 50.0, "large-v3")
    s.close()


def test_replace_segments_drops_blank_and_scopes_by_track():
    s = _store()
    rid = s.upsert_recording("a.mp4", status="done")
    n = s.replace_segments(rid, [
        dict(track_index=1, speaker="h", start_s=1, end_s=2, text="one"),
        dict(track_index=1, speaker="h", start_s=3, end_s=4, text="   "),
        dict(track_index=3, speaker="f", start_s=5, end_s=6, text="three"),
    ])
    assert n == 2
    # replacing only track 1 keeps track 3's row
    s.replace_segments(rid, [dict(speaker="h", start_s=7, end_s=8, text="uno")], track_index=1)
    rows = s.conn.execute("SELECT track_index, text FROM segments ORDER BY start_s").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(3, "three"), (1, "uno")]
    s.close()


def test_replace_events_classifies_kinds_and_skips_junk():
    s = _store()
    rid = s.upsert_recording("b.mp4", status="done")
    n = s.replace_events(rid, [
        {"timestamp_sec": 10, "type": "kill", "note": "triple kill"},
        {"timestamp_sec": 20, "type": "kill", "note": ""},
        {"timestamp_sec": 30, "type": "death"},
        {"timestamp_sec": "nan?", "type": "kill"},      # unparsable ts
        {"timestamp_sec": 40},                           # no type
    ], god="Ymir")
    assert n == 3
    kinds = [r[0] for r in s.conn.execute("SELECT kind FROM events ORDER BY ts_s")]
    assert kinds == ["multikill", "kill", "death"]
    assert s.get_event(1)["god"] == "Ymir"
    s.close()


def test_delete_recording_removes_children():
    s = _store()
    y, _ = _seed(s)
    s.delete_recording(y)
    assert s.get_recording(y) is None
    assert s.conn.execute("SELECT COUNT(*) FROM segments WHERE recording_id=?", (y,)).fetchone()[0] == 0
    assert s.conn.execute("SELECT COUNT(*) FROM events WHERE recording_id=?", (y,)).fetchone()[0] == 0
    # FTS content stays consistent: the deleted text no longer matches
    assert s.search("trap")["total"] == 1
    s.close()


# ── store: reads ──────────────────────────────────────────────────────

def test_search_and_then_or_fallback():
    s = _store()
    _seed(s)
    r = s.search("nice trap")
    assert r["mode"] == "and" and r["total"] == 1
    m = r["moments"][0]
    assert m["god"] == "Ymir" and m["speaker"] == "hatmaster"
    assert "<mark>nice</mark>" in m["snippet"] and "<mark>trap</mark>" in m["snippet"]
    assert m["events"] == []                    # nothing within 20s of t=100
    r2 = s.search("nice zebra")
    assert r2["mode"] == "or" and r2["total"] >= 1
    assert s.search("zebra unicorn")["total"] == 0
    assert s.search("   ")["total"] == 0
    s.close()


def test_search_attaches_nearby_events_and_filters_by_kind():
    s = _store()
    _seed(s)
    r = s.search("double kill")
    assert r["total"] == 1
    kinds = [e["kind"] for e in r["moments"][0]["events"]]
    assert kinds == ["multikill"]
    assert s.search("trap", event="kill")["total"] == 0
    assert s.search("kill", event="kill")["total"] == 1       # kill includes multikill
    assert s.search("kill", event="multikill")["total"] == 1
    assert s.search("kill", event="death")["total"] == 0
    assert s.search("nice", event="any")["total"] == 1        # only the friends line at 1306
    s.close()


def test_search_filters_by_god_and_speaker_and_pages():
    s = _store()
    _seed(s)
    assert s.search("trap", god="Loki")["total"] == 1
    assert s.search("trap", god="Ymir")["total"] == 1
    assert s.search("trap")["total"] == 2
    assert s.search("nice", speaker="friends")["total"] == 1
    page = s.search("trap", limit=1, offset=1)
    assert page["total"] == 2 and len(page["moments"]) == 1
    s.close()


def test_browse_lists_events_newest_recording_first_with_text():
    s = _store()
    _seed(s)
    b = s.browse()
    assert b["mode"] == "browse" and b["total"] == 2         # game_start excluded
    first = b["moments"][0]
    assert first["events"][0]["kind"] == "multikill"
    assert "double kill" in first["text"] and "nice one" in first["text"]
    assert abs(first["start_s"] - (1302.7 - 7)) < 1e-6
    assert first["segment_id"] is not None
    death = b["moments"][1]
    assert death["events"][0]["kind"] == "death" and death["text"] == ""
    assert s.browse(event="death")["total"] == 1
    assert s.browse(god="Loki")["total"] == 0
    s.close()


def test_events_near_and_segments_near():
    s = _store()
    y, _ = _seed(s)
    near = s.events_near(y, 1300.0)
    assert [e["kind"] for e in near] == ["multikill"]
    assert s.events_near(y, 5.0) == []                       # game_start excluded
    segs = s.segments_near(y, 1305.0, window_s=5)
    assert [x["text"] for x in segs] == ["double kill let's go", "nice one"]
    s.close()


def test_stats_and_gods():
    s = _store()
    _seed(s)
    s.upsert_recording("pending.mp4", god="Geb", status="pending", duration_s=3600)
    st = s.stats()
    assert st["recordings"] == 2
    assert abs(st["hours"] - round(2400 / 3600, 1)) < 1e-6
    assert st["segments"] == 4
    assert st["events"] == {"multikill": 1, "death": 1, "game_start": 1}
    assert st["by_status"] == {"done": 2, "pending": 1}
    assert st["oldest"] == "2026-07-15T17:10:48" and st["newest"] == "2026-07-16T18:00:00"
    gods = {g["god"]: g["recordings"] for g in st["gods"]}
    assert gods == {"Ymir": 1, "Loki": 1}                    # pending Geb excluded
    s.close()


def test_get_segment_and_event_join_recording_fields():
    s = _store()
    y, _ = _seed(s)
    seg = s.get_segment(1)
    assert seg["path"].endswith("Ymir-1.mp4") and seg["duration_s"] == 1500
    ev = s.get_event(1)
    assert ev["recording_id"] == y and ev["kind"] == "multikill"
    assert s.get_segment(999) is None and s.get_event(999) is None
    s.close()


# ── query building ────────────────────────────────────────────────────

def test_build_match_quotes_terms_phrases_and_prefixes():
    assert build_match("nice trap") == '"nice" AND "trap"'
    assert build_match("nice trap", "or") == '"nice" OR "trap"'
    assert build_match('"first blood" wow*') == '"first blood" AND "wow"*'
    assert build_match("don't") == '"don\'t"'
    assert build_match("") == ""
    assert build_match("!!! ???") == ""
    assert build_match('a "b') == '"a" AND "b"'              # stray quote survives


def test_event_kind():
    assert event_kind("kill") == "kill"
    assert event_kind("kill", "double kill") == "multikill"
    assert event_kind("kill", "", [{"type": "kill"}]) == "kill"           # no label, no timestamps
    assert event_kind("kill", "", [{"type": "kill", "timestamp_sec": 8.0}], ts=1.0) == "multikill"
    assert event_kind("kill", "", [{"type": "assist", "note": "penta kill"}]) == "multikill"
    assert event_kind("Death") == "death"
    assert event_kind("") == "unknown"


# ── transcript cleaning ───────────────────────────────────────────────

def test_clean_segments_filters_junk_and_loops():
    segs = [
        Segment(0, 1, "  "),
        Segment(1, 2, "Thank you."),
        Segment(2, 3, "Thanks for watching!"),
        Segment(3, 4, "real line", no_speech_prob=0.9, avg_logprob=-0.2),   # confident enough
        Segment(4, 5, "ghost", no_speech_prob=0.9, avg_logprob=-1.5),       # both weak -> drop
        Segment(6, 5, "backwards"),
        Segment(7, 8, "Let's go"),
        Segment(8, 9, "lets go!"),
        Segment(9, 10, "LETS GO"),
        Segment(10, 11, "let's go"),
        Segment(11, 12, "different"),
    ]
    out = clean_segments(segs)
    assert [x.text for x in out] == ["real line", "Let's go", "lets go!", "different"]
    assert all(isinstance(x.start, float) for x in out)


# ── clips ─────────────────────────────────────────────────────────────

def test_clip_window_clamps_and_pads():
    assert clip_window(10.0, None, 100.0) == (2.0, 22.0)
    assert clip_window(10.0, 20.0, 100.0) == (2.0, 32.0)
    assert clip_window(3.0, None, 100.0)[0] == 0.0
    assert clip_window(95.0, None, 100.0)[1] == 100.0
    a, b = clip_window(0.0, 500.0, 1000.0)
    assert b - a == MAX_CLIP_S
    a, b = clip_window(99.0, None, 100.0, pre_s=0, post_s=0.5)
    assert b - a >= MIN_CLIP_S - 1e-6 and b <= 100.0
    a, b = clip_window(10.0, None, 0.0)                     # unknown duration: no upper clamp
    assert (a, b) == (2.0, 22.0)


def test_clip_name_is_deterministic_and_distinct():
    n1 = clip_name(7, 12.34, 30.0, 720, (0, 1))
    assert n1 == clip_name(7, 12.34, 30.0, 720, (0, 1))
    assert n1.startswith("r7_") and n1.endswith(".mp4")
    assert n1 != clip_name(7, 12.35, 30.0, 720, (0, 1))
    assert n1 != clip_name(7, 12.34, 30.0, 480, (0, 1))
    assert n1 != clip_name(7, 12.34, 30.0, 720, (0, 1, 2))
    assert "/" not in n1 and "\\" not in n1


def test_build_clip_cmd_shapes():
    cmd = build_clip_cmd("ffmpeg", "src.mp4", 5.0, 25.0, "out.mp4", (0, 1))
    assert cmd[0] == "ffmpeg" and cmd[-1] == "out.mp4"
    assert cmd.index("-ss") < cmd.index("-i")
    assert "-filter_complex" in cmd and "amix=inputs=2" in cmd[cmd.index("-filter_complex") + 1]
    assert "h264_nvenc" in cmd and "libx264" not in cmd
    assert "+faststart" in cmd
    single = build_clip_cmd("ffmpeg", "src.mp4", 5.0, 25.0, "o.mp4", (2,), encoder="libx264")
    assert "-filter_complex" not in single and "0:a:2" in single
    assert "libx264" in single and "h264_nvenc" not in single
    silent = build_clip_cmd("ffmpeg", "src.mp4", 5.0, 25.0, "o.mp4", ())
    assert "-an" in silent


# ── indexer helpers ───────────────────────────────────────────────────

def test_discover_skips_root_dotdirs_and_orders_newest_first():
    root = Path(tempfile.mkdtemp(prefix="vod_discover_"))
    (root / "Ymir").mkdir()
    (root / ".tiktok_bg").mkdir()
    (root / "processed").mkdir()
    files = {
        "root.mp4": root / "root.mp4",
        "old": root / "Ymir" / "Ymir-1.mp4",
        "new": root / "Ymir" / "Ymir-2.mp4",
        "dot": root / ".tiktok_bg" / "bg.mp4",
        "skip": root / "processed" / "p.mp4",
        "txt": root / "Ymir" / "notes.txt",
    }
    for p in files.values():
        p.write_bytes(b"0")
    os.utime(files["old"], (1_000, 1_000))
    os.utime(files["new"], (2_000, 2_000))
    found = discover(root, include_root=False, skip_dirs=(".tiktok_bg", "processed"))
    assert found == [files["new"], files["old"]]
    with_root = discover(root, include_root=True, skip_dirs=(".tiktok_bg", "processed"))
    assert files["root.mp4"] in with_root and len(with_root) == 3
    assert discover(root / "nope") == []


def test_sidecar_helpers():
    assert sidecar_path(Path("x/Ymir-1.mp4")) == Path("x/Ymir-1.events.json")
    d = Path(tempfile.mkdtemp(prefix="vod_sidecar_"))
    mp4 = d / "Ah Puch-1.mp4"
    assert load_sidecar(mp4) == ([], [])
    (d / "Ah Puch-1.events.json").write_text(json.dumps({
        "gods_seen": ["Ah Puch", 7], "events": [{"timestamp_sec": 1, "type": "kill"}, "junk"]}),
        encoding="utf-8")
    gods, events = load_sidecar(mp4)
    assert gods == ["Ah Puch"] and events == [{"timestamp_sec": 1, "type": "kill"}]
    (d / "Ah Puch-1.events.json").write_text("{not json", encoding="utf-8")
    assert load_sidecar(mp4) == ([], [])


def test_recorded_at_for_prefers_filename_then_mtime_minus_duration():
    assert recorded_at_for(Path("2026-07-15_17-10-48.mp4"), 999, 0) == "2026-07-15T17:10:48"
    assert recorded_at_for(Path("2026-07-15 17-10-48.mp4"), 999, 0) == "2026-07-15T17:10:48"
    mtime = 1_800_000_000.0
    expect = (datetime.fromtimestamp(mtime) - timedelta(seconds=120)).replace(microsecond=0).isoformat()
    assert recorded_at_for(Path("Ymir-3.mp4"), 120, mtime) == expect


def test_god_for_precedence():
    root = Path(r"C:\rec")
    assert god_for(root / "unknown" / "u-1.mp4", ["Ymir"], root) == "Ymir"
    assert god_for(root / "Ymir" / "Ymir-1.mp4", [], root) == "Ymir"
    assert god_for(root / "Ymir" / "Ymir-1.mp4", ["Ymir", "Loki"], root) == "Ymir"
    assert god_for(root / "mixed" / "mixed-1.mp4", ["Ymir", "Loki"], root) is None
    assert god_for(root / "Unknown" / "x.mp4", [], root) is None
    assert god_for(root / "root.mp4", [], root) is None


def test_fmt_hms():
    assert fmt_hms(65) == "1:05"
    assert fmt_hms(3725) == "1:02:05"
    assert fmt_hms(-3) == "0:00"


# ── audio math ────────────────────────────────────────────────────────

def test_levels_and_speech_fraction():
    assert rms_db(np.zeros(100, dtype=np.float32)) == -120.0
    half = np.full(16000, 0.5, dtype=np.float32)
    assert abs(rms_db(half) + 6.02) < 0.1
    assert abs(peak_db(half) + 6.02) < 0.1
    t = np.arange(16000) / 16000.0
    sine = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    data = np.concatenate([np.zeros(16000, dtype=np.float32), sine])
    assert abs(speech_fraction(data, 16000) - 0.5) < 0.1
    assert speech_fraction(np.zeros(0, dtype=np.float32)) == 0.0


def test_same_audio():
    a = np.linspace(-1, 1, 50_000, dtype=np.float32)
    assert same_audio(a, a.copy())
    assert not same_audio(a, a * 0.5)
    assert not same_audio(a, a[:-1])
    assert not same_audio(np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32))


# ── sentence splitting + render attempts ──────────────────────────────

def test_split_sentences_breaks_on_punctuation_length_and_passthrough():
    from vodsearch.clips import render_attempts
    from vodsearch.transcribe import Word, split_sentences
    ws = [Word(0.0, 0.3, " Hello"), Word(0.3, 0.6, " there."), Word(0.7, 1.0, " How"),
          Word(1.0, 1.2, " are"), Word(1.2, 1.5, " you?"), Word(1.6, 1.9, " Fine")]
    out = split_sentences([Segment(0, 1.9, "Hello there. How are you? Fine", words=ws)])
    assert [(s.start, s.end, s.text) for s in out] == [
        (0.0, 0.6, "Hello there."), (0.7, 1.5, "How are you?"), (1.6, 1.9, "Fine")]
    long = [Word(i * 1.0, i * 1.0 + 0.9, " word") for i in range(40)]
    pieces = split_sentences([Segment(0, 40, "x", words=long)], max_len_s=15)
    assert len(pieces) == 3 and pieces[0].end <= 15.0 and pieces[-1].text.count("word") == 10
    assert split_sentences([Segment(0, 5, "no words")])[0].text == "no words"
    assert render_attempts("h264_nvenc") == [("cuda", "h264_nvenc"), (None, "h264_nvenc"), (None, "libx264")]
    assert render_attempts("libx264") == [(None, "libx264")]
    cmd = build_clip_cmd("ffmpeg", "s.mp4", 1, 5, "o.mp4", (0,), hwaccel="cuda")
    assert cmd.index("-hwaccel") < cmd.index("-ss") and "cuda" in cmd


def test_build_stream_cmd_writes_fragmented_mp4_to_stdout():
    from vodsearch.clips import build_stream_cmd
    cmd = build_stream_cmd("ffmpeg", "src.mp4", 754.2, 600, (0, 1, 3), hwaccel="cuda")
    assert cmd[-1] == "pipe:1" and cmd[cmd.index("-f") + 1] == "mp4"
    assert "frag_keyframe+empty_moov+default_base_moof" in cmd
    assert cmd.index("-hwaccel") < cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "754.200" and cmd[cmd.index("-t") + 1] == "600.000"
    assert "amix=inputs=3" in cmd[cmd.index("-filter_complex") + 1]
    assert "h264_nvenc" in cmd
    soft = build_stream_cmd("ffmpeg", "src.mp4", -5, 0.2, (1,), encoder="libx264")
    assert soft[soft.index("-ss") + 1] == "0.000" and soft[soft.index("-t") + 1] == "1.000"
    assert "libx264" in soft and "-hwaccel" not in soft


# ── multikill tiers ───────────────────────────────────────────────────

def test_event_tier_and_labels():
    from vodsearch.store import event_tier, tier_label
    assert event_tier("kill") == 1
    assert event_tier("kill", "double kill") == 2
    # "kill + kill" is the overlap merger listing clip windows, NOT a streak
    assert event_tier("kill", "kill + kill") == 1
    assert event_tier("kill", "first blood (kill + kill + kill + kill)") == 1
    # the detector's own label inside the merged group is the truth
    assert event_tier("kill", "kill + kill", [{"type": "kill", "note": "double kill"}]) == 2
    assert event_tier("kill", "kill + kill + kill + kill",
                      [{"type": "kill", "note": "double kill"}, {"type": "kill", "note": "triple kill"},
                       {"type": "kill", "note": ""}]) == 3
    assert event_tier("kill", "", [{"type": "assist", "note": "penta kill"}]) == 5
    # no labels at all: fall back to the 10 s chain rule on timestamps
    assert event_tier("kill", "", [{"type": "kill", "timestamp_sec": 104.0},
                                   {"type": "kill", "timestamp_sec": 113.0}], ts=100.0) == 3
    assert event_tier("kill", "", [{"type": "kill", "timestamp_sec": 104.0},
                                   {"type": "kill", "timestamp_sec": 130.0}], ts=100.0) == 2
    assert event_tier("kill", "", [{"type": "kill", "timestamp_sec": 115.0}], ts=100.0) == 1
    assert event_tier("death", "double") == 0
    assert tier_label(1) == "Kill" and tier_label(4) == "Quadra kill" and tier_label(0) == ""
    assert event_kind("kill", "kill + kill + kill") == "kill"
    assert event_kind("kill", "kill + kill", [{"type": "kill", "note": "double kill"}]) == "multikill"


def test_browse_multikills_sort_by_tier_and_tier_filters():
    s = _store()
    rid = s.upsert_recording("m.mp4", god="Ymir", status="done", duration_s=2000,
                             recorded_at="2026-07-01T10:00:00")
    s.replace_events(rid, [
        {"timestamp_sec": 100, "type": "kill", "note": "double kill"},
        {"timestamp_sec": 200, "type": "kill", "note": "penta kill"},
        {"timestamp_sec": 300, "type": "kill", "note": "kill + kill + kill",
         "merged": [{"type": "kill", "timestamp_sec": 303, "note": "double kill"},
                    {"type": "kill", "timestamp_sec": 307, "note": "triple kill"}]},
        {"timestamp_sec": 400, "type": "kill", "note": ""},
        {"timestamp_sec": 500, "type": "death", "note": ""},
    ])
    s.replace_segments(rid, [dict(track_index=1, speaker="hatmaster", start_s=99, end_s=101, text="go go"),
                             dict(track_index=1, speaker="hatmaster", start_s=299, end_s=301, text="go go go")])
    b = s.browse(event="multikill")
    assert [m["events"][0]["label"] for m in b["moments"]] == ["Penta kill", "Triple kill", "Double kill"]
    assert [m["events"][0]["tier"] for m in b["moments"]] == [5, 3, 2]
    assert s.browse(event="penta")["total"] == 1 and s.browse(event="quadra")["total"] == 0
    assert s.browse(event="kill")["total"] == 4                       # singles included, chronological
    r = s.search("go", event="multikill")
    assert [m["events"][0]["label"] for m in r["moments"]] == ["Triple kill", "Double kill"]
    assert s.search("go", event="triple")["total"] == 1
    assert s.search("go", event="double")["total"] == 1
    assert s.search("go", event="penta")["total"] == 0
    s.close()


def test_migration_adds_tier_to_old_index():
    import sqlite3 as _sq
    d = tempfile.mkdtemp(prefix="vod_migrate_")
    db = Path(d) / "old.db"
    con = _sq.connect(db)
    con.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, recording_id INTEGER NOT NULL, ts_s REAL NOT NULL,
            type TEXT NOT NULL, kind TEXT NOT NULL, note TEXT, pre_s REAL DEFAULT 0, post_s REAL DEFAULT 0, god TEXT);
        INSERT INTO events (recording_id, ts_s, type, kind, note) VALUES (1, 10, 'kill', 'multikill', 'kill + kill + kill');
        INSERT INTO events (recording_id, ts_s, type, kind, note) VALUES (1, 20, 'kill', 'kill', '');
    """)
    con.commit(); con.close()
    s = Store(db)
    rows = s.conn.execute("SELECT tier, kind FROM events ORDER BY ts_s").fetchall()
    # notes alone can't prove a streak (labels live in the merged group,
    # which the DB doesn't keep), so the heal demotes to a plain kill and
    # `vod_index.py events` restores tiers from the sidecars
    assert [(r[0], r[1]) for r in rows] == [(1, "kill"), (1, "kill")]
    s.close()


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
