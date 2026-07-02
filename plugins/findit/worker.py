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

Protocol (WebSocket /ws):
    -> {"type": "query", "classes": ["ketchup bottle"], "conf": 0.2}
    -> binary JPEG frame
    <- {"type": "detections", "boxes": [...], "w":, "h":, "ms":}
    -> {"type": "enroll", "name": "my keys", "base": "keys", "image": dataURL}
    <- {"type": "enrolled", "name": "my keys", "views": 1}
    -> {"type": "forget", "name": "my keys"}
    <- {"type": "forgot", "name": "my keys"}
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

app = FastAPI()

_model = None
# One GPU job at a time: the model is stateful (set_classes) and CLIP is shared.
_infer_lock = threading.Lock()
_loaded_classes: list[str] = []
# name (lowercase) -> {"name": display, "base_class": str, "embeds": [[512 floats], ...]}
_items: dict[str, dict] = {}
_ready = False


def _items_path() -> Path:
    return Path(app.state.items_path)


def _load_items():
    global _items
    path = _items_path()
    if path.exists():
        _items = json.loads(path.read_text(encoding="utf-8"))
        print(f"[worker] loaded {len(_items)} custom item(s)", flush=True)


def _save_items():
    path = _items_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_items), encoding="utf-8")
    tmp.replace(path)


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


def _best_item_match(vec):
    """Highest cosine similarity between vec and any view of any enrolled item."""
    best_key, best_sim = None, 0.0
    for key, item in _items.items():
        sim = max(float(np.dot(vec, np.asarray(e, dtype=np.float32))) for e in item["embeds"])
        if sim > best_sim:
            best_key, best_sim = key, sim
    return best_key, best_sim


def detect(jpeg: bytes, terms: list[str], conf: float):
    """Decode a JPEG, run open-vocab detection, return (boxes, w, h, ms).

    `terms` are the user's raw search terms and may include custom item
    names; those are expanded to their base class for detection, then
    boxes are relabeled by CLIP-embedding similarity against enrollments.
    """
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return [], 0, 0, 0.0
    h, w = img.shape[:2]
    requested = {t.lower() for t in terms}

    det_classes, seen = [], set()
    for t in terms:
        item = _items.get(t.lower())
        c = item["base_class"] if item else t
        if c.lower() not in seen:
            seen.add(c.lower())
            det_classes.append(c)

    t0 = time.perf_counter()
    global _loaded_classes
    boxes = []
    with _infer_lock:
        model = _get_model()
        if det_classes != _loaded_classes:
            model.set_classes(det_classes)
            _loaded_classes = list(det_classes)
        result = model.predict(img, conf=conf, verbose=False)[0]
        for b in result.boxes:
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
            boxes.append({
                "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "conf": round(float(b.conf[0]), 3),
                "label": result.names[int(b.cls[0])],
                "custom": False,
            })
        if _items:
            for box in boxes:
                pil = _crop_pil(img, box["x1"], box["y1"], box["x2"], box["y2"])
                if pil is None:
                    continue
                vec = _clip().encode_image(pil)[0].cpu().numpy()
                key, sim = _best_item_match(vec)
                if key is not None and sim >= app.state.sim_threshold:
                    box["label"] = _items[key]["name"]
                    box["custom"] = True
                    box["sim"] = round(sim, 3)
    ms = (time.perf_counter() - t0) * 1000

    for box in boxes:
        if box["custom"]:
            item = _items[box["label"].lower()]
            box["match"] = (box["label"].lower() in requested
                            or item["base_class"].lower() in requested)
        else:
            box["match"] = box["label"].lower() in requested
    return boxes, w, h, ms


def enroll(name: str, base: str, jpeg: bytes):
    """Store a CLIP embedding of the crop under `name` (extra views append)."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return {"type": "error", "message": "could not decode enroll image"}
    from PIL import Image
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    with _infer_lock:
        vec = _clip().encode_image(pil)[0].cpu().tolist()
        # tapping an already-custom box passes its custom name as base; resolve it
        base_item = _items.get(base.lower())
        base_class = base_item["base_class"] if base_item else base
        item = _items.setdefault(name.lower(),
                                 {"name": name, "base_class": base_class, "embeds": []})
        item["embeds"].append(vec)
    _save_items()
    return {"type": "enrolled", "name": item["name"], "views": len(item["embeds"])}


@app.on_event("startup")
async def warmup():
    _load_items()

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


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    terms: list[str] = []
    conf = 0.2
    loop = asyncio.get_running_loop()
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            break
        if msg.get("text") is not None:
            data = json.loads(msg["text"])
            kind = data.get("type")
            if kind == "query":
                terms = [c.strip() for c in data.get("classes", []) if c.strip()]
                conf = max(0.01, min(0.95, float(data.get("conf", conf))))
            elif kind == "enroll":
                name = (data.get("name") or "").strip()
                base = (data.get("base") or "object").strip()
                try:
                    jpeg = base64.b64decode(data.get("image", "").split(",", 1)[-1])
                except (ValueError, IndexError):
                    jpeg = b""
                if not name or not jpeg:
                    await ws.send_text(json.dumps(
                        {"type": "error", "message": "enroll needs a name and an image"}))
                    continue
                reply = await loop.run_in_executor(None, enroll, name, base, jpeg)
                await ws.send_text(json.dumps(reply))
            elif kind == "forget":
                name = (data.get("name") or "").strip().lower()
                if _items.pop(name, None) is not None:
                    _save_items()
                await ws.send_text(json.dumps({"type": "forgot", "name": name}))
        elif msg.get("bytes"):
            if not terms:
                await ws.send_text(json.dumps(
                    {"type": "detections", "boxes": [], "w": 0, "h": 0, "ms": 0}))
                continue
            boxes, w, h, ms = await loop.run_in_executor(
                None, detect, msg["bytes"], terms, conf)
            await ws.send_text(json.dumps(
                {"type": "detections", "boxes": boxes, "w": w, "h": h, "ms": round(ms, 1)}))


@app.post("/detect")
async def detect_http(request: Request, classes: str, conf: float = 0.2):
    """Single-image endpoint for testing: POST a JPEG body, get boxes back."""
    body = await request.body()
    cls = [c.strip() for c in classes.split(",") if c.strip()]
    boxes, w, h, ms = await asyncio.get_running_loop().run_in_executor(
        None, detect, body, cls, conf)
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
