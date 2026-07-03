import json
import base64
import threading
import time
import uuid
from pathlib import Path
import shutil

class ItemsStore:
    """
    Persistence layer for personal-object recognition items.
    
    Manages v2 JSON storage, handles backward-compatible v1 migration,
    ensures thread-safe mutations via a single lock, and coordinates
    on-disk thumbnail storage alongside the metadata file.
    Designed to be GPU-free and easily unit-testable.
    """

    def __init__(self, path):
        self._path = Path(path)
        self._thumbs_dir = self._path.parent / "thumbs"
        self._lock = threading.Lock()
        
        # Ensure the thumbnail directory exists upfront to avoid race conditions later
        self._thumbs_dir.mkdir(parents=True, exist_ok=True)

        if self._path.exists():
            try:
                with open(self._path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, OSError):
                # A corrupt file is the one case where losing the original
                # would hurt the most — preserve it before starting fresh.
                corrupt_bak = self._path.with_suffix('.corrupt.bak')
                if not corrupt_bak.exists():
                    try:
                        shutil.copy2(self._path, corrupt_bak)
                    except OSError:
                        pass
                raw = {}
                
            # Detect v1 format by absence of "version" key
            if not isinstance(raw, dict) or "version" not in raw:
                self._migrate_v1_to_v2(raw)
            elif isinstance(raw.get("profiles"), dict) and isinstance(raw.get("items"), dict):
                self._data = raw
            else:
                # Parseable JSON but not a well-formed v2 store (truncated,
                # hand-edited, missing keys). Dereferencing profiles/items
                # would KeyError in the constructor and brick the worker's
                # startup — back up and start fresh instead.
                corrupt_bak = self._path.with_suffix('.corrupt.bak')
                if not corrupt_bak.exists():
                    try:
                        shutil.copy2(self._path, corrupt_bak)
                    except OSError:
                        pass
                print(f"[worker] items.json was malformed — backed up to "
                      f"{corrupt_bak.name} and started a fresh store", flush=True)
                self._data = {"version": 2, "profiles": {}, "items": {}}
        else:
            self._data = {"version": 2, "profiles": {}, "items": {}}
            
        # Guarantee the reserved 'shared' profile exists for system-wide items
        if "shared" not in self._data["profiles"]:
            self._data["profiles"]["shared"] = {"name": "Shared", "created": 0}

        for item in self._data["items"].values():
            # Items enrolled before the embedder became configurable were CLIP
            item.setdefault("embed_model", "clip")
            # `creator` is the immutable device that made the item and the ONLY
            # one allowed to rename/delete/un-share it; `owner` is just the
            # visibility key ("shared" or a device id). Legacy items predate
            # the split — fall back to owner, so a legacy shared item has
            # creator "shared" (no real device) and stays locked to mutation.
            item.setdefault("creator", item.get("owner", "shared"))

    def _migrate_v1_to_v2(self, raw):
        """
        Convert legacy v1 flat structure to v2.
        Backs up the original file unless it's empty, preserving user data safely.
        """
        now = time.time()
        new_data = {
            "version": 2, 
            "profiles": {"shared": {"name": "Shared", "created": 0}}, 
            "items": {}
        }
        
        if isinstance(raw, dict) and raw:
            # Only backup if data exists and backup hasn't been created yet
            bak_path = self._path.with_suffix('.v1.bak')
            if not bak_path.exists():
                shutil.copy2(self._path, bak_path)
                
            for lower_name, info in raw.items():
                item_id = uuid.uuid4().hex[:12]
                new_data["items"][item_id] = {
                    "id": item_id,
                    "name": info.get("name", lower_name),
                    "owner": "shared",
                    "base_class": info.get("base_class", ""),
                    "embeds": info.get("embeds", []),
                    "thumbs": [],
                    "locations": [],
                    "created": now,
                    "updated": now
                }
        self._data = new_data
        self._save()

    def _save(self):
        """
        Atomically persist state to disk.
        Writes to a temp file first, then replaces the target to prevent 
        corruption if the process crashes mid-write.
        """
        tmp_path = self._path.with_suffix('.tmp')
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, indent=2)
            tmp_path.replace(self._path)
        except OSError:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def _get_visible_unlocked(self, profile_id):
        """Internal helper to filter items without acquiring the lock."""
        return {
            iid: item for iid, item in self._data["items"].items()
            if item["owner"] == profile_id or item["owner"] == "shared"
        }

    def register_profile(self, profile_id, name):
        """Create or update a profile's display name. 'shared' name is immutable."""
        with self._lock:
            if profile_id == "shared":
                # Prevent accidental overwrites of the system profile
                return dict(self._data["profiles"].setdefault("shared", {"name": "Shared", "created": 0}))
            
            # Display names come straight from phone input — trim and cap
            name = (name or "").strip()[:40] or "Someone"
            now = time.time()
            if profile_id not in self._data["profiles"]:
                self._data["profiles"][profile_id] = {"name": name, "created": now}
            else:
                self._data["profiles"][profile_id]["name"] = name
                
            self._save()
            return dict(self._data["profiles"][profile_id])

    def visible_items(self, profile_id):
        """Return items owned by the profile or shared."""
        with self._lock:
            # Return a shallow copy to prevent external mutation of internal state
            return dict(self._get_visible_unlocked(profile_id))

    def find_by_name(self, profile_id, name):
        """Case-insensitive lookup within visible items."""
        target = name.lower()
        with self._lock:
            for item in self._get_visible_unlocked(profile_id).values():
                if item["name"].lower() == target:
                    return dict(item)
        return None

    def create_item(self, profile_id, name, base_class, embed_model="clip"):
        """Create a new item. Raises ValueError if name conflicts case-insensitively."""
        with self._lock:
            trimmed_name = name.strip()
            if not trimmed_name:
                raise ValueError("name cannot be empty")
            # Check conflict directly to avoid nested lock acquisition
            for it in self._get_visible_unlocked(profile_id).values():
                if it["name"].lower() == trimmed_name.lower():
                    raise ValueError("name already used")
                
            now = time.time()
            item_id = uuid.uuid4().hex[:12]
            item = {
                "id": item_id,
                "name": trimmed_name,
                "owner": profile_id,
                "creator": profile_id,   # immutable; gates mutation (see load)
                "base_class": base_class,
                "embed_model": embed_model,
                "embeds": [],
                "thumbs": [],
                "locations": [],
                "created": now,
                "updated": now
            }
            self._data["items"][item_id] = item
            self._save()
            return dict(item)

    # Views are matched one-by-one every frame and stored in items.json, so
    # they can't grow without bound — an enrollment past this keeps only the
    # newest MAX_VIEWS (more than enough angles for recognition, and it caps
    # both disk use and per-frame matching cost on a public endpoint).
    MAX_VIEWS = 12

    def add_view(self, item_id, embed, thumb_jpeg=None):
        """Append an embedding and optionally a thumbnail. Returns new embed count."""
        with self._lock:
            if item_id not in self._data["items"]:
                raise KeyError(item_id)

            item = self._data["items"][item_id]
            item["embeds"].append(embed)

            if thumb_jpeg is not None:
                # Isolate thumbnails per item to prevent directory collisions.
                # Use a monotonic index (never reused) so a capped-off view's
                # filename can't collide with a survivor's.
                item_dir = self._thumbs_dir / item_id
                item_dir.mkdir(parents=True, exist_ok=True)
                n = item.get("thumb_seq", len(item["thumbs"]))
                item["thumb_seq"] = n + 1
                rel_path = f"{item_id}/{n}.jpg"
                with open(self._thumbs_dir / rel_path, 'wb') as f:
                    f.write(thumb_jpeg)
                item["thumbs"].append(rel_path)

            # Enforce the cap: drop the oldest views (and their thumb files)
            while len(item["embeds"]) > self.MAX_VIEWS:
                item["embeds"].pop(0)
            while len(item["thumbs"]) > self.MAX_VIEWS:
                old = item["thumbs"].pop(0)
                try:
                    (self._thumbs_dir / old).unlink(missing_ok=True)
                except OSError:
                    pass

            item["updated"] = time.time()
            self._save()
            return len(item["embeds"])

    def rename(self, item_id, new_name, profile_id):
        """Rename an item. Raises ValueError on case-insensitive conflict."""
        with self._lock:
            if item_id not in self._data["items"]:
                raise KeyError(item_id)
                
            trimmed = new_name.strip()
            # Verify uniqueness among visible items, excluding the target itself
            for iid, it in self._get_visible_unlocked(profile_id).items():
                if iid != item_id and it["name"].lower() == trimmed.lower():
                    raise ValueError("name already used")
                    
            self._data["items"][item_id]["name"] = trimmed
            self._data["items"][item_id]["updated"] = time.time()
            self._save()
            return dict(self._data["items"][item_id])

    def set_shared(self, item_id, shared, profile_id):
        """Toggle sharing status. True -> 'shared', False -> profile_id."""
        with self._lock:
            if item_id not in self._data["items"]:
                raise KeyError(item_id)
                
            self._data["items"][item_id]["owner"] = "shared" if shared else profile_id
            self._data["items"][item_id]["updated"] = time.time()
            self._save()
            return dict(self._data["items"][item_id])

    def delete(self, item_id):
        """Remove item and its thumbnail directory."""
        with self._lock:
            if item_id not in self._data["items"]:
                return None
                
            removed = dict(self._data["items"].pop(item_id))
            # Clean up associated disk artifacts immediately
            item_dir = self._thumbs_dir / item_id
            if item_dir.exists():
                shutil.rmtree(item_dir, ignore_errors=True)
                
            self._save()
        return removed

    def log_location(self, item_id, place):
        """Record a location sighting. Caps history at 200 entries."""
        with self._lock:
            if item_id not in self._data["items"]:
                raise KeyError(item_id)
                
            trimmed = place.strip()[:80]
            loc_entry = {"place": trimmed, "ts": time.time()}
            
            # Maintain FIFO cap to prevent unbounded growth of the JSON file
            locations = self._data["items"][item_id]["locations"]
            locations.append(loc_entry)
            if len(locations) > 200:
                self._data["items"][item_id]["locations"] = locations[-200:]
                
            self._data["items"][item_id]["updated"] = time.time()
            self._save()
            
        # Compute summary outside the lock to prevent deadlock with standard Lock
        return self.location_summary(item_id)

    def location_summary(self, item_id):
        """Aggregate locations by place (case-insensitive), sorted by frequency then recency."""
        if item_id not in self._data["items"]:
            raise KeyError(item_id)
            
        # Brief lock ensures we read a consistent snapshot during concurrent writes
        with self._lock:
            locs = list(self._data["items"][item_id]["locations"])
            
        groups = {}
        for loc in locs:
            key = loc["place"].lower()
            if key not in groups:
                groups[key] = {"place": loc["place"], "count": 0, "last_ts": loc["ts"]}
            g = groups[key]
            g["count"] += 1
            # Track the most recent timestamp and preserve its casing
            if loc["ts"] > g["last_ts"]:
                g["last_ts"] = loc["ts"]
                g["place"] = loc["place"]
                
        result = list(groups.values())
        result.sort(key=lambda x: (-x["count"], -x["last_ts"]))
        return result

    def item_summary(self, item_id, with_thumb=False, viewer=None):
        """Generate a JSON-safe summary of an item. `viewer` (a profile id)
        sets `mine` — whether the viewer created it and may mutate it."""
        if item_id not in self._data["items"]:
            raise KeyError(item_id)

        with self._lock:
            item = dict(self._data["items"][item_id])
            creator = item.get("creator", item.get("owner"))
            owner_name = self._data["profiles"].get(creator, {}).get("name", "?")

        summary = {
            "id": item["id"],
            "name": item["name"],
            "base_class": item["base_class"],
            "views": len(item["embeds"]),
            "shared": item["owner"] == "shared",
            "mine": viewer is not None and creator == viewer,
            "owner_name": owner_name,
            "locations": self.location_summary(item_id),
            "thumb": None
        }
        
        if with_thumb and item["thumbs"]:
            # Load first thumbnail for quick preview
            thumb_path = self._thumbs_dir / item["thumbs"][0]
            try:
                with open(thumb_path, 'rb') as f:
                    b64 = base64.b64encode(f.read()).decode('ascii')
                summary["thumb"] = f"data:image/jpeg;base64,{b64}"
            except (OSError, IOError):
                # Gracefully handle missing or corrupted thumb files
                pass
                
        return summary

    def summaries_for(self, profile_id, with_thumb=False):
        """Return summaries for all visible items, sorted by updated timestamp descending."""
        visible = self.visible_items(profile_id)
        summaries = []
        for iid in visible:
            try:
                summaries.append(
                    self.item_summary(iid, with_thumb, viewer=profile_id))
            except KeyError:
                # Item might have been deleted concurrently; skip safely
                continue
                
        # Sort by most recently updated first
        summaries.sort(key=lambda x: -visible[x["id"]]["updated"])
        return summaries
