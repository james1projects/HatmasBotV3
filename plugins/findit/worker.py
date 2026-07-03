"""FindIt detection worker — runs OUTSIDE the bot process.

Open-vocabulary object detection (YOLO-World) + custom-item recognition
(CLIP image embeddings). The phone client at hatmaster.tv/FindIt streams
camera frames over a WebSocket that the public webserver proxies to this
process; boxes come back as JSON.

This file is launched as a child process by plugins/findit/plugin.py
using the dedicated .venv-findit interpreter (torch + ultralytics live
there, NOT in the bot's environment). It binds 127.0.0.1 only — the only
path in from outside is bot -> public webserver -> proxy.

Run standalone for debugging:
    .venv-findit\\Scripts\\python.exe plugins\\findit\\worker.py --port 8474

Protocol v2 (WebSocket /ws) — item storage lives in items_store.py.
A connection belongs to one profile (device); until "hello" arrives it
acts as the "shared" profile, which is also exactly the v1 behavior, so
old clients keep working.

    -> {"type": "hello", "profile": "<uuid>", "name": "James"}
    <- {"type": "hello_ok", "profile_id":, "name":, "items": [summary...]}
    -> {"type": "query", "classes": ["ketchup bottle"], "conf": 0.2}
    -> binary JPEG frame
    <- {"type": "detections", "boxes": [...], "w":, "h":, "ms":}
       (custom boxes carry "item_id" and "sim")
    -> {"type": "enroll", "name": "my keys", "base": "keys",
        "image": dataURL, "item_id": optional}
    <- {"type": "enrolled", "item_id":, "name":, "views":, "thumb": dataURL}
    -> {"type": "forget", "item_id": ...}        (v1 {"name": ...} still works)
    <- {"type": "forgot", "item_id":, "name":}
    -> {"type": "rename", "item_id":, "name":}         <- {"type": "renamed", ...}
    -> {"type": "set_shared", "item_id":, "shared":}   <- {"type": "shared_set", ...}
    -> {"type": "list_items"}                          <- {"type": "items", "items": [...]}
    -> {"type": "log_location", "item_id":, "place":}  <- {"type": "location_logged", ...}
    Any handled failure -> {"type": "error", "message": ...}
"""
import argparse
import asyncio
import base64
import json
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse

from items_store import ItemsStore

app = FastAPI()

_model = None
# One GPU job at a time: the model is stateful (set_classes) and CLIP is shared.
_infer_lock = threading.Lock()
_loaded_classes: list[str] = []
_store: ItemsStore | None = None
_ready = False

# When the searched terms include a custom item, the detector runs at this
# floor instead of the user's confidence: CLIP-embedding verification acts
# as the real filter for those boxes, so we can afford far higher recall on
# the detector side (the v1 pipeline could never find an item whose base
# class the detector missed at user conf).
CUSTOM_CONF_FLOOR = 0.12
# A box is only relabeled as a custom item if its best similarity beats the
# second-best *other* item by this much — stops "everything key-shaped is
# my keys" when several similar items are enrolled.
MATCH_MARGIN = 0.04
# Never CLIP-embed more than this many candidate boxes per frame.
MAX_CLIP_CROPS = 32

# Extra detector prompts per base class. YOLO-World's recall depends a lot
# on prompt wording; several phrasings of the same thing, all mapped back
# to one canonical label, is a cheap recall boost.
SYNONYMS = {
    "keys": ["key", "keychain", "key ring"],
    "wallet": ["billfold", "leather wallet"],
    "remote": ["remote control", "tv remote"],
    "glasses": ["eyeglasses", "reading glasses", "spectacles"],
    "sunglasses": ["shades"],
    "phone": ["cell phone", "mobile phone", "smartphone"],
    "cup": ["mug", "coffee cup"],
    "bottle": ["water bottle", "drink bottle"],
    "headphones": ["earbuds", "headset"],
    "controller": ["game controller", "gamepad"],
    "hat": ["cap", "beanie"],
    "shoe": ["sneaker", "trainer"],
    "bag": ["handbag", "tote bag"],
}


def _get_model():
    global _model
    if _model is None:
        import torch
        from ultralytics import YOLO
        _model = YOLO(app.state.model_name)
        # Move to GPU BEFORE the first set_classes: ultralytics caches its CLIP
        # text encoder on whatever device the model is on at that moment, and a
        # CPU-cached encoder breaks after predict() migrates weights to CUDA.
        if torch.cuda.is_available():
            _model.to("cuda")
    return _model


def _clip():
    """The CLIP wrapper ultralytics caches after the first set_classes call."""
    model = _get_model()
    clip = getattr(model.model, "clip_model", None)
    if clip is None:
        global _loaded_classes
        model.set_classes(["object"])
        _loaded_classes = ["object"]
        clip = model.model.clip_model
    return clip


def _crop_pil(img_bgr, x1, y1, x2, y2, margin=0.12):
    """Crop a box (with a little context margin) as a PIL image, or None if tiny."""
    from PIL import Image
    h, w = img_bgr.shape[:2]
    mx, my = (x2 - x1) * margin, (y2 - y1) * margin
    xa, ya = max(0, int(x1 - mx)), max(0, int(y1 - my))
    xb, yb = min(w, int(x2 + mx)), min(h, int(y2 + my))
    if xb - xa < 8 or yb - ya < 8:
        return None
    return Image.fromarray(cv2.cvtColor(img_bgr[ya:yb, xa:xb], cv2.COLOR_BGR2RGB))


def _make_thumb(img_bgr, max_side=128):
    """Small JPEG for the gallery tile, or None if encoding fails."""
    h, w = img_bgr.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1:
        img_bgr = cv2.resize(img_bgr, (max(1, round(w * scale)),
                                       max(1, round(h * scale))))
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else None


def _rank_items(vec, items):
    """Cosine similarity of vec against every view of every item; returns
    (best_item, best_sim, runner_up_sim). CLIP image embeddings from
    ultralytics are unit-norm, so the dot product IS the cosine."""
    best, best_sim, second = None, 0.0, 0.0
    for item in items.values():
        if not item["embeds"]:
            continue
        sim = max(float(np.dot(vec, np.asarray(e, dtype=np.float32)))
                  for e in item["embeds"])
        if sim > best_sim:
            best, best_sim, second = item, sim, best_sim
        elif sim > second:
            second = sim
    return best, best_sim, second


def detect(jpeg: bytes, terms: list[str], conf: float, profile_id: str = "shared"):
    """Decode a JPEG, run open-vocab detection, return (boxes, w, h, ms).

    `terms` are the user's raw search terms and may include custom item
    names (resolved against the profile's visible items). Custom searches
    run the detector at CUSTOM_CONF_FLOOR and let CLIP-similarity gating
    do the filtering; boxes that neither clear the user's confidence nor
    get relabeled as a custom item are dropped.
    """
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return [], 0, 0, 0.0
    h, w = img.shape[:2]
    requested = {t.lower() for t in terms}

    visible = _store.visible_items(profile_id) if _store else {}
    by_name = {it["name"].lower(): it for it in visible.values()}
    custom_searched = [by_name[t] for t in requested if t in by_name]
    # base classes of ALL visible items — boxes of these classes are the
    # only ones worth CLIP-checking for a custom relabel
    relabel_bases = {it["base_class"].lower() for it in visible.values()
                     if it["embeds"]}

    # Build detector prompts: canonical term + synonym phrasings, with a map
    # back so every output box carries the canonical label the user typed.
    prompt_to_canon: dict[str, str] = {}
    det_classes: list[str] = []

    def add_prompt(p, canon):
        if p.lower() not in prompt_to_canon:
            prompt_to_canon[p.lower()] = canon
            det_classes.append(p)

    for t in terms:
        item = by_name.get(t.lower())
        canon = item["base_class"] if item else t
        add_prompt(canon, canon)
        for s in SYNONYMS.get(canon.lower(), []):
            add_prompt(s, canon)

    det_conf = min(conf, CUSTOM_CONF_FLOOR) if custom_searched else conf

    t0 = time.perf_counter()
    global _loaded_classes
    boxes = []
    with _infer_lock:
        model = _get_model()
        if det_classes != _loaded_classes:
            model.set_classes(det_classes)
            _loaded_classes = list(det_classes)
        result = model.predict(img, conf=det_conf, verbose=False)[0]
        for b in result.boxes:
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
            raw_label = result.names[int(b.cls[0])]
            boxes.append({
                "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "conf": round(float(b.conf[0]), 3),
                "label": prompt_to_canon.get(raw_label.lower(), raw_label),
                "custom": False,
            })

        # CLIP pass: try to relabel candidate boxes as enrolled items.
        # Only boxes whose class is some visible item's base class are
        # candidates, highest-confidence first, capped per frame.
        candidates = sorted(
            (b for b in boxes if b["label"].lower() in relabel_bases),
            key=lambda b: b["conf"], reverse=True)[:MAX_CLIP_CROPS]
        for box in candidates:
            pil = _crop_pil(img, box["x1"], box["y1"], box["x2"], box["y2"])
            if pil is None:
                continue
            vec = _clip().encode_image(pil)[0].cpu().numpy()
            item, sim, second = _rank_items(vec, visible)
            if (item is not None and sim >= app.state.sim_threshold
                    and sim - second >= MATCH_MARGIN):
                box["label"] = item["name"]
                box["custom"] = True
                box["item_id"] = item["id"]
                box["sim"] = round(sim, 3)
    ms = (time.perf_counter() - t0) * 1000

    kept = []
    for box in boxes:
        if box["custom"]:
            item = by_name.get(box["label"].lower())
            base = (item or {}).get("base_class", "").lower()
            box["match"] = box["label"].lower() in requested or base in requested
        else:
            box["match"] = box["label"].lower() in requested
            # Low-conf boxes existed only as custom-item candidates; if CLIP
            # didn't claim them they must clear the user's own threshold.
            if box["conf"] < conf:
                continue
        kept.append(box)
    return kept, w, h, ms


def enroll(profile_id: str, name: str, base: str, jpeg: bytes,
           item_id: str | None = None):
    """Embed the crop and store it as a view of an item (creating the item
    if needed). Returns the reply dict for the client."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return {"type": "error", "message": "could not decode enroll image"}
    from PIL import Image
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    with _infer_lock:
        vec = _clip().encode_image(pil)[0].cpu().tolist()
    thumb = _make_thumb(img)

    if item_id:
        item = _store.visible_items(profile_id).get(item_id)
        if item is None:
            return {"type": "error", "message": "unknown item"}
    else:
        # tapping an already-custom box passes its custom name as base;
        # resolve to that item's true base class
        base_item = _store.find_by_name(profile_id, base)
        base_class = base_item["base_class"] if base_item else base
        item = _store.find_by_name(profile_id, name)
        if item is None:
            try:
                item = _store.create_item(profile_id, name, base_class)
            except ValueError as e:
                return {"type": "error", "message": str(e)}
    views = _store.add_view(item["id"], vec, thumb)
    reply = {"type": "enrolled", "item_id": item["id"],
             "name": item["name"], "views": views, "thumb": None}
    if thumb:
        reply["thumb"] = ("data:image/jpeg;base64,"
                          + base64.b64encode(thumb).decode("ascii"))
    return reply


@app.on_event("startup")
async def warmup():
    global _store
    _store = ItemsStore(app.state.items_path)
    print(f"[worker] loaded {len(_store.visible_items('shared'))} shared "
          f"item(s)", flush=True)

    def _warm():
        global _ready
        dummy = cv2.imencode(".jpg", np.zeros((480, 640, 3), np.uint8))[1].tobytes()
        detect(dummy, ["warmup object"], 0.5)
        _ready = True
        print("[worker] model warmed up and ready", flush=True)
    threading.Thread(target=_warm, daemon=True).start()


@app.get("/healthz")
async def healthz():
    """200 once the model is loaded and warm; 503 while still loading.
    The plugin polls this after launching the process."""
    if _ready:
        return PlainTextResponse("ok")
    return PlainTextResponse("warming up", status_code=503)


def _handle_control(data: dict, profile: dict):
    """Non-frame, non-enroll messages. Runs on the event loop (store ops are
    quick and lock internally). Returns a reply dict, or None for no reply."""
    kind = data.get("type")
    pid = profile["id"]

    if kind == "hello":
        raw = str(data.get("profile") or "").strip()[:64]
        # device ids come from the client — keep them key-safe; an empty or
        # stripped-to-nothing id falls back to the shared profile
        pid = "".join(c for c in raw if c.isalnum() or c in "-_") or "shared"
        name = str(data.get("name") or "").strip()[:40] or "Someone"
        if pid != "shared":
            _store.register_profile(pid, name)
        profile["id"] = pid
        profile["name"] = name
        return {"type": "hello_ok", "profile_id": pid, "name": name,
                "items": _store.summaries_for(pid, with_thumb=True)}

    if kind == "list_items":
        return {"type": "items",
                "items": _store.summaries_for(pid, with_thumb=True)}

    if kind == "forget":
        item = None
        if data.get("item_id"):
            item = _store.visible_items(pid).get(str(data["item_id"]))
        elif data.get("name"):   # v1 compat
            item = _store.find_by_name(pid, str(data["name"]).strip())
        if item is None:
            return {"type": "error", "message": "unknown item"}
        _store.delete(item["id"])
        return {"type": "forgot", "item_id": item["id"], "name": item["name"]}

    if kind in ("rename", "set_shared", "log_location"):
        item = _store.visible_items(pid).get(str(data.get("item_id") or ""))
        if item is None:
            return {"type": "error", "message": "unknown item"}
        try:
            if kind == "rename":
                it = _store.rename(item["id"], str(data.get("name") or ""), pid)
                return {"type": "renamed", "item_id": it["id"], "name": it["name"]}
            if kind == "set_shared":
                it = _store.set_shared(item["id"], bool(data.get("shared")), pid)
                return {"type": "shared_set", "item_id": it["id"],
                        "shared": it["owner"] == "shared"}
            place = str(data.get("place") or "").strip()
            if not place:
                return {"type": "error", "message": "location needs a place"}
            locs = _store.log_location(item["id"], place)
            return {"type": "location_logged", "item_id": item["id"],
                    "locations": locs}
        except ValueError as e:
            return {"type": "error", "message": str(e)}

    return None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    terms: list[str] = []
    conf = 0.2
    # per-connection identity; "shared" until a hello arrives (= v1 clients)
    profile = {"id": "shared", "name": "Shared"}
    loop = asyncio.get_running_loop()
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            break
        if msg.get("text") is not None:
            try:
                data = json.loads(msg["text"])
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except ValueError:
                await ws.send_text(json.dumps(
                    {"type": "error", "message": "malformed message"}))
                continue
            kind = data.get("type")
            if kind == "query":
                raw = data.get("classes", [])
                if isinstance(raw, list):
                    terms = [str(c).strip() for c in raw if str(c).strip()][:8]
                try:
                    conf = max(0.01, min(0.95, float(data.get("conf", conf))))
                except (TypeError, ValueError):
                    pass
            elif kind == "enroll":
                name = str(data.get("name") or "").strip()[:60]
                base = str(data.get("base") or "object").strip()[:60]
                item_id = str(data.get("item_id") or "") or None
                try:
                    jpeg = base64.b64decode(
                        str(data.get("image", "")).split(",", 1)[-1])
                except (ValueError, IndexError):
                    jpeg = b""
                if (not name and not item_id) or not jpeg:
                    await ws.send_text(json.dumps(
                        {"type": "error",
                         "message": "enroll needs a name and an image"}))
                    continue
                reply = await loop.run_in_executor(
                    None, enroll, profile["id"], name, base, jpeg, item_id)
                await ws.send_text(json.dumps(reply))
            else:
                try:
                    reply = _handle_control(data, profile)
                except Exception as e:
                    reply = {"type": "error", "message": f"internal: {e}"}
                if reply is not None:
                    await ws.send_text(json.dumps(reply))
        elif msg.get("bytes"):
            if not terms:
                await ws.send_text(json.dumps(
                    {"type": "detections", "boxes": [], "w": 0, "h": 0, "ms": 0}))
                continue
            boxes, w, h, ms = await loop.run_in_executor(
                None, detect, msg["bytes"], terms, conf, profile["id"])
            await ws.send_text(json.dumps(
                {"type": "detections", "boxes": boxes, "w": w, "h": h,
                 "ms": round(ms, 1)}))


@app.post("/detect")
async def detect_http(request: Request, classes: str, conf: float = 0.2,
                      profile: str = "shared"):
    """Single-image endpoint for testing: POST a JPEG body, get boxes back."""
    body = await request.body()
    cls = [c.strip() for c in classes.split(",") if c.strip()]
    boxes, w, h, ms = await asyncio.get_running_loop().run_in_executor(
        None, detect, body, cls, conf, profile)
    return JSONResponse({"boxes": boxes, "w": w, "h": h, "ms": round(ms, 1)})


def main():
    p = argparse.ArgumentParser(description="FindIt detection worker")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--model", default="yolov8l-worldv2.pt",
                   help="Ultralytics open-vocab model. Resolved relative to the "
                        "process cwd (the plugin sets cwd=data/findit so weights "
                        "download there, not into the repo root).")
    p.add_argument("--sim-threshold", type=float, default=0.80,
                   help="CLIP cosine similarity needed to relabel a box as a "
                        "custom item (lower = matches more eagerly)")
    p.add_argument("--items", default="items.json",
                   help="Path of the custom-item gallery JSON")
    args = p.parse_args()
    app.state.model_name = args.model
    app.state.sim_threshold = args.sim_threshold
    app.state.items_path = args.items

    print(f"[worker] starting on 127.0.0.1:{args.port} "
          f"(model={args.model}, sim>={args.sim_threshold})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
