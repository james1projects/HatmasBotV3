"""
test_aspects.py — regression tests for the god Aspect feature
=============================================================
Aspects are alternate god kits viewers can request ("!nominate Khepri
aspect"). These tests lock down the pieces that decide whether an
aspect request is understood and accepted:

  - core/aspect_roster.py: bundled fallback, cache round-trip,
    refresh merge semantics (never shrinks, implausible fetches
    ignored), has_aspect slug matching
  - core/god_resolver.py: split_aspect keyword extraction and the
    courtesy-filler retry ("khepri please!" resolves like "khepri")
  - display_god rendering

No network — the wiki fetch is stubbed. Run:
    python tests/test_aspects.py            # whole suite
    python tests/test_aspects.py refresh    # name filter

Exit 0 if every test passes, 1 otherwise. Same conventions as
tests/test_god_roster.py.
"""

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import aspect_roster
from core.aspect_roster import display_god
from core.god_resolver import resolve_sync, split_aspect


def _reset(cache_path: Path):
    """Point the module at a scratch cache file and drop in-memory
    state so each test starts cold."""
    aspect_roster.ASPECT_CACHE = cache_path
    aspect_roster._aspects = None
    aspect_roster._updated_at = None


# ------------------------------------------------------------------
# aspect_roster
# ------------------------------------------------------------------

def test_bundled_fallback_loads(tmp: Path):
    _reset(tmp / "nope.json")
    slugs = aspect_roster.aspect_slugs()
    assert len(slugs) >= aspect_roster.MIN_PLAUSIBLE_ASPECTS, len(slugs)
    assert "khepri" in slugs
    assert "ymir" not in slugs
    assert aspect_roster.updated_at() == aspect_roster.BUNDLED_SNAPSHOT_DATE


def test_has_aspect_matches_names(tmp: Path):
    _reset(tmp / "nope.json")
    assert aspect_roster.has_aspect("Khepri") is True
    assert aspect_roster.has_aspect("khepri") is True
    assert aspect_roster.has_aspect("The Morrigan") is True
    assert aspect_roster.has_aspect("Morgan Le Fay") is True
    assert aspect_roster.has_aspect("Ymir") is False
    assert aspect_roster.has_aspect("") is False
    assert aspect_roster.has_aspect(None) is False


def test_refresh_adds_and_never_shrinks(tmp: Path):
    _reset(tmp / "cache.json")
    baseline = set(aspect_roster.BUNDLED_ASPECT_SLUGS)

    # Live fetch returns baseline plus one new god, minus one god that
    # "vanished" from the wiki: the new god is added, the vanished god
    # is kept.
    fake_live = (baseline - {"khepri"}) | {"ymir"}
    aspect_roster.fetch_live_aspects = lambda: set(fake_live)
    added = aspect_roster.refresh(force=True)
    assert added == ["ymir"], added
    slugs = aspect_roster.aspect_slugs()
    assert "ymir" in slugs and "khepri" in slugs

    # Cache round-trip: cold reload sees the merged set.
    aspect_roster._aspects = None
    aspect_roster._updated_at = None
    assert "ymir" in aspect_roster.aspect_slugs()


def test_refresh_ignores_implausible_fetch(tmp: Path):
    _reset(tmp / "cache2.json")
    before = aspect_roster.aspect_slugs()
    aspect_roster.fetch_live_aspects = lambda: {"khepri", "ra"}
    added = aspect_roster.refresh(force=True)
    assert added == []
    assert aspect_roster.aspect_slugs() == before


def test_refresh_ignores_fetch_failure(tmp: Path):
    _reset(tmp / "cache3.json")
    before = aspect_roster.aspect_slugs()
    aspect_roster.fetch_live_aspects = lambda: None
    added = aspect_roster.refresh(force=True)
    assert added == []
    assert aspect_roster.aspect_slugs() == before


# ------------------------------------------------------------------
# split_aspect + resolver filler retry
# ------------------------------------------------------------------

GODS = ["Khepri", "Kukulkan", "Guan Yu", "Ymir", "The Morrigan"]


def test_split_aspect_extraction(tmp: Path):
    assert split_aspect("Khepri aspect please!")[1] is True
    assert split_aspect("aspect khepri")[1] is True
    assert split_aspect("ASPECTS of ra")[1] is True
    assert split_aspect("Khepri")[1] is False
    # No god contains the word — but substrings must not trigger.
    assert split_aspect("aspectral")[1] is False
    cleaned, _ = split_aspect("Khepri aspect")
    assert "aspect" not in cleaned.lower()
    assert split_aspect("")[0] == ""
    assert split_aspect(None)[1] is False


def test_resolution_after_split(tmp: Path):
    for raw, want in [
        ("Khepri aspect please!", "Khepri"),
        ("kuku aspect", "Kukulkan"),
        ("guan yu ASPECT pls", "Guan Yu"),
        ("aspect", None),
    ]:
        cleaned, _ = split_aspect(raw)
        hit = resolve_sync(cleaned, GODS)
        got = hit[0] if hit else None
        assert got == want, (raw, got, want)


def test_filler_retry(tmp: Path):
    hit = resolve_sync("khepri please!", GODS)
    assert hit and hit[0] == "Khepri", hit
    hit = resolve_sync("ymir thank you", GODS)
    assert hit and hit[0] == "Ymir", hit
    # Filler alone resolves to nothing.
    assert resolve_sync("please", GODS) is None
    # A direct match is never overridden by filler stripping.
    assert resolve_sync("Khepri", GODS)[1] == "exact"


def test_display_god(tmp: Path):
    assert display_god("Khepri", True) == "Khepri (Aspect)"
    assert display_god("Khepri", 1) == "Khepri (Aspect)"
    assert display_god("Khepri", False) == "Khepri"
    assert display_god("Khepri", None) == "Khepri"


# ------------------------------------------------------------------
# runner
# ------------------------------------------------------------------

def main():
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)
             and name_filter in n]
    passed = failed = 0
    real_fetch = aspect_roster.fetch_live_aspects
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
                print(f"PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {name}: {e}")
                failed += 1
            finally:
                aspect_roster.fetch_live_aspects = real_fetch
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
