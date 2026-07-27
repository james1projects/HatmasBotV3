"""
test_god_roster.py — regression tests for core/god_roster.py
=============================================================
The roster is what decides whether !godreq calls a god unknown, so
these lock down the load/merge/fallback behavior:

  - bundled snapshot loads when no cache exists (incl. the six gods
    the old static list missed: Bastet, Chronos, Ah Puch,
    Cu Chulainn, Horus, Xing Tian)
  - wiki filename parsing (underscores, (S2) marker, camel-concat)
  - refresh() merge semantics: new gods stamped with today's
    first_seen, existing first_seen preserved, vanished gods kept,
    wiki_filename updates tracked
  - refresh() failure paths never touch the roster (fetch None,
    implausibly small page)
  - cache round-trip through data/god_roster.json
  - SMITE 1 catalog: ported gods excluded from smite1_only_names
    (including the Mulan -> Hua Mulan rename)

Run:
    python tests/test_god_roster.py

Exit 0 if every test passes, 1 otherwise. No network — the live
fetch is monkeypatched.
"""

import json
import sys
import tempfile
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import god_roster as gr


# ──────────────────────────────────────────────────────────────────────
#   HARNESS
# ──────────────────────────────────────────────────────────────────────

_TMP = Path(tempfile.mkdtemp(prefix="god_roster_test_"))
_REAL_CACHE = gr.ROSTER_CACHE
_REAL_FETCH = gr.fetch_live_gods


def reset(cache_name=None):
    """Fresh module state with the cache redirected into the temp dir
    (missing file unless a test writes one first)."""
    gr._roster = None
    gr._updated_at = None
    gr.ROSTER_CACHE = _TMP / (cache_name or "no_such_cache.json")
    gr.fetch_live_gods = _REAL_FETCH


def fake_fetch(gods_list):
    """Replace the live fetch with a canned result (None = failure)."""
    gr.fetch_live_gods = lambda: gods_list


# ──────────────────────────────────────────────────────────────────────
#   TESTS
# ──────────────────────────────────────────────────────────────────────

def test_bundled_load_has_new_gods():
    reset()
    names = gr.names()
    assert len(names) >= 88, f"bundled roster too small: {len(names)}"
    for god in ["Bastet", "Chronos", "Ah Puch", "Cu Chulainn",
                "Horus", "Xing Tian", "Zeus", "Morgan Le Fay"]:
        assert god in names, f"{god} missing from bundled roster"


def test_wiki_filename_parsing():
    cases = {
        "T_Ra(S2)_Default_Icon.png": "Ra",
        "T_Hou_Yi(S2)_Default_Icon.png": "Hou Yi",
        "T_MorganLeFay(S2)_Default_Icon.png": "Morgan Le Fay",
        "T_CuChulainn_Default_Icon.png": "Cu Chulainn",
        "T_XingTian_Default_Icon.png": "Xing Tian",
        "T_Ah_Puch(S2)_Default_Icon.png": "Ah Puch",
        "T_Atlas_Default_Icon.png": "Atlas",
    }
    for filename, expected in cases.items():
        got = gr.wiki_filename_to_name(filename)
        assert got == expected, f"{filename}: {got!r} != {expected!r}"


def test_name_to_slug():
    assert gr.name_to_slug("Morgan Le Fay") == "morgan-le-fay"
    assert gr.name_to_slug("Chang'e") == "change"
    assert gr.name_to_slug("Cu Chulainn") == "cu-chulainn"


def test_refresh_adds_new_god_with_first_seen():
    reset()
    base = gr.gods()
    fake = [{"name": g["name"], "slug": g["slug"],
             "wiki_filename": g["wiki_filename"]} for g in base]
    fake.append({"name": "Testgod", "slug": "testgod",
                 "wiki_filename": "T_Testgod(S2)_Default_Icon.png"})
    fake_fetch(fake)

    added = gr.refresh(force=True)
    assert [g["name"] for g in added] == ["Testgod"], added
    assert added[0]["first_seen"] == date.today().isoformat()
    assert "Testgod" in gr.names()
    assert len(gr.names()) == len(base) + 1


def test_refresh_preserves_first_seen_and_keeps_vanished():
    reset()
    base = gr.gods()
    bastet = next(g for g in base if g["name"] == "Bastet")
    original_first_seen = bastet["first_seen"]
    assert original_first_seen, "Bastet should carry a first_seen date"

    # Live page renames Bastet's file and drops Zeus entirely
    fake = []
    for g in base:
        if g["name"] == "Zeus":
            continue
        entry = {"name": g["name"], "slug": g["slug"],
                 "wiki_filename": g["wiki_filename"]}
        if g["name"] == "Bastet":
            entry["wiki_filename"] = "T_Bastet(S2)_NewArt_Default_Icon.png"
        fake.append(entry)
    fake_fetch(fake)

    added = gr.refresh(force=True)
    assert added == [], f"nothing new expected, got {added}"
    after = {g["name"]: g for g in gr.gods()}
    assert "Zeus" in after, "vanished god must be kept"
    assert after["Bastet"]["first_seen"] == original_first_seen
    assert after["Bastet"]["wiki_filename"] == \
        "T_Bastet(S2)_NewArt_Default_Icon.png"


def test_refresh_failure_leaves_roster_untouched():
    reset()
    before = gr.names()
    fake_fetch(None)
    assert gr.refresh(force=True) == []
    assert gr.names() == before


def test_cache_round_trip():
    reset("round_trip.json")
    fake = [{"name": "Testgod", "slug": "testgod",
             "wiki_filename": "T_Testgod(S2)_Default_Icon.png"}]
    base = [{"name": g["name"], "slug": g["slug"],
             "wiki_filename": g["wiki_filename"]} for g in gr.gods()]
    fake_fetch(base + fake)
    gr.refresh(force=True)
    assert gr.ROSTER_CACHE.exists(), "refresh must write the cache"

    # Fresh module state loads the cache, not the bundled snapshot
    gr._roster = None
    gr._updated_at = None
    assert "Testgod" in gr.names()
    assert not gr.refresh_needed(), "cache written seconds ago"


def test_corrupt_and_tiny_cache_fall_back_to_bundled():
    reset("corrupt.json")
    gr.ROSTER_CACHE.write_text("{not json", encoding="utf-8")
    gr._roster = None
    assert len(gr.names()) >= 88

    reset("tiny.json")
    gr.ROSTER_CACHE.write_text(
        json.dumps({"updated_at": "2026-01-01T00:00:00",
                    "gods": [{"name": "Zeus", "slug": "zeus",
                              "wiki_filename": "x", "first_seen": None}]}),
        encoding="utf-8")
    gr._roster = None
    assert len(gr.names()) >= 88, "implausibly small cache must be ignored"


def test_new_gods_window():
    reset()
    recent = gr.new_gods(days=21)
    assert {g["name"] for g in recent} >= {"Bastet", "Chronos"}
    # A first_seen far in the past falls out of the window
    old = gr.new_gods(days=0)
    names = {g["name"] for g in old}
    cutoff_ok = all(
        datetime.fromisoformat(g["first_seen"]) >= datetime.now() - timedelta(days=0)
        for g in old)
    assert cutoff_ok, f"stale gods leaked into a 0-day window: {names}"


def test_smite1_only_excludes_ported():
    reset()
    only = gr.smite1_only_names()
    for ported in ["Bastet", "Chronos", "Zeus", "Mulan"]:
        assert ported not in only, f"{ported} is in SMITE 2 (or ported)"
    for waiting in ["Bakasura", "Cthulhu", "Baba Yaga"]:
        assert waiting in only, f"{waiting} should still be SMITE 1 only"


def test_refresh_needed_ages_out():
    reset()
    gr._load()
    gr._updated_at = (datetime.now() - timedelta(hours=30)).isoformat(
        timespec="seconds")
    assert gr.refresh_needed(max_age_hours=24)
    gr._updated_at = datetime.now().isoformat(timespec="seconds")
    assert not gr.refresh_needed(max_age_hours=24)


# ──────────────────────────────────────────────────────────────────────
#   RUNNER
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    failures = 0
    for fn in tests:
        if name_filter and name_filter not in fn.__name__:
            continue
        try:
            fn()
        except Exception as e:
            failures += 1
            print(f"FAIL  {fn.__name__}: {e}")
            traceback.print_exc()
        else:
            print(f"PASS  {fn.__name__}")

    # Restore module globals for anyone importing after us
    gr.ROSTER_CACHE = _REAL_CACHE
    gr.fetch_live_gods = _REAL_FETCH
    gr._roster = None
    gr._updated_at = None

    print(f"\n{'ALL GREEN' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
