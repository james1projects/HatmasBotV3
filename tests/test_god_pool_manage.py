"""
test_god_pool_manage.py — regression tests for the control panel's
spin-pool manager (GodPoolPlugin.list_pool / set_votes)
==================================================================
set_votes is Hatmaster's override for pruning test nominations
without waiting for the daily-cap reset, so it must stay predictable:

  - absolute set (not increment), resolver-backed god names
  - votes 0 deletes exactly the (god, aspect) entry, idempotently
  - upsert creates missing entries as added_by='control_panel'
  - unknown_god / no_aspect rejected before any write
  - clamping: negative = remove, cap at 999
  - god_pool_votes (daily-cap records) never touched

No network. Run:
    python tests/test_god_pool_manage.py

Exit 0 if every test passes, 1 otherwise. Same conventions as
tests/test_aspects.py.
"""

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import aiosqlite
except ImportError:
    print("Missing aiosqlite. Install with: pip install aiosqlite")
    sys.exit(1)

from plugins.god_pool import GodPoolPlugin

KNOWN = ["Khepri", "Ymir", "Chang'e", "Osiris"]


async def make_plugin():
    plugin = GodPoolPlugin()
    plugin._known_gods = list(KNOWN)
    db = await aiosqlite.connect(":memory:")
    await plugin._init_schema(db)
    return plugin, db


async def test_absolute_set_and_resolution():
    plugin, db = await make_plugin()
    try:
        await plugin.do_nominate("v1", "Osiris")
        r = await plugin.set_votes("osiris", False, 4)
        assert r == {"ok": True, "god": "Osiris", "use_aspect": False,
                     "votes": 4, "removed": False}, r
        r = await plugin.set_votes("osiris", False, 3)
        assert r["votes"] == 3, r
        rows = await plugin.list_pool()
        assert rows[0]["votes"] == 3 and rows[0]["added_by"] == "v1", rows
    finally:
        await db.close()


async def test_upsert_creates_control_panel_entry():
    plugin, db = await make_plugin()
    try:
        r = await plugin.set_votes("khepri", True, 2)
        assert r["ok"] and r["use_aspect"] and r["votes"] == 2, r
        rows = await plugin.list_pool()
        assert rows == [{"god": "Khepri", "use_aspect": True,
                         "added_by": "control_panel", "votes": 2,
                         "added_at": rows[0]["added_at"]}], rows
    finally:
        await db.close()


async def test_zero_removes_exact_entry():
    plugin, db = await make_plugin()
    try:
        await plugin.set_votes("Khepri", False, 3)
        await plugin.set_votes("Khepri", True, 2)
        r = await plugin.set_votes("Khepri", True, 0)
        assert r["ok"] and r["removed"] and r["votes"] == 0, r
        # Idempotent re-remove.
        r = await plugin.set_votes("Khepri", True, 0)
        assert r["ok"] and not r["removed"], r
        # Base entry untouched.
        rows = await plugin.list_pool()
        assert [(e["god"], e["use_aspect"]) for e in rows] \
            == [("Khepri", False)], rows
    finally:
        await db.close()


async def test_validation_rejections():
    plugin, db = await make_plugin()
    try:
        r = await plugin.set_votes("notagod", False, 1)
        assert r == {"ok": False, "reason": "unknown_god"}, r
        r = await plugin.set_votes("Ymir", True, 1)
        assert not r["ok"] and r["reason"] == "no_aspect", r
        assert await plugin.list_pool() == []
    finally:
        await db.close()


async def test_clamping():
    plugin, db = await make_plugin()
    try:
        r = await plugin.set_votes("Osiris", False, 5000)
        assert r["votes"] == 999, r
        r = await plugin.set_votes("Osiris", False, -5)
        assert r["votes"] == 0 and r["removed"], r
    finally:
        await db.close()


async def test_daily_cap_records_untouched():
    plugin, db = await make_plugin()
    try:
        await plugin.do_nominate("v1", "Osiris")
        r = await plugin.set_votes("Osiris", False, 0)
        assert r["removed"], r
        # The viewer's daily-cap row must survive the prune (same
        # semantics as !poolclear): no free re-vote.
        r2 = await plugin.do_nominate("v1", "Ymir")
        assert not r2["ok"] and r2["reason"] == "already_voted", r2
    finally:
        await db.close()


def main():
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)
             and name_filter in n]
    passed = failed = 0
    for name, fn in tests:
        try:
            asyncio.run(fn())
            print(f"PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {name}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
