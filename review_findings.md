# Adversarial review — FindIt v2 branch (worktree-overnight-2026-07-03)

Scope: 7 commits, `git diff main...HEAD`. Files read in full: items_store.py,
worker.py, plugin.py, public/findit.html, core/public_webserver.py FindIt
section, core/config.py diff, tools/* (bench/devserver/tests).

Legend: CRITICAL = data loss / cross-user compromise / hard outage;
MAJOR = exploitable or crash under realistic conditions; MINOR = correctness
wart / cosmetic / defense-in-depth.

---

## 1. Persistence & migration

### F1 (MAJOR) — Truncated/partial v2 items.json bricks the whole feature
`plugins/findit/items_store.py:43-56`. When the file parses as JSON and has a
`"version"` key, the code does `self._data = raw` with **no schema check**, then
immediately dereferences `self._data["profiles"]` (line 51) and
`self._data["items"]` (line 55). A v2 file truncated to e.g. `{"version": 2}`
(power loss between `json.dump` writing `{` and the rest — though the atomic
tmp+replace makes this specific case unlikely, a hand-edited or externally
corrupted-but-still-valid-JSON file triggers it) raises `KeyError` in the
`ItemsStore` constructor. That runs inside the worker's FastAPI `startup`
event (`worker.py:356-357`), so the worker never sets `_ready`, `/healthz`
stays 503 forever, `_wait_ready` times out after `FINDIT_STARTUP_TIMEOUT`
(240 s), and every phone gets 503. FindIt is hard-down until someone manually
fixes the file. Verified the KeyError reproduces.
Note the corrupt-file *branch* (line 31-40) only fires on `JSONDecodeError`/
`OSError`; a structurally-invalid-but-parseable file skips it entirely.
Fix: validate after load — `if raw.get("version") == 2 and isinstance(raw.get("profiles"), dict) and isinstance(raw.get("items"), dict): self._data = raw` else treat as corrupt (back up + fresh). Or `self._data.setdefault("profiles", {})` / `setdefault("items", {})` defensively.

### F2 (MINOR) — Corrupt file recovered but silently reset to empty
`items_store.py:31-48`. On `JSONDecodeError`, raw = {} → falls into
`_migrate_v1_to_v2({})` → because raw is empty, no `.v1.bak` is written and
`_save()` overwrites items.json with an empty store. Data *is* preserved in
`items.corrupt.bak` (good), but only if `.corrupt.bak` doesn't already exist
(line 35). A second corruption event overwrites nothing new but also can't
re-back-up. Acceptable, but there is no log line telling the operator the
store was reset — they'll discover it only when the gallery is empty. Suggest a
`print(..., flush=True)` on the corrupt path.

### F3 (MINOR) — v1 list / non-dict JSON silently wiped with no backup
`items_store.py:43,70`. If items.json is valid JSON but a list (or any
non-dict), `_migrate_v1_to_v2` sees `isinstance(raw, dict)` false, skips the
backup, and `_save()`s an empty store. No `.v1.bak`, no `.corrupt.bak`. Only
reachable if the file was hand-written oddly; low likelihood but zero-backup
data loss. Fix: back up unconditionally before overwriting anything.

### F4 (MINOR) — Migration flips every legacy item to `owner="shared"`
`items_store.py:82`. All v1 items become household-visible after migration.
v1 had no profiles so there's no "correct" owner, but the effect is that a
user's previously-personal items are now visible to and mutable by every other
device (see F6/F7). Worth a doc note; arguably by design.

### F5 (MINOR) — Unbounded embeds/thumbs per item (no cap)
`items_store.py:177-201`. `add_view` appends to `embeds` and writes a thumb
JPEG with **no limit**. `locations` is capped at 200 (line 258) but views are
not. A client can call enroll thousands of times on one item: items.json grows
without bound, thumbs/ fills the disk, and `_rank_items` (worker.py:196-212)
iterates every embed of every item on **every frame**, so detection latency
degrades linearly — a cheap DoS on a public, unauthenticated endpoint. Fix:
cap views (e.g. keep newest N, N~12) in `add_view`.

---

## 2. Profile isolation

### F6 (MAJOR) — "Make private" on a shared item you don't own **steals** it
`items_store.py:220-229` + `worker.py:419-430`. `set_shared` is reached after
only a *visibility* check (`visible_items(pid).get(item_id)` — shared items are
visible to everyone). When `shared=False` it sets `owner = profile_id` = the
**caller's** id, with no check that the caller was the original owner. So any
household member (or any anonymous client that saw the item's id in a
`list_items`/`hello_ok` reply — shared item ids are broadcast to all) can send
`{"type":"set_shared","item_id":"<shared id>","shared":false}` and the item
becomes *their* private item, vanishing from the real owner's gallery. The UI's
"Make private" button does exactly this to any shared tile. Ownership/data
transfer across profiles. Fix: record and check an `owner`/`creator` field; only
the creator may un-share, or un-share should restore the original owner, not the
caller.

### F7 (MAJOR / partly by-design) — Any device can rename or forget any *shared* item
`worker.py:408-438`, store `rename`/`delete`/`log_location`. Same root cause:
authorization == visibility, and shared items are visible to all. Device A can
`forget` (permanently delete item + thumbs) or `rename` device B's shared item.
For a "household" feature deleting shared items may be intended, but permanent
cross-user deletion with no confirmation server-side and no ownership check is
worth an explicit decision. Private items ARE protected (a guessed item_id for
another profile's private item resolves to None → "unknown item"; verified).

### F8 (MINOR) — A client can declare itself the `"shared"` profile
`worker.py:391-402`. `hello` with `profile:"shared"` passes the alnum
sanitizer unchanged, so `pid` stays `"shared"`, the `register_profile` guard is
skipped, and the connection now operates *as* the shared profile: everything it
enrolls is owned by "shared" (household-visible) and it can mutate all shared
items. This is also the default pre-hello state, so it's not a new privilege,
but it lets a hostile client spam the shared gallery. Consider reserving
`"shared"` (and treating a client-supplied `"shared"` as the anonymous default
rather than an addressable identity).

### F9 (MINOR) — Profile uuid is an unauthenticated bearer credential
`findit.html:192`. Identity = a localStorage `crypto.randomUUID()`. It's 122-bit
random (not guessable), but there is no secret beyond it: anyone who learns a
uuid (shared device, screen capture, localStorage inspection) can impersonate
that profile via `hello` and read/enroll/delete its private items. Also two
people using the *same* browser profile share one uuid, so "your saved items are
yours" is per-browser, not per-person — the name prompt doesn't create a new
identity. Document the trust model; if stronger isolation is wanted, the uuid
should be treated as a capability and never displayed/logged.

---

## 3. Protocol robustness

### F10 (MAJOR) — enroll is the one message with **no exception guard**; a
concurrent delete (or any GPU/PIL error) kills the WebSocket
`worker.py:487-489`. Control messages are wrapped in try/except
(`worker.py:491-494`), and `detect` failures are contained, but the enroll
branch does `reply = await loop.run_in_executor(None, enroll, ...)` bare.
`enroll` calls `_store.add_view(item["id"], ...)` (worker.py:345) after having
resolved the item via a *separate* `visible_items`/`find_by_name` call — a
classic read-modify-write TOCTOU. If another connection `forget`s that item in
the window, `add_view` raises `KeyError` (items_store.py:181), which propagates
out of the executor, out of `ws_endpoint`, and tears the connection down with no
error reply to the phone. Any other enroll-time exception (torch OOM, cv2/PIL
failure, corrupt enroll JPEG that slips past decode) does the same. Fix: wrap
the enroll dispatch in try/except like the control path and return a `{"type":
"error"}` reply; consider making `add_view` tolerant of a vanished id.

### F11 (MINOR) — 8 MB text frame → `json.loads` recursion / memory
`worker.py:452-462` and proxy `max_msg_size=8*1024*1024`
(public_webserver.py:2638,2646). A hostile 8 MB text frame of deeply-nested
JSON (`[[[[...`) makes `json.loads` raise `RecursionError` (verified — Python's
default limit trips well before 8 MB). `RecursionError` is *not* a subclass of
`ValueError`, so the `except ValueError` on line 460 does **not** catch it; it
falls through to the `else` branch? No — it's raised on line 457 inside the try,
uncaught, propagating out of `ws_endpoint` and killing the connection (no error
reply). Same 8 MB spent per frame across up to `FINDIT_MAX_SESSIONS` (3)
connections. Low impact (one connection dies, worker survives) but a trivially
crafted frame drops a session silently. Fix: `except (ValueError, RecursionError)`
and/or a length sanity check before parse.

### F12 (MINOR) — base64 decoded with implicit `validate=False`
`worker.py:478-479`. Junk after the `,` is silently accepted and truncated;
non-base64 bytes are ignored rather than rejected, so a malformed image yields
an empty/garbage `jpeg` → `cv2.imdecode` returns None → handled ("could not
decode enroll image"). Not exploitable, just noting the permissive decode.

### F13 (info) — item names/places: XSS surface checked, clean
`findit.html:200-203` `esc()` is applied at every `innerHTML` sink for
`item.name` and `l.place` (renderItems:582, locations list:639); titles use
`textContent`; the thumb data-URL is regex-validated (line 580). No injection
found. Good.

---

## 4. Matching / detection

### F14 (info) — generic-only searches unchanged from v1; no low-conf leak
`worker.py:255,296-308`. `det_conf` only drops to `CUSTOM_CONF_FLOOR` when a
searched term resolves to a visible custom item (`custom_searched` non-empty).
Pure generic searches keep `det_conf = conf`, matching v1. For mixed searches,
low-conf boxes that CLIP/DINO didn't claim are dropped when `box["conf"] < conf`
(line 306), so a low-conf generic box of a *different* class cannot leak through.
Verified end to end. Correct.

### F15 (MINOR) — model-mismatched custom item degrades searches silently
`worker.py:230-236,255`. If a searched custom item was enrolled under the other
embed model, `_usable` excludes it → `matchable`/`relabel_bases` empty → nothing
relabels, yet `det_conf` still drops to 0.12 for the frame (because
`custom_searched` is non-empty). Result: the detector runs hot but all the extra
low-conf boxes are dropped at line 306, so the user sees only base-class boxes
that clear their own slider and never a match — with no message explaining the
item is dormant until re-added. Enroll *does* warn (line 341-344) but search does
not. Minor UX/correctness. Consider surfacing "this item needs re-adding" when a
searched item is non-`_usable`.

### F16 (MAJOR) — first-ever DINOv2 load with no internet AND no torch.hub cache
leaves the worker permanently 503 (not a crash, a hang)
`worker.py:361-373,124-146`. `_warm` runs `detect()` (YOLO — already cached),
then for dinov2 calls `_embed_pil` → `_get_dino` → `torch.hub.load(
"facebookresearch/dinov2", ...)`. With no cached hub checkout and no internet,
that raises inside the daemon `_warm` thread, which has **no try/except**, so the
thread dies, `_ready` is never set, and `/healthz` returns 503 until
`_wait_ready` gives up at 240 s → client gets `HTTPServiceUnavailable`. The
worker process stays alive (holding the port/VRAM) but useless; the reconciler
won't reap it for `FINDIT_IDLE_TIMEOUT` (900 s) since it's "running". The
exception is swallowed with no log line, so the operator sees only "worker not
ready after 240s". YOLO warming successfully but the whole feature being gated on
an un-cached network download is the trap. Fix: catch and log in `_warm`; and/or
don't fail readiness on the embed warmup (degrade to detection-only), or verify
the hub cache exists before advertising dinov2.

---

## 5. Client state machine (findit.html)

### F17 (MINOR) — `pendingLocationItem` left dangling after cancelled name prompt
`findit.html:704-713, 511-524`. Tapping the Found pill on a *generic* box sets
`pendingLocationItem = 'await'` then calls `nameBox`. If the user cancels the
`prompt` (`name === null`), `nameBox` returns at line 517 **without** clearing
`pendingLocationItem`. It stays `'await'`, so the *next* successful non-add-mode
enroll (e.g. tapping any box and naming it) will unexpectedly pop the location
sheet (handle → enrolled → `pendingLocationItem === 'await'` branch, line
384-387). Fix: reset `pendingLocationItem = null` when the pill's `nameBox` is
cancelled.

### F18 (MINOR) — late `enrolled` after add-mode cancel is dropped on the floor
`findit.html:375-389, 781-791`. If a capture's `enroll` is in flight when the
user hits cancel/✔ (`finishAddMode(false)` sets `addMode=false`), the arriving
`enrolled` reply hits neither the `addMode` nor the `pendingLocationItem`
branch, so the capture-dots/target flow is lost — but the item WAS created
server-side. Harmless (item persists, list refreshes) but the guided-add view
count can end up inconsistent with reality. Cosmetic.

### F19 (MINOR) — items cache is not keyed by profile
`findit.html:196,370,393`. `findit_items_cache` is a single global localStorage
key. Because the uuid is fixed per browser it never actually serves the wrong
profile's items, but if the uuid mechanism ever changes (or someone clears
`findit_profile` but not the cache) the stale cache would show another identity's
items until the first `hello_ok`. Defense-in-depth: key the cache by profileId.

### F20 (info) — reconnect re-sends hello+query, overwrites myItems
`findit.html:329`. `onopen` always `sendHello(); sendQuery()`. Reconnect mid-add
re-fetches items (fine, idempotent) and preserves `addModeItemId` client-side.
No bug found, but the add-mode flow spanning a reconnect is untested.

---

## 6. PWA routes / webserver

### F21 (info) — icon path traversal: none (hardcoded fnames), confirmed
`public_webserver.py:340-342,2616-2625`. The loop registers exactly two literal
filenames and `_make_findit_asset_handler` closes over the constant `fname`; the
request path never feeds the filesystem lookup. Safe.

### F22 (MINOR) — icon `Cache-Control: public, max-age=86400` vs the invisibility
contract
`public_webserver.py:2624`. The page and manifest use `no-cache` and 404 when the
toggle is off, but the two PNG icons are served `public, max-age=86400`. A
CDN/browser (hatmaster.tv is behind Cloudflare per the memory notes) can cache
the 200 icon for a day, so after the feature is toggled off the icons may still
be served from cache even though everything else 404s. The icons aren't secret,
but it's a small crack in the "indistinguishable from a URL that never existed"
claim. Consider `no-cache` for consistency, or accept it as documented.

---

## Tests: gaps worth filling
- **No profile-isolation test.** `test_items_store.py` uses dev-a/dev-b only for
  name-collision. Nothing asserts that dev-b *cannot* rename/forget/log/enroll
  into dev-a's **private** item, nor that F6's "make private steals a shared
  item" is prevented (it currently isn't — a test would have caught it).
- **No concurrency test.** F10's enroll-vs-forget TOCTOU and the "stale item
  dict" scenarios are unexercised; a threaded test hammering add_view/delete on
  one id would surface the KeyError-out-of-executor crash.
- **No corrupt/partial-v2 test.** F1 (missing profiles/items keys) and F3
  (non-dict JSON) both crash or wipe with no coverage. Only the happy v1 path is
  tested.
- **No views cap / large-input test** (F5, F11) — nothing bounds embeds or feeds
  a hostile oversized/nested frame.
- **Integration test is single-connection.** `test_findit_public.py` never opens
  two WebSockets, so every cross-profile and concurrency path above is untested
  through the real proxy.
- **embed_model mismatch:** no test that a clip-tagged item is dormant under
  dinov2 and that enroll returns the "older model" error (F15).
