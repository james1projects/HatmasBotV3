"""Integration test for FindIt on the public webserver.

Exercises the real PublicWebServer + FindItPlugin + GPU worker chain
WITHOUT starting the bot (no Twitch, no OBS, no DB): a stub bot supplies
the feature toggle, aiohttp's TestServer hosts the real app on an
ephemeral port, and the plugin spawns the real detection worker.

Checks:
  1. toggle OFF -> /FindIt and /ws/findit are 404 (invisible)
  2. toggle ON  -> /FindIt serves the page, /findit redirects
  3. WebSocket proxy end-to-end: query + JPEG frame -> detections
  4. enroll/forget round-trip through the proxy
  4b. v2 protocol: hello/profiles, list_items, locations, rename, error replies
  5. toggle OFF mid-run -> reconciler kills the worker; page 404s again

Run:  python tools\\test_findit_public.py     (system python, bot env)
Exit 0 = all pass. The worker's first-ever start may take a couple of
minutes (CLIP auto-install); later runs are ~15-30s.
"""
import asyncio
import base64
import io
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from core.public_webserver import PublicWebServer
from plugins.findit import FindItPlugin
import plugins.findit.plugin as findit_plugin

BUS_JPG = (Path(__file__).resolve().parent.parent / ".venv-findit" / "Lib"
           / "site-packages" / "ultralytics" / "assets" / "bus.jpg")

# Machine-bound integration test: it spawns the REAL GPU worker out of
# .venv-findit (torch/ultralytics live there, not in the bot env). On a
# box without that venv — CI included — there is nothing real to test,
# so skip loudly rather than fail or fake it.
if not BUS_JPG.exists():
    print("[SKIP] test_findit_public: .venv-findit (FindIt GPU worker env) "
          "not present on this machine — integration test skipped.")
    sys.exit(0)

# Hermetic item store: the worker must NOT open the live gallery
# (data/findit/items.json). Runs against the live store both polluted it
# (D1 test items showed up in the real gallery) and broke this suite —
# leftover items' embeds relabeled detection boxes in later runs. The
# worker cwd stays data/findit so the cached model weights are reused.
findit_plugin.ITEMS_PATH = (
    Path(tempfile.mkdtemp(prefix="findit_test_")) / "items.json")

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))


class StubBot:
    def __init__(self):
        self.features = {"findit": False}

    def is_feature_enabled(self, feature):
        return self.features.get(feature, False)


async def run():
    stub = StubBot()
    plugin = FindItPlugin()
    plugin.setup(stub)
    await plugin.on_ready()          # starts the reconciler task

    public = PublicWebServer(findit=plugin)
    server = TestServer(public.app)
    client = TestClient(server)
    await client.start_server()

    try:
        # ── 1. invisible while toggled off ──
        r = await client.get("/FindIt")
        check("toggle off: /FindIt is 404", r.status == 404, f"status={r.status}")
        r = await client.get("/ws/findit")
        check("toggle off: /ws/findit is 404", r.status == 404, f"status={r.status}")
        check("toggle off: no worker process", not plugin.worker_running())

        # ── 2. page appears when toggled on ──
        stub.features["findit"] = True
        r = await client.get("/FindIt")
        body = await r.text()
        check("toggle on: /FindIt serves page",
              r.status == 200 and "FindIt" in body and "nameBox" in body,
              f"status={r.status}")
        r = await client.get("/findit")   # follows the redirect
        check("/findit redirects to the page", r.status == 200)
        check("page alone does not spawn worker", not plugin.worker_running())

        # ── 3. WS proxy end-to-end ──
        jpeg = BUS_JPG.read_bytes()
        t0 = time.monotonic()
        ws = await client.ws_connect("/ws/findit", timeout=300)
        check("WS connect spawns worker",
              plugin.worker_running(),
              f"ready in {time.monotonic() - t0:.0f}s")
        await ws.send_str(json.dumps(
            {"type": "query", "classes": ["bus", "person"], "conf": 0.2}))
        await ws.send_bytes(jpeg)
        msg = json.loads((await ws.receive(timeout=60)).data)
        labels = sorted({b["label"] for b in msg["boxes"]})
        check("frame -> detections through proxy",
              msg["type"] == "detections" and "bus" in labels and "person" in labels,
              f"{len(msg['boxes'])} boxes {labels} in {msg['ms']}ms")

        # ── 4. enroll / forget through proxy ──
        # Enroll a CROP of the detected bus, exactly like the real client
        # (findit.html cuts the tapped box out of the frame before it
        # sends the enroll). A whole-scene embed barely resembles a box
        # crop under DINOv2 — instance embeddings are the point — so the
        # old full-frame enroll here stopped matching at the DINOv2 swap
        # and never represented real usage in the first place.
        bus_box = next(b for b in msg["boxes"] if b["label"] == "bus")
        crop = Image.open(io.BytesIO(jpeg)).crop(
            (int(bus_box["x1"]), int(bus_box["y1"]),
             int(bus_box["x2"]), int(bus_box["y2"])))
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=90)
        b64 = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        await ws.send_str(json.dumps(
            {"type": "enroll", "name": "Test Bus", "base": "bus", "image": b64}))
        msg = json.loads((await ws.receive(timeout=60)).data)
        check("enroll through proxy",
              msg.get("type") == "enrolled" and msg.get("views") == 1, str(msg))
        await ws.send_bytes(jpeg)
        msg = json.loads((await ws.receive(timeout=60)).data)
        custom = [b for b in msg["boxes"] if b.get("custom")]
        check("custom label round-trip",
              len(custom) == 1 and custom[0]["label"] == "Test Bus",
              f"sim={custom[0].get('sim') if custom else None}")
        await ws.send_str(json.dumps({"type": "forget", "name": "Test Bus"}))
        msg = json.loads((await ws.receive(timeout=60)).data)
        check("forget through proxy", msg.get("type") == "forgot", str(msg))

        # ── 4b. v2 protocol: hello/profiles, list_items, locations, rename, error replies ──
        await ws.send_str(json.dumps(
            {"type": "hello", "profile": "test-device-1", "name": "Tester"}))
        msg = json.loads((await ws.receive(timeout=60)).data)
        check("hello handshake", msg.get("type") == "hello_ok" and
              msg.get("profile_id") == "test-device-1" and
              isinstance(msg.get("items"), list), str(msg))

        # re-enroll the bus AFTER hello
        await ws.send_str(json.dumps(
            {"type": "enroll", "name": "Profile Bus", "base": "bus", "image": b64}))
        msg = json.loads((await ws.receive(timeout=60)).data)
        item_id = msg.get("item_id")
        check("v2 enroll returns item_id", bool(item_id), str(msg))
        thumb = msg.get("thumb")
        check("v2 enroll returns thumb", thumb and thumb.startswith("data:image/jpeg;base64,"), str(msg))

        await ws.send_str(json.dumps({"type": "list_items"}))
        msg = json.loads((await ws.receive(timeout=60)).data)
        check("list_items sees the enrollment", msg.get("type") == "items", str(msg))
        items = msg.get("items", [])
        bus_item = next((i for i in items if i.get("name") == "Profile Bus"), None)
        check("list_items sees the enrollment", bool(bus_item and
              bus_item.get("views") == 1 and bus_item.get("locations") == []), str(msg))

        await ws.send_str(json.dumps(
            {"type": "log_location", "item_id": item_id, "place": "garage shelf"}))
        msg = json.loads((await ws.receive(timeout=60)).data)
        check("log_location round-trip", msg.get("type") == "location_logged" and
              msg.get("locations") and msg["locations"][0]["place"] == "garage shelf" and
              len(msg["locations"]) == 1, str(msg))

        await ws.send_str(json.dumps(
            {"type": "rename", "item_id": item_id, "name": "Renamed Bus"}))
        msg = json.loads((await ws.receive(timeout=60)).data)
        check("rename round-trip", msg.get("type") == "renamed" and
              msg.get("name") == "Renamed Bus", str(msg))

        await ws.send_str(json.dumps(
            {"type": "log_location", "item_id": "nonexistent99", "place": "x"}))
        msg = json.loads((await ws.receive(timeout=60)).data)
        check("unknown item -> error reply", msg.get("type") == "error", str(msg))

        await ws.send_str("not json{{")
        msg = json.loads((await ws.receive(timeout=60)).data)
        check("malformed message -> error reply", msg.get("type") == "error", str(msg))

        await ws.send_str(json.dumps(
            {"type": "forget", "item_id": item_id}))
        msg = json.loads((await ws.receive(timeout=60)).data)
        check("v2 forget by item_id", msg.get("type") == "forgot", str(msg))

        # ── 4c. cross-profile isolation through the real proxy (two devices) ──
        # device-1 enrolls a PRIVATE item, then SHARES a second one.
        await ws.send_str(json.dumps(
            {"type": "enroll", "name": "D1 Private", "base": "bus", "image": b64}))
        priv = json.loads((await ws.receive(timeout=60)).data)
        priv_id = priv.get("item_id")
        await ws.send_str(json.dumps(
            {"type": "enroll", "name": "D1 Shared", "base": "bus", "image": b64}))
        shared = json.loads((await ws.receive(timeout=60)).data)
        shared_id = shared.get("item_id")
        await ws.send_str(json.dumps(
            {"type": "set_shared", "item_id": shared_id, "shared": True}))
        await ws.receive(timeout=60)

        # device-2 connects as a different profile on a second socket
        ws2 = await client.ws_connect("/ws/findit", timeout=60)
        await ws2.send_str(json.dumps(
            {"type": "hello", "profile": "test-device-2", "name": "Other"}))
        hello2 = json.loads((await ws2.receive(timeout=60)).data)
        names2 = {i["name"] for i in hello2.get("items", [])}
        check("isolation: device-2 sees shared not private",
              "D1 Shared" in names2 and "D1 Private" not in names2, str(names2))

        # device-2 cannot forget device-1's private item (invisible → unknown)
        await ws2.send_str(json.dumps({"type": "forget", "item_id": priv_id}))
        r = json.loads((await ws2.receive(timeout=60)).data)
        check("isolation: device-2 cannot forget private item",
              r.get("type") == "error", str(r))

        # device-2 cannot rename or delete device-1's SHARED item (not creator)
        await ws2.send_str(json.dumps(
            {"type": "rename", "item_id": shared_id, "name": "Hijacked"}))
        r = json.loads((await ws2.receive(timeout=60)).data)
        check("isolation: device-2 cannot rename shared item",
              r.get("type") == "error", str(r))
        await ws2.send_str(json.dumps({"type": "forget", "item_id": shared_id}))
        r = json.loads((await ws2.receive(timeout=60)).data)
        check("isolation: device-2 cannot forget shared item",
              r.get("type") == "error", str(r))

        # the "make private steals it" attack (F6) must be refused
        await ws2.send_str(json.dumps(
            {"type": "set_shared", "item_id": shared_id, "shared": False}))
        r = json.loads((await ws2.receive(timeout=60)).data)
        check("isolation: device-2 cannot steal via make-private",
              r.get("type") == "error", str(r))

        # but device-2 CAN log where it found the shared item (the point)
        await ws2.send_str(json.dumps(
            {"type": "log_location", "item_id": shared_id, "place": "hallway"}))
        r = json.loads((await ws2.receive(timeout=60)).data)
        check("isolation: device-2 can log location on shared item",
              r.get("type") == "location_logged", str(r))

        # the shared item still belongs to device-1, unrenamed
        await ws.send_str(json.dumps({"type": "list_items"}))
        back = json.loads((await ws.receive(timeout=60)).data)
        still = next((i for i in back.get("items", []) if i["id"] == shared_id), None)
        check("isolation: shared item survived the attacks intact",
              still is not None and still["name"] == "D1 Shared" and still["mine"] is True,
              str(still))

        await ws2.close()
        await ws.close()

        # ── 5. toggle off mid-run -> reconciler kills worker ──
        stub.features["findit"] = False
        deadline = time.monotonic() + 20
        while plugin.worker_running() and time.monotonic() < deadline:
            await asyncio.sleep(1)
        check("toggle off kills worker", not plugin.worker_running())
        r = await client.get("/FindIt")
        check("toggle off: page 404s again", r.status == 404, f"status={r.status}")

    finally:
        await plugin.cleanup()
        await client.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
