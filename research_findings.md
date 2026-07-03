# FindIt research: detection, instance recognition, prior art
Date: 2026-07-03. Scope: self-hosted, single RTX 5090, Python/ultralytics/torch, no cloud APIs.

---

## 1. DETECTION — what to run instead of (or alongside) YOLO-World v2

### 1.1 Top recommendation: YOLOE (and YOLOE-26) — drop-in upgrade inside ultralytics

- **What it is:** "YOLOE: Real-Time Seeing Anything" (Tsinghua THU-MIG, March 2025) — open-vocabulary detection **and segmentation** with three prompt modes: text prompts (RepRTA head), **visual prompts** (SAVPE encoder — give it an image crop of the object instead of a word), and prompt-free mode (LRPC, built-in ~1200+ class vocabulary). Paper: https://arxiv.org/abs/2503.07465 (linked from docs); repo: https://github.com/THU-MIG/yoloe ; ultralytics docs: https://docs.ultralytics.com/models/yoloe/
- **Verified downloadable:** checkpoints in the THU-MIG repo (YOLOE-v8-S/M/L, YOLOE-11-S/M/L) and supported directly in the `ultralytics` package (`YOLOE("yoloe-11l-seg.pt")`, `model.set_classes([...])`). The docs also list a **YOLOE-26** family (N/S/M/L/X) built on YOLO26 (arXiv: https://arxiv.org/html/2606.03748v1, docs: https://docs.ultralytics.com/models/yolo26), with YOLOE26-L at 36.8 LVIS AP.
- **Accuracy vs. what FindIt runs now:** YOLOE-v8-L = 35.9 AP on LVIS minival at 102.5 FPS on a **T4** (repo table) vs YOLO-World v2 comparable size; ultralytics/paper claim **+3.5 AP over YOLO-Worldv2 with 1.4x faster inference and 3x less training cost** (https://docs.ultralytics.com/models/yoloe/). On a 5090 this is comfortably <10 ms/frame at 640 — far inside the 100 ms budget, leaving headroom for the tactics in §1.4.
- **Why it matters for FindIt specifically:** the **visual-prompt mode is the killer feature** — you can prompt the detector with crops of the user's *actual* remote/keys from enrollment instead of the word "remote". This directly attacks the weak classes (remote, scissors) where text-prompt recall is poor, and partially merges the detection and instance-recognition stages.
- **License:** the THU-MIG repo and ultralytics are both **AGPL-3.0** (https://github.com/THU-MIG/yoloe — same as YOLO-World-in-ultralytics today, so no change for a self-hosted hobby app).

### 1.2 Strong second candidate: LLMDet (CVPR 2025 highlight) — accuracy ceiling, HF-native

- Repo: https://github.com/iSEE-Laboratory/LLMDet ; paper: https://openaccess.thecvf.com/content/CVPR2025/html/Fu_LLMDet_Learning_Strong_Open-Vocabulary_Object_Detectors_under_the_Supervision_of_CVPR_2025_paper.html
- Open-vocabulary detector trained with LLM caption supervision (GroundingCap-1M). **Merged into Hugging Face `transformers` >= 4.55**, checkpoints on HF released — verified downloadable, easy to wrap without ultralytics.
- Trade-off: Swin/Grounding-DINO-style architecture, so heavier than YOLOE — best used as an **offline / second-pass verifier** (re-check frames where YOLOE was uncertain, or scan recorded "last seen" snapshots), not the per-frame live path. Worth benchmarking on the 5090: if it holds <100 ms it could even be the primary.

### 1.3 Other options, and what to skip

| Model | Verdict for FindIt | Evidence |
|---|---|---|
| **MM-Grounding-DINO** (MMDetection) | Fully open re-implementation of Grounding DINO with public weights and training pipeline; good accuracy, but MMDetection dependency stack is heavy and it's not real-time at large sizes. Reasonable second-pass alternative to LLMDet. | https://arxiv.org/pdf/2405.10300 (comparisons), MMDetection project referenced at https://www.emergentmind.com/topics/grounding-dino |
| **Grounding DINO 1.5 / 1.5 Edge, T-Rex2, DINO-X** (IDEA Research) | **Skip: API-only.** The "repo" is an API client (https://github.com/IDEA-Research/Grounding-DINO-1.5-API); weights are not self-hostable. Edge numbers (36.2 AP LVIS @ 75 FPS TensorRT, https://arxiv.org/abs/2405.10300) are nice but irrelevant without weights. |
| **OmDet-Turbo** | Real-time OVD in `transformers` >= 4.45 (https://huggingface.co/docs/transformers/model_doc/omdet-turbo), 100 FPS on A100 (https://arxiv.org/abs/2403.06892). Viable, but no visual-prompt mode and no clear win over YOLOE; A/B only if YOLOE disappoints. |
| **OWLv2** | High recall on rare classes, Apache-2.0, but slow (ViT encoder per frame) — only as an offline batch verifier; LLMDet is newer and stronger. (Comparative context: https://arxiv.org/html/2503.16538) |
| **YOLO-UniOW / Mamba-YOLO-World** | Research successors of YOLO-World on the efficiency frontier (https://www.emergentmind.com/topics/yolo-world); less ecosystem support than YOLOE — not worth the integration cost. |

### 1.4 Fixing the ~70% recall on small household objects (orthogonal to model swap)

- **Raise inference resolution.** Phone JPEGs downscaled to 640 destroy remotes/scissors at room distance. On a 5090, YOLOE-11-S/M at `imgsz=1280` will still clear 10 FPS. Cheapest single win.
- **Tiled inference (SAHI)** for the "scan the room slowly" mode: slicing-aided hyper inference is the standard recipe for small-object recall — https://github.com/obss/sahi (works with ultralytics models). Use it in a high-recall sweep mode, not the live preview.
- **Prompt engineering the vocabulary:** OVD recall on LVIS-style rare classes is prompt-sensitive; expand each target into several phrasings ("remote", "tv remote control", "black remote control") and take the max — YOLO-World/YOLOE embed each class name independently, so extra synonyms are nearly free (`set_classes`, https://docs.ultralytics.com/models/yoloe/).
- **Use YOLOE visual prompts from enrollment crops** for the user's known items (see §1.1) — bypasses the text-embedding bottleneck for the weak classes entirely.

---

## 2. INSTANCE RECOGNITION — "my keys", not "keys"

### 2.1 Embedding backbone: move DINOv2-small → DINOv3 ViT-S+/16 (with a license caveat)

- **DINOv3 exists and is downloadable.** Meta, Aug 2025. Repo: https://github.com/facebookresearch/dinov3 ; paper: https://arxiv.org/pdf/2508.10104. Sizes: ViT-S/16 **21M**, ViT-S+/16 **29M**, ViT-B/16 86M, ViT-L/16 300M, ViT-H+/16 840M, ViT-7B 6.7B, plus ConvNeXt distills (29M–198M). Available via `torch.hub.load('facebookresearch/dinov3', 'dinov3_vits16', ...)` and on HF (`transformers` >= 4.56, e.g. `facebook/dinov3-vits16-pretrain-lvd1689m`).
- **Gains over DINOv2:** trained on ~1.7B images (vs 142M) with a 7B teacher + Gram-anchoring loss; **dense/patch features improve most** (+6 mIoU ADE20K; 88.4% vs 87.3% linear ImageNet) — dense-feature quality is exactly what instance matching of small crops leans on. Sources: https://github.com/facebookresearch/dinov3, https://www.deeplearning.ai/the-batch/metas-dinov3-gets-an-updated-loss-term-and-improved-vision-performance, https://www.lightly.ai/blog/dinov3.
- **License caveat:** DINOv3 is under a **custom "DINOv3 License"** — commercial use allowed but with redistribution terms and a "Built with DINOv3" attribution requirement; DINOv2 remains plain Apache-2.0. For a personal self-hosted app this is a non-issue; if FindIt ever ships, note it. Sources: https://ai.meta.com/resources/models-and-libraries/dinov3-license/, https://github.com/facebookresearch/dinov3/issues/31, https://the-decoder.com/meta-makes-its-state-of-the-art-dinov3-image-analysis-model-available-for-commercial-projects/.
- **Concrete swap:** DINOv2-small (21M) → DINOv3 ViT-S+/16 (29M): near-identical latency class on a 5090, better patch features. Keep an eval set of enrolled-object crops and verify retrieval accuracy actually improves before committing.

### 2.2 The instance-matching recipe the 2025 SOTA uses (IDOW, CVPR 2025)

"Solving Instance Detection from an Open-World Perspective" — https://arxiv.org/abs/2503.00359, code: https://github.com/shenqq377/IDOW, CVPR 2025. This is the closest published problem statement to FindIt's ("localize *specific* object instances in novel scenes from a handful of reference photos"). Its pipeline, and what to copy:

1. **Open-world proposals first, match second** — a class-agnostic/OVD detector proposes boxes (high recall), then a foundation-model embedding matches proposals to enrolled references. (FindIt already has this shape: YOLO-World → DINOv2.)
2. **Light metric-learning adaptation of the frozen backbone** — foundation features are not optimized for instance-level matching; a small fine-tune with contrastive/metric loss on open-world data gave **>10 AP** over prior work on the InsDet benchmarks (https://insdet.github.io/). A cheap per-deployment variant: keep DINOv3 frozen and train a small projection head / per-object linear probe on enrollment crops — minutes on a 5090.
3. **Distractor negatives** — sample random other-object crops as negatives so "my keys" beats "visually similar keys". FindIt can mine negatives from its own video stream (all non-enrolled detections).
4. **Novel-view synthesis to enrich references** — augment the enrollment set with synthesized/rotated views; multi-view references materially improve matching.

Related supporting work:
- **OBoI — "Object-conditioned Bag of Instances"** (ICASSP 2024, https://arxiv.org/pdf/2404.01397): extends a YOLOv8-class detector with multi-order feature statistics for few-shot instance recognition, **no backprop needed at personalization time** — 77.1% accuracy over 18 personal instances (+12% rel. over SOTA). Good template if you want enrollment to stay training-free.
- **Swiss DINO** (IROS 2024, https://arxiv.org/abs/2407.07541): one-shot personal object search with frozen DINOv2, on-device; key trick is **open-set classification of image regions before mask generation to suppress false positives** — 42–55% identification improvement over lightweight baselines on cluttered vs simple scenes. Directly relevant: FindIt's worst failure mode will be confident wrong matches; add an explicit "none of my objects" rejection threshold calibrated on distractor negatives.
- **Cross-Architecture Auxiliary Feature Space Translation** (https://arxiv.org/pdf/2407.01193): efficient few-shot personalized detection by translating detector features into a foundation-model feature space — an option to skip running a second backbone per crop.

### 2.3 Enrollment & test-time practice (from the accessibility literature — battle-tested with real users)

- **ORBIT dataset/benchmark** (Microsoft + City Univ. London): 4,733 videos of 588 personal objects filmed by 97 blind/low-vision users — the canonical few-shot *personal* object recognition benchmark, with exactly FindIt's noise profile (blur, bad framing, hand occlusion). Paper: https://arxiv.org/pdf/2104.03841, repo: https://github.com/microsoft/ORBIT-Dataset, project: https://orbit.city.ac.uk/. **Use ORBIT as the offline eval set for the instance-recognition stage.**
- **Enrollment protocol that works:** Microsoft's Find My Things asks for **four short videos per object, varied angles and varied backgrounds, with real-time framing feedback**, then personalizes on-device in seconds (https://www.microsoft.com/en-us/research/story/find-my-things/). Copy this: multi-view + multi-background enrollment videos beat a handful of stills, and live "object left the frame" feedback keeps enrollment data clean.
- **Test-time:** average embeddings over a short burst of frames (temporal TTA) rather than single-frame matching; combine with the rejection threshold from §2.2. This is consistent with ORBIT's finding that high-variation clips are the hard part of real-world few-shot recognition.

---

## 3. PRIOR ART — camera-based "where did I leave it"

### 3.1 Lighthouse AI (2017–2018, discontinued) — the closest dead product
- $299 depth-camera home assistant with NLP video search: "Did the kids get home?", face/pet recognition, 3D sensing from self-driving tech. Shut down Dec 2018 — crowded camera market, price too high. TechCrunch shutdown: https://techcrunch.com/2018/12/18/smart-security-camera-maker-lighthouse-ai-shuts-down/, review: https://www.techhive.com/article/583364/lighthouse-review-a-smarter-security-camera.html.
- **Lesson:** the *query* UX (ask in natural language, get a video moment back) was loved by reviewers; the failure was hardware economics, not the concept. FindIt's "usually in the kitchen drawer" answer with a last-seen snapshot is the same UX pattern without selling hardware — the query-to-clip interaction is validated.

### 3.2 Microsoft Find My Things / Seeing AI (live, 2024–) — the closest live product
- Teachable personal-item finder for blind/low-vision users, shipped in the **World channel of Seeing AI**; CHI 2024 Best Paper; Fast Company 2024 design awards. https://www.microsoft.com/en-us/research/story/find-my-things/
- **What it does well:** honest scope — it *guides you to within arm's reach* of an item that is in view, it does not claim to know where things are when out of view; enrollment is user-owned ("teach it your objects"); multimodal guidance cues (audio + vibration). **Gap FindIt fills:** no persistent location memory — Find My Things only works when you're actively scanning. FindIt's per-item location priors ("usually in the kitchen drawer") is the differentiator.

### 3.3 Pixie Points (2017, effectively dead) — AR + tags hybrid
- Bluetooth/UWB-ish tags plus an AR "scan the room like a panorama, see Pixie Dust where the item is" interface; needed a tag on the phone *and* the item, $50/2-pack. Reviews: https://www.tomsguide.com/us/pixie-point,review-5006.html, https://techcrunch.com/2017/01/25/pixie-hands-on/, https://www.gearbrain.com/pixie-tracker-review-lost-found-2212636694.html.
- **Lesson:** the AR-overlay "X marks the spot" reveal was the most praised part; the tag-everything requirement killed it (cost + can't tag scissors). Camera-only FindIt removes that constraint; consider stealing the AR-arrow/heatmap reveal for the phone UI.

### 3.4 Amazon Astro (2021–, invite-only limbo)
- $1,600 home robot with cameras + Alexa; Astro for Business killed Sept 2024 (https://www.geekwire.com/2024/amazon-discontinues-astro-for-business-robot-security-guard-to-focus-on-astro-home-robot/); consumer unit quietly vanished from the store (Jan 2026 reports), still Day1 Editions invite-only (https://en.wikipedia.org/wiki/Amazon_Astro).
- **Lesson:** a roaming camera that patrols and remembers the house is the maximal version of location memory, and even Amazon couldn't find product-market fit at that price. A phone-plus-server that only looks when asked is the right cost point.

### 3.5 Big-tech assistant memory features (the expectation setters)
- **Google Project Astra** (I/O 2024 demo): answered "where did I leave my glasses?" from ambient video memory — the demo that defined the UX in the public imagination. https://9to5google.com/2024/12/17/metas-ray-ban-smart-glasses-adding-live-ai-that-works-like-googles-project-astra/
- **Ray-Ban Meta "Live AI"** (Dec 2024 rollout, expanding since): continuous video-context AI; Meta's stated direction includes recalling where you left car keys from personal recordings. https://9to5google.com/2024/12/17/metas-ray-ban-smart-glasses-adding-live-ai-that-works-like-googles-project-astra/, release notes: https://www.meta.com/help/ai-glasses/1809764829519902/
- **Lesson for location-memory UX:** users now expect the *answer* form ("your glasses are on the desk by the red mug"), i.e., location expressed relative to landmark objects, with a snapshot as proof. FindIt should render memory as: last-seen photo + timestamp + landmark phrase ("on the coffee table, next to the controller"), and degrade honestly to the prior ("usually in the kitchen drawer — last confirmed Tuesday 9pm").
- **Reliability expectations (cross-cutting):** every surviving product in this space (AirTag/Tile world included) wins by being *calibrated*, not omniscient — say when confidence is low, show the evidence frame, and never assert a location from a stale sighting without a timestamp. Find My Things' "guides you to within arm's reach" phrasing and its citizen-design process (https://www.microsoft.com/en-us/research/story/find-my-things/) are the model for honest scoping.

---

## Priority actions for FindIt

1. Swap YOLO-World v2 → **YOLOE-11-M/L (or YOLOE-26) in ultralytics**; enable **visual prompts** from enrollment crops for weak classes. (§1.1)
2. Add `imgsz=1280` + **SAHI tiling** in a deliberate "sweep mode" for small-object recall; expand class names into synonym sets. (§1.4)
3. Swap DINOv2-small → **DINOv3 ViT-S+/16**; add distractor-negative mining, a per-object linear probe or OBoI-style stats, and a calibrated "not my object" rejection threshold. Eval on **ORBIT** and **InsDet**. (§2)
4. Copy the **Find My Things enrollment protocol**: 4 short multi-angle/multi-background videos with live framing feedback. (§2.3)
5. UX: last-seen snapshot + timestamp + landmark phrase, with explicit confidence degradation to the location prior. Steal Pixie's AR reveal; keep Lighthouse's natural-language query pattern. (§3)
