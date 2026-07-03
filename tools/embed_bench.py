import argparse
import json
import time
import numpy as np
import torch
import torchvision.transforms as T
from pathlib import Path
from ultralytics import YOLO
import cv2
from PIL import Image

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--images', type=str, required=True)
    parser.add_argument('--repo', type=str, default=None)
    parser.add_argument('--out', type=str, default=None)
    return parser.parse_args()

def crop_img(img_rgb, xywh, margin=0.12):
    x, y, w, h = [int(round(v)) for v in xywh]
    cx, cy = x + w/2, y + h/2
    new_w = w * (1 + 2*margin)
    new_h = h * (1 + 2*margin)
    x1 = max(0, int(round(cx - new_w/2)))
    y1 = max(0, int(round(cy - new_h/2)))
    x2 = min(img_rgb.shape[1], int(round(cx + new_w/2)))
    y2 = min(img_rgb.shape[0], int(round(cy + new_h/2)))
    return img_rgb[y1:y2, x1:x2]

def augment_positives(anchor_crop, img_rgb, xywh):
    augments = []
    augments.append(cv2.convertScaleAbs(anchor_crop, alpha=1.3))
    augments.append(cv2.convertScaleAbs(anchor_crop, alpha=0.7))
    h, w = anchor_crop.shape[:2]
    M = cv2.getRotationMatrix2D((w/2, h/2), 12, 1.0)
    augments.append(cv2.warpAffine(anchor_crop, M, (w, h), borderMode=cv2.BORDER_REPLICATE))
    M = cv2.getRotationMatrix2D((w/2, h/2), -12, 1.0)
    augments.append(cv2.warpAffine(anchor_crop, M, (w, h), borderMode=cv2.BORDER_REPLICATE))
    augments.append(cv2.flip(anchor_crop, 1))
    augments.append(crop_img(img_rgb, xywh, margin=0.04))
    return augments

def build_pairs(gt, images_dir):
    targets = gt['targets']
    images = [img for img in gt['images'] if not img.get('negative', False)]
    
    loaded_imgs = {}
    for img in images:
        path = images_dir / img['file']
        bgr = cv2.imread(str(path))
        if bgr is None:
            continue
        loaded_imgs[img['file']] = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        
    class_img_boxes = {c: {} for c in targets}
    for img in images:
        fname = img['file']
        for box in img.get('boxes', []):
            lbl = box['label']
            if lbl in class_img_boxes:
                if fname not in class_img_boxes[lbl]:
                    class_img_boxes[lbl][fname] = []
                class_img_boxes[lbl][fname].append(box)
                
    pairs = []
    for c in targets:
        img_boxes = class_img_boxes[c]
        if len(img_boxes) < 2:
            continue
            
        img_max_areas = []
        for fname, boxes in img_boxes.items():
            max_area = max(b['xywh'][2]*b['xywh'][3] for b in boxes)
            max_box = max(boxes, key=lambda b: b['xywh'][2]*b['xywh'][3])
            img_max_areas.append((fname, max_area, max_box))
            
        img_max_areas.sort(key=lambda x: x[1], reverse=True)
        if len(img_max_areas) < 2:
            continue
            
        fname_A, _, box_A = img_max_areas[0]
        fname_B, _, box_B = img_max_areas[1]
        
        img_A = loaded_imgs[fname_A]
        img_B = loaded_imgs[fname_B]
        
        anchor_crop = crop_img(img_A, box_A['xywh'], margin=0.12)
        positives = augment_positives(anchor_crop, img_A, box_A['xywh'])
        hard_neg = crop_img(img_B, box_B['xywh'], margin=0.12)
        
        easy_negs = []
        for oc in targets:
            if oc == c: continue
            oc_boxes = class_img_boxes[oc]
            best_box = None
            best_area = -1
            best_fname = None
            for fname, boxes in oc_boxes.items():
                for b in boxes:
                    area = b['xywh'][2]*b['xywh'][3]
                    if area > best_area:
                        best_area = area
                        best_box = b
                        best_fname = fname
            if best_box is not None:
                easy_negs.append(crop_img(loaded_imgs[best_fname], best_box['xywh'], margin=0.12))
                
        pairs.append({
            'class': c,
            'anchor': anchor_crop,
            'positives': positives,
            'hard_neg': hard_neg,
            'easy_negs': easy_negs
        })
    return pairs

def compute_metrics(pos_sims, hard_neg_sims, all_neg_sims):
    def auc_mann_whitney(positives, negatives):
        if not positives or not negatives: return 0.5
        u = sum(1 for p in positives for n in negatives if p > n) + \
            0.5 * sum(1 for p in positives for n in negatives if p == n)
        return u / (len(positives) * len(negatives))
        
    auc_all = auc_mann_whitney(pos_sims, all_neg_sims)
    auc_hard = auc_mann_whitney(pos_sims, hard_neg_sims)
    
    pos_sorted = sorted(pos_sims)
    idx = int(np.floor(0.05 * len(pos_sorted)))
    thresh = pos_sorted[idx]
    
    fpr_hard = sum(1 for n in hard_neg_sims if n >= thresh) / max(len(hard_neg_sims), 1)
    fpr_all = sum(1 for n in all_neg_sims if n >= thresh) / max(len(all_neg_sims), 1)
    
    mean_pos = np.mean(pos_sims) if pos_sims else 0.0
    mean_hard = np.mean(hard_neg_sims) if hard_neg_sims else 0.0
    gap = mean_pos - mean_hard
    
    # float() everywhere: numpy scalars are not JSON-serializable
    return {
        'auc_all': float(auc_all),
        'auc_hard': float(auc_hard),
        'fpr_hard_at_95_tpr': float(fpr_hard),
        'fpr_all_at_95_tpr': float(fpr_all),
        'mean_pos_sim': float(mean_pos),
        'mean_hard_neg_sim': float(mean_hard),
        'gap': float(gap)
    }

def run_clip(pairs, repo):
    model_path = str(repo / 'data' / 'findit' / 'yolov8l-worldv2.pt')
    model = YOLO(model_path)
    if torch.cuda.is_available():
        model.to('cuda')
    model.set_classes(['object'])
    clip = model.model.clip_model
    
    pos_sims, hard_neg_sims, all_neg_sims = [], [], []
    total_time = 0.0
    n_crops = 0
    
    for p in pairs:
        pil_anchor = Image.fromarray(p['anchor'])
        t0 = time.time()
        vec_anchor = clip.encode_image(pil_anchor)[0].cpu().numpy()
        total_time += time.time() - t0
        n_crops += 1
        
        for pos in p['positives']:
            pil_pos = Image.fromarray(pos)
            t0 = time.time()
            vec_pos = clip.encode_image(pil_pos)[0].cpu().numpy()
            total_time += time.time() - t0
            n_crops += 1
            pos_sims.append(np.dot(vec_anchor, vec_pos))
            
        pil_hard = Image.fromarray(p['hard_neg'])
        t0 = time.time()
        vec_hard = clip.encode_image(pil_hard)[0].cpu().numpy()
        total_time += time.time() - t0
        n_crops += 1
        sim_hard = np.dot(vec_anchor, vec_hard)
        hard_neg_sims.append(sim_hard)
        all_neg_sims.append(sim_hard)
        
        for eneg in p['easy_negs']:
            pil_eneg = Image.fromarray(eneg)
            t0 = time.time()
            vec_eneg = clip.encode_image(pil_eneg)[0].cpu().numpy()
            total_time += time.time() - t0
            n_crops += 1
            all_neg_sims.append(np.dot(vec_anchor, vec_eneg))
            
    metrics = compute_metrics(pos_sims, hard_neg_sims, all_neg_sims)
    metrics['mean_embed_time_ms'] = (total_time / max(n_crops, 1)) * 1000.0
    return metrics

def run_dinov2(pairs):
    dino = torch.hub.load('facebookresearch/dinov2', 'dinov2_vits14').to('cuda').eval()
    transform = T.Compose([
        T.Resize(256),
        T.CenterCrop(224),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    
    pos_sims, hard_neg_sims, all_neg_sims = [], [], []
    total_time = 0.0
    n_crops = 0
    
    def embed_batch(crops):
        nonlocal total_time, n_crops
        if not crops: return []
        tensors = [transform(Image.fromarray(c)) for c in crops]
        batch = torch.stack(tensors).to('cuda')
        t0 = time.time()
        with torch.no_grad():
            out = dino(batch)
        total_time += time.time() - t0
        n_crops += len(crops)
        
        if isinstance(out, (list, tuple)):
            feats = out[0]
        elif isinstance(out, dict):
            feats = out.get('x_norm_clstoken', out['x'])
        else:
            feats = out
            
        norms = torch.norm(feats, dim=1, keepdim=True)
        feats = feats / norms
        return feats.cpu().numpy()
        
    for p in pairs:
        all_crops = [p['anchor']] + p['positives'] + [p['hard_neg']] + p['easy_negs']
        vecs = embed_batch(all_crops)
        
        vec_anchor = vecs[0]
        pos_vecs = vecs[1:1+len(p['positives'])]
        hard_vec = vecs[1+len(p['positives'])]
        easy_vecs = vecs[2+len(p['positives']):]
        
        for pv in pos_vecs:
            pos_sims.append(np.dot(vec_anchor, pv))
            
        sim_hard = np.dot(vec_anchor, hard_vec)
        hard_neg_sims.append(sim_hard)
        all_neg_sims.append(sim_hard)
        
        for ev in easy_vecs:
            all_neg_sims.append(np.dot(vec_anchor, ev))
            
    metrics = compute_metrics(pos_sims, hard_neg_sims, all_neg_sims)
    metrics['mean_embed_time_ms'] = (total_time / max(n_crops, 1)) * 1000.0
    return metrics

def main():
    args = parse_args()
    images_dir = Path(args.images)
    repo_dir = Path(args.repo) if args.repo else Path(__file__).resolve().parent.parent
    out_file = Path(args.out) if args.out else images_dir / 'embed_results.json'
    
    with open(images_dir / 'ground_truth.json') as f:
        gt = json.load(f)
        
    pairs = build_pairs(gt, images_dir)
    print(f"Constructed {len(pairs)} class pairs.")
    
    clip_metrics = run_clip(pairs, repo_dir)
    dino_metrics = run_dinov2(pairs)
    
    results = {
        'clip': clip_metrics,
        'dinov2': dino_metrics,
        'n_classes': len(pairs),
        'n_pos_pairs': sum(len(p['positives']) for p in pairs),
        'n_neg_pairs': sum(1 + len(p['easy_negs']) for p in pairs)
    }
    
    with open(out_file, 'w') as f:
        json.dump(results, f, indent=2)
        
    print("\n{:<20} | {:>10} | {:>10}".format("Metric", "CLIP", "DINOv2"))
    print("-" * 50)
    rows = [
        ("AUC (all neg)", f"{clip_metrics['auc_all']:.4f}", f"{dino_metrics['auc_all']:.4f}"),
        ("AUC (hard neg)", f"{clip_metrics['auc_hard']:.4f}", f"{dino_metrics['auc_hard']:.4f}"),
        ("FPR hard @95 TPR", f"{clip_metrics['fpr_hard_at_95_tpr']:.4f}", f"{dino_metrics['fpr_hard_at_95_tpr']:.4f}"),
        ("FPR all @95 TPR", f"{clip_metrics['fpr_all_at_95_tpr']:.4f}", f"{dino_metrics['fpr_all_at_95_tpr']:.4f}"),
        ("Mean Pos Sim", f"{clip_metrics['mean_pos_sim']:.4f}", f"{dino_metrics['mean_pos_sim']:.4f}"),
        ("Mean Hard Neg Sim", f"{clip_metrics['mean_hard_neg_sim']:.4f}", f"{dino_metrics['mean_hard_neg_sim']:.4f}"),
        ("GAP (Pos - Hard)", f"{clip_metrics['gap']:.4f}", f"{dino_metrics['gap']:.4f}"),
        ("Embed Time (ms/crop)", f"{clip_metrics['mean_embed_time_ms']:.2f}", f"{dino_metrics['mean_embed_time_ms']:.2f}")
    ]
    for r in rows:
        print(f"{r[0]:<20} | {r[1]:>10} | {r[2]:>10}")
        
    winner = "CLIP" if clip_metrics['auc_hard'] > dino_metrics['auc_hard'] else "DINOv2"
    best_auc = max(clip_metrics['auc_hard'], dino_metrics['auc_hard'])
    worst_auc = min(clip_metrics['auc_hard'], dino_metrics['auc_hard'])
    print(f"\nVERDICT: {winner} separates instances better (hard-AUC {best_auc:.3f} vs {worst_auc:.3f})")

if __name__ == '__main__':
    main()
