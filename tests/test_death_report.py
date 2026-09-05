"""
Tests for tools/death_report.py (private coaching report over the VOD index).

Self-running script per house convention: exit 0 only on full pass.
Hermetic: temp SQLite index, no network (the Ollama summary is not called).
"""

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import death_report  # noqa: E402
from vodsearch.store import Store  # noqa: E402


def _index():
    s = Store(Path(tempfile.mkdtemp(prefix="death_report_")) / "i.db")
    a = s.upsert_recording("a.mp4", god="Sylvanus", stem="Sylvanus-1", status="done", duration_s=900,
                           recorded_at="2026-07-10T18:00:00")
    b = s.upsert_recording("b.mp4", god="Atlas", stem="Atlas-1", status="done", duration_s=900,
                           recorded_at="2026-06-01T18:00:00")
    s.replace_segments(a, [
        dict(track_index=1, speaker="hatmaster", start_s=80, end_s=82, text="I have no mana at all."),
        dict(track_index=1, speaker="hatmaster", start_s=95, end_s=97, text="Okay, backing up now."),
        dict(track_index=3, speaker="friends", start_s=96, end_s=98, text="get out get out"),
        dict(track_index=1, speaker="hatmaster", start_s=101, end_s=103, text="Oh, I'm dead."),
        dict(track_index=1, speaker="hatmaster", start_s=200, end_s=202, text="No mana again, great."),
        dict(track_index=1, speaker="hatmaster", start_s=400, end_s=402, text="Unrelated chatter."),
    ])
    s.replace_events(a, [{"timestamp_sec": 100.0, "type": "death"}, {"timestamp_sec": 210.0, "type": "death"},
                         {"timestamp_sec": 300.0, "type": "kill", "note": "double kill"}])
    s.replace_segments(b, [dict(track_index=1, speaker="hatmaster", start_s=10, end_s=12, text="Old game.")])
    s.replace_events(b, [{"timestamp_sec": 15.0, "type": "death"}])
    return s


def test_collect_windows_and_speaker_filter():
    s = _index()
    deaths = death_report.collect(s, None, None, before_s=25, after_s=4, include_friends=False)
    assert [d["god"] for d in deaths] == ["Atlas", "Sylvanus", "Sylvanus"]      # ordered by date
    first_sylv = deaths[1]
    assert [l["text"] for l in first_sylv["before"]] == ["I have no mana at all.", "Okay, backing up now."]
    assert [l["text"] for l in first_sylv["after"]] == ["Oh, I'm dead."]
    with_friends = death_report.collect(s, "Sylvanus", None, 25, 4, include_friends=True)
    assert "get out get out" in [l["text"] for l in with_friends[0]["before"]]
    assert len(death_report.collect(s, "Sylvanus", "2026-07-01T00:00:00", 25, 4, False)) == 2
    assert death_report.collect(s, "Atlas", "2026-07-01T00:00:00", 25, 4, False) == []
    s.close()


def test_word_patterns_and_render():
    s = _index()
    deaths = death_report.collect(s, "Sylvanus", None, 25, 4, False)
    pats = dict(death_report.word_patterns(deaths))
    assert pats.get("mana") == 2                     # counted once per death, not per repeat
    assert "the" not in pats and "oh" not in pats
    text = death_report.render(deaths)
    assert text.startswith("2 death(s) across 1 god(s)")
    assert "=== Sylvanus: 2 deaths ===" in text and "mana (2)" in text
    assert "Sylvanus-1 @ 1:40" in text and "you: I have no mana at all." in text
    assert "+ 0:01 you: Oh, I'm dead." in text
    assert death_report.render([]) == "No deaths with transcript in that range."
    assert death_report.fmt_clock(3725) == "1:02:05"
    s.close()


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
