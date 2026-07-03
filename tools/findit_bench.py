#!/usr/bin/env python3
"""
Benchmark driver for findit object-detection worker service.
Usage: <repo>/.venv-findit/Scripts/python.exe tools/findit_bench.py --images <dir>
"""
import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
import requests
import websockets.sync.client as ws_client


def parse_args():
    p = argparse.ArgumentParser(description="findit benchmark driver")
    p.add_argument("--images", required=True, help="directory containing benchmark JPEGs and ground_truth.json")
    p.add_argument("--port", type=int, default=8475)
    p.add_argument("--repo", type=str, default=None, help="repo root (default: parent of this script's directory)")
    p.add_argument("--sim-threshold", type=float, default=0.80)
    p.add_argument("--out", type=str, default=None)
    p.add_argument("--skip-instance", action="store_true")
    p.add_argument("--conf", type=float, default=0.2)
    return p.parse_args()


def iou(b1, b2):
    xA = max(b1[0], b2[0])
    yA = max(b1[1], b2[1])
    xB = min(b1[2], b2[2])
    yB = min(b1[3], b2[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    area1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
    area2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
    union = area1 + area2 - inter
    return inter / union if union > 0 else 0.0


def run_detection(port, img_dir, gt, conf):
    targets = gt["targets"]
    classes_str = ",".join(targets)
    stats = {c: {"gt": 0, "hits": 0} for c in targets}
    overall_gt = 0
    overall_hits = 0
    neg_fp = 0
    stray_total = 0
    lats = []

    for img in gt["images"]:
        fpath = img_dir / img["file"]
        with open(fpath, "rb") as f:
            data = f.read()
        r = requests.post(f"http://127.0.0.1:{port}/detect?classes={classes_str}&conf={conf}", data=data)
        res = r.json()
        lats.append(res["ms"])

        gt_labels = set(b["label"] for b in img["boxes"])
        gt_rects = [{"label": b["label"], "rect": [b["xywh"][0], b["xywh"][1], b["xywh"][0]+b["xywh"][2], b["xywh"][1]+b["xywh"][3]]} for b in img["boxes"]]
        preds = res["boxes"]

        matched = set()
        for g in gt_rects:
            best_iou = -1.0
            best_idx = -1
            for i, p in enumerate(preds):
                if i in matched or p["label"] != g["label"]:
                    continue
                sc = iou(g["rect"], [p["x1"], p["y1"], p["x2"], p["y2"]])
                if sc > best_iou and sc >= 0.5:
                    best_iou = sc
                    best_idx = i
            if best_idx != -1:
                matched.add(best_idx)
                stats[g["label"]]["hits"] += 1
                overall_hits += 1

        for g in gt_rects:
            stats[g["label"]]["gt"] += 1
            overall_gt += 1

        if img["negative"]:
            neg_fp += sum(1 for p in preds if p["label"] in targets)
        else:
            stray_total += sum(1 for p in preds if p["label"] in targets and p["label"] not in gt_labels)

    per_class = {}
    for c in targets:
        g = stats[c]["gt"]
        h = stats[c]["hits"]
        per_class[c] = {"gt": g, "hits": h, "recall": h / g if g > 0 else 1.0}

    return {
        "per_class": per_class,
        "overall_recall": overall_hits / overall_gt if overall_gt > 0 else 1.0,
        "negatives_fp": neg_fp,
        "stray_boxes": stray_total,
        "latency_mean": float(np.mean(lats)) if lats else 0.0,
        "latency_median": float(np.median(lats)) if lats else 0.0
    }


def run_instance(port, img_dir, gt):
    class_imgs = {}
    for img in gt["images"]:
        if img["negative"]:
            continue
        labels = set(b["label"] for b in img["boxes"])
        for lbl in labels:
            class_imgs.setdefault(lbl, []).append(img)

    eligible = [c for c, imgs in class_imgs.items() if len(imgs) >= 2]
    orig_hits = 0
    aug_hits = 0
    aug_total = 0
    fp_count = 0

    uri = f"ws://127.0.0.1:{port}/ws"
    with ws_client.connect(uri) as ws:
        for cls in eligible:
            imgs = class_imgs[cls]
            img_areas = []
            for im in imgs:
                mx = 0
                for b in im["boxes"]:
                    if b["label"] == cls:
                        a = b["xywh"][2] * b["xywh"][3]
                        if a > mx:
                            mx = a
                img_areas.append((im, mx))
            img_areas.sort(key=lambda x: x[1], reverse=True)

            im_a = img_areas[0][0]
            im_b = img_areas[1][0]

            a_img = cv2.imread(str(img_dir / im_a["file"]))
            h, w = a_img.shape[:2]

            best_box = max((b for b in im_a["boxes"] if b["label"] == cls), key=lambda b: b["xywh"][2]*b["xywh"][3])
            # COCO bbox values are floats; numpy slicing needs ints
            x, y, bw, bh = (int(round(v)) for v in best_box["xywh"])
            mx_m = int(bw * 0.12)
            my_m = int(bh * 0.12)
            crop = a_img[max(0,y-my_m):min(h,y+bh+my_m), max(0,x-mx_m):min(w,x+bw+mx_m)]

            _, buf = cv2.imencode('.jpg', crop)
            dataurl = f"data:image/jpeg;base64,{base64.b64encode(buf).decode()}"

            ws.send(json.dumps({"type": "enroll", "name": f"bench my {cls}", "base": cls, "image": dataurl}))
            assert json.loads(ws.recv())["type"] == "enrolled"

            ws.send(json.dumps({"type": "query", "classes": [f"bench my {cls}"], "conf": 0.2}))

            def detect(raw):
                ws.send(raw)
                return json.loads(ws.recv()).get("boxes", [])

            _, buf_a = cv2.imencode('.jpg', a_img)
            if any(b.get("custom") and b["label"] == f"bench my {cls}" for b in detect(buf_a.tobytes())):
                orig_hits += 1

            for alpha in [1.3, 0.7]:
                aug = cv2.convertScaleAbs(a_img, alpha=alpha, beta=0)
                _, buf_aug = cv2.imencode('.jpg', aug)
                if any(b.get("custom") and b["label"] == f"bench my {cls}" for b in detect(buf_aug.tobytes())):
                    aug_hits += 1
                aug_total += 1

            M = cv2.getRotationMatrix2D((w/2, h/2), 12, 1.0)
            rot = cv2.warpAffine(a_img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
            _, buf_rot = cv2.imencode('.jpg', rot)
            if any(b.get("custom") and b["label"] == f"bench my {cls}" for b in detect(buf_rot.tobytes())):
                aug_hits += 1
            aug_total += 1

            b_img = cv2.imread(str(img_dir / im_b["file"]))
            _, buf_b = cv2.imencode('.jpg', b_img)
            if any(b.get("custom") and b["label"] == f"bench my {cls}" for b in detect(buf_b.tobytes())):
                fp_count += 1

            ws.send(json.dumps({"type": "forget", "name": f"bench my {cls}"}))
            assert json.loads(ws.recv())["type"] == "forgot"

    nc = len(eligible)
    return {
        "instance_recall_original": orig_hits / max(nc, 1),
        "instance_recall_augmented": aug_hits / max(aug_total, 1),
        "instance_fp_rate": fp_count / max(nc, 1),
        "n_classes": nc
    }


def print_summary(det, inst):
    print("\n=== Detection Benchmark ===")
    print(f"{'Class':<15} {'GT':>5} {'Hits':>5} {'Recall':>7}")
    for c, v in det["per_class"].items():
        print(f"{c:<15} {v['gt']:>5} {v['hits']:>5} {v['recall']:>7.2%}")
    print(f"{'OVERALL':<15} {'':>5} {'':>5} {det['overall_recall']:>7.2%}")
    print(f"Negatives FP: {det['negatives_fp']} | Stray boxes: {det['stray_boxes']}")
    print(f"Latency (mean/median): {det['latency_mean']:.1f} / {det['latency_median']:.1f} ms\n")

    if inst:
        print("=== Instance Benchmark ===")
        print(f"Classes tested: {inst['n_classes']}")
        print(f"Original recall: {inst['instance_recall_original']:.2%}")
        print(f"Augmented recall: {inst['instance_recall_augmented']:.2%}")
        print(f"FP rate on B: {inst['instance_fp_rate']:.2%}\n")


def main():
    args = parse_args()
    img_dir = Path(args.images).resolve()
    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parent.parent

    tmp = tempfile.mkdtemp()
    items_json = Path(tmp) / "items.json"
    py_exe = repo / ".venv-findit" / "Scripts" / "python.exe"
    script = repo / "plugins" / "findit" / "worker.py"
    cwd = repo / "data" / "findit"

    cmd = [str(py_exe), str(script), "--port", str(args.port), "--model", "yolov8l-worldv2.pt",
           "--sim-threshold", str(args.sim_threshold), "--items", str(items_json)]

    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=sys.stdout, stderr=sys.stderr)

    try:
        t0 = time.time()
        while time.time() - t0 < 300:
            if proc.poll() is not None:
                print(f"Worker exited early with status {proc.returncode}")
                sys.exit(2)
            try:
                r = requests.get(f"http://127.0.0.1:{args.port}/healthz", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("Timeout waiting for worker healthz")
            sys.exit(1)

        with open(img_dir / "ground_truth.json") as f:
            gt = json.load(f)

        det = run_detection(args.port, img_dir, gt, args.conf)
        inst = {} if args.skip_instance else run_instance(args.port, img_dir, gt)

        out = Path(args.out) if args.out else img_dir / "bench_results.json"
        with open(out, "w") as f:
            json.dump({"detection": det, "instance": inst, "config": vars(args)}, f, indent=2)

        print_summary(det, inst)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
