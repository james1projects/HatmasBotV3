import sys
import os
import tempfile
import json
import time
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plugins.findit.items_store import ItemsStore

PASS = 0
FAIL = 0

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        print(f"[PASS] {name}")
        PASS += 1
    else:
        print(f"[FAIL] {name}")
        if detail:
            print(f"       Detail: {detail}")
        FAIL += 1

def main():
    global PASS, FAIL
    PASS = 0
    FAIL = 0

    test_dir = Path(tempfile.mkdtemp())
    try:
        # Test 1: Fresh store on missing file
        items_path = test_dir / "items.json"
        store = ItemsStore(items_path)
        check("fresh_store_empty", len(store.visible_items("dev-a")) == 0)
        alice = store.register_profile("dev-a", "Alice")
        bob = store.register_profile("dev-b", "Bob")
        check("register_profiles", alice["name"] == "Alice" and bob["name"] == "Bob")

        # Test 2: v1 migration
        v1_data = {
            "my keys": {"name": "My Keys", "base_class": "keys", "embeds": [[0.1, 0.2]]},
            "another item": {"name": "Another Item", "base_class": "item", "embeds": []}
        }
        with open(items_path, 'w') as f:
            json.dump(v1_data, f)
        store = ItemsStore(items_path)
        items = store.visible_items("dev-a")
        check("v1_migration_items_count", len(items) == 2)
        shared_items = [i for i in items.values() if i["owner"] == "shared"]
        check("v1_migration_shared_owner", len(shared_items) == 2)
        bak_path = items_path.with_suffix('.v1.bak')
        check("v1_backup_exists", bak_path.exists())
        # Reload to ensure no duplication
        store2 = ItemsStore(items_path)
        items2 = store2.visible_items("dev-a")
        check("v1_no_duplication", len(items2) == 2)

        # Test 3: Create/collision
        mug = store.create_item("dev-a", "My Mug", "mug")
        check("create_item_success", mug["name"] == "My Mug")
        try:
            store.create_item("dev-a", "my mug", "mug")
            check("create_collision_same_profile", False)
        except ValueError:
            check("create_collision_same_profile", True)
        try:
            store.create_item("dev-b", "my mug", "mug")
            check("create_collision_different_profile", True)
        except ValueError:
            check("create_collision_different_profile", False)
        store.set_shared(mug["id"], True, "dev-a")
        try:
            store.create_item("dev-c", "MY MUG", "mug")
            check("create_collision_shared", False)
        except ValueError:
            check("create_collision_shared", True)

        # Test 4: Add view with fake jpeg
        view_count = store.add_view(mug["id"], [0.3, 0.4], b"\xff\xd8fakejpeg")
        check("add_view_increments_count", view_count == 1)
        thumb_path = test_dir / "thumbs" / mug["id"] / "0.jpg"
        check("add_view_creates_thumb_file", thumb_path.exists())
        summary = store.item_summary(mug["id"], with_thumb=True)
        check("item_summary_has_thumb_data", summary["thumb"] is not None and summary["thumb"].startswith("data:image/jpeg;base64,"))

        # Test 5: Rename
        renamed = store.rename(mug["id"], "New Mug", "dev-a")
        check("rename_success", renamed["name"] == "New Mug")
        try:
            store.rename(mug["id"], "Another Item", "dev-a")
            check("rename_collision", False)
        except ValueError:
            check("rename_collision", True)
        renamed2 = store.rename(mug["id"], "NEW MUG", "dev-a")
        check("rename_same_name_different_case", renamed2["name"] == "NEW MUG")

        # Test 6: Locations
        store.log_location(mug["id"], "Kitchen Drawer")
        time.sleep(0.01)
        store.log_location(mug["id"], "kitchen drawer")
        time.sleep(0.01)
        store.log_location(mug["id"], "Desk")
        summary = store.location_summary(mug["id"])
        check("location_summary_groups", len(summary) == 2)
        check("location_summary_order", summary[0]["place"].lower() == "kitchen drawer")
        check("location_summary_counts", summary[0]["count"] == 2)
        long_place = "x" * 100
        for _ in range(205):
            store.log_location(mug["id"], long_place)
        raw_locs = store._data["items"][mug["id"]]["locations"]
        check("location_history_capped", len(raw_locs) == 200)

        # Test 7: Delete
        deleted = store.delete(mug["id"])
        check("delete_removes_item", deleted is not None)
        item_dir = test_dir / "thumbs" / mug["id"]
        check("delete_removes_thumbs_dir", not item_dir.exists())

        # Test 8: Persistence
        store2 = ItemsStore(items_path)
        items = store2.visible_items("dev-a")
        check("persistence_sees_all", len(items) == 2)
        tmp_files = list(test_dir.glob("*.tmp"))
        check("no_tmp_files_left", len(tmp_files) == 0)

        # Test 9: Unicode name
        unicode_item = store2.create_item("dev-a", "café keys ☕", "keys")
        check("unicode_name_created", unicode_item["name"] == "café keys ☕")
        store3 = ItemsStore(items_path)
        items = store3.visible_items("dev-a")
        found = None
        for item in items.values():
            if item["id"] == unicode_item["id"]:
                found = item
                break
        check("unicode_name_roundtrip", found is not None and found["name"] == "café keys ☕")

    finally:
        shutil.rmtree(test_dir)

    print(f"\nTotal: {PASS} passed, {FAIL} failed")
    return 1 if FAIL > 0 else 0

if __name__ == "__main__":
    sys.exit(main())
