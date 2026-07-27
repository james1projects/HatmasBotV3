"""
test_clean_god_name.py — regression tests for tracker.gg god-name
canonicalization (plugins/smite/history.py:_clean_god_name)
=================================================================
Tracker.gg leaks Hi-Rez-internal god names in two raw forms: the
Unreal class path ("Gods.Atlas") and the concatenated display name
("XingTian"). The July 2026 XingTian leak mid-game "corrected" the
OBS portrait to a name with no image file (clearing the portrait) and
opened a duplicate god_prices stock — so canonicalization must stay
exact and total:

  - "Gods." prefix stripped, then roster-resolved (exact tier only)
  - concatenated multi-word names map to their canonical form
  - canonical names are idempotent
  - unknown names (not on the roster) pass through unchanged
  - _same_god_name treats formatting variants as the same god,
    so the portrait-vs-tracker comparison never false-fires

No network — the roster loads from cache/bundled data. Run:
    python tests/test_clean_god_name.py

Exit 0 if every test passes, 1 otherwise. Same conventions as
tests/test_aspects.py.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.smite.history import _clean_god_name
from plugins.smite.match_state import _MatchStateMixin


def test_prefix_strip():
    assert _clean_god_name("Gods.Atlas") == "Atlas"
    assert _clean_god_name("Gods.XingTian") == "Xing Tian"
    assert _clean_god_name("Gods.") is None


def test_concatenated_names_canonicalize():
    # The July 2026 leak plus every multi-word god on the roster in
    # concatenated form — all must resolve to their spaced names.
    assert _clean_god_name("XingTian") == "Xing Tian"
    assert _clean_god_name("MorganLeFay") == "Morgan Le Fay"
    assert _clean_god_name("AhPuch") == "Ah Puch"
    assert _clean_god_name("GuanYu") == "Guan Yu"
    assert _clean_god_name("BaronSamedi") == "Baron Samedi"
    assert _clean_god_name("TheMorrigan") == "The Morrigan"
    assert _clean_god_name("NuWa") == "Nu Wa"
    assert _clean_god_name("HunBatz") == "Hun Batz"


def test_every_roster_god_concatenated():
    """Exhaustive: every current roster god survives the squash
    round-trip, so no future multi-word god needs a special case."""
    from core import god_roster
    for name in god_roster.names():
        squashed = name.replace(" ", "")
        got = _clean_god_name(squashed)
        assert got == name, (squashed, got, name)


def test_canonical_names_idempotent():
    assert _clean_god_name("Xing Tian") == "Xing Tian"
    assert _clean_god_name("Apollo") == "Apollo"
    assert _clean_god_name("Ymir") == "Ymir"


def test_unknown_passthrough():
    # Gods the roster doesn't know (not in SMITE 2, or released
    # before a refresh) pass through unchanged — never mis-mapped.
    assert _clean_god_name("ChangE") == "ChangE"
    assert _clean_god_name("SomeBrandNewGod") == "SomeBrandNewGod"
    assert _clean_god_name(None) is None
    assert _clean_god_name("") == ""


def test_same_god_name():
    sg = _MatchStateMixin._same_god_name
    assert sg("Xing Tian", "XingTian")
    assert sg("Xing Tian", "xing-tian")
    assert sg("Chang'e", "ChangE")
    assert sg("Ymir", "Ymir")
    assert not sg("Xing Tian", "Ymir")
    assert not sg("", "Ymir")
    assert not sg(None, "Ymir")


def main():
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)
             and name_filter in n]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {name}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
