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
  5. toggle OFF mid-run -> reconciler kills the worker; page 404s again

Run:  python tools\\test_findit_public.py     (system python, bot env)
Exit 0 = all pass. The worker's first-ever start may take a couple of
minutes (CLIP auto-install); later runs are ~15-30s.
"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from core.public_webserver import PublicWebServer
from plugins.findit import FindItPlugin

BUS_JPG = (Path(__file__).resolve().parent.parent / ".venv-findit" / "Lib"
           / "site-packages" / "ultralytics" / "assets" / "bus.jpg")

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
        b64 = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
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
