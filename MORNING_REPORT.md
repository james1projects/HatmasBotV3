# Morning Report — FindIt overnight run, 2026-07-03

**Branch:** `worktree-overnight-2026-07-03` (worktree at `.claude/worktrees/overnight-2026-07-03`)
**Six commits, nothing merged — you merge after reading this.** All tests
green at time of writing: `test_items_store.py` 26/26, `test_findit_public.py`
23/23 (real webserver → plugin → GPU worker chain).

## TL;DR

FindIt now has everything we discussed: per-device profiles, a My Items
gallery with thumbnails, a guided ➕ add-item mode, the Found! 📍 button
with location memory ("Usually in: kitchen drawer (4×)"), temporal
smoothing that kills the flickery false FOUNDs, and — the big one — custom
items are now recognized with **DINOv2 instead of CLIP**, which cut the
"claimed someone else's keys were mine" false-positive rate in half in
benchmarks, with a similarity structure that suggests it's near-eliminated
in practice (details below).

## What was built (per commit)

1. **`8e52654` — benchmark harness + dev server.** `tools/findit_bench.py`
   spawns an isolated worker (temp items.json, port 8475) and measures
   detection recall/FP against 66 COCO images with ground truth, plus an
   instance benchmark (enroll object A, must re-find A under augmentation,
   must NOT match a different object of the same class).
   `tools/findit_devserver.py` serves the page + proxies the WS locally so
   the client can be tested in a desktop browser with a mocked camera.

2. **`4292609` — worker protocol v2.** New `plugins/findit/items_store.py`
   (GPU-free, 26 unit tests): profiles, per-view thumbnail JPEGs, location
   history with case-insensitive grouping, v1→v2 migration with `.bak`
   backup (a corrupt file is also backed up before starting fresh), atomic
   saves. Worker messages: `hello` (profile handshake), `list_items`,
   `rename`, `set_shared`, `log_location`, forget-by-item_id — all v1
   messages still work (no hello = shared profile), so nothing breaks if
   an old client connects. Malformed JSON now gets an error reply instead
   of killing the connection.

3. **`799c503` — client v2** (browser-verified end-to-end against the live
   GPU worker with a mocked `getUserMedia`): name-on-first-visit card,
   📦 items drawer (tap tile = search it; Edit mode = rename / add view /
   share toggle / location history / forget), ➕ guided add mode (captures
   4 angles with progress dots), Found! pill after 3 consecutive matched
   frames (2-miss hysteresis), location sheet with **dropdown of past
   places ranked by frequency + "somewhere new…"**, and the "Usually in:"
   banner the moment you search a named item. Item names/places are
   HTML-escaped — with shared items, another person's item name renders on
   your phone, so that mattered.

4. **`73001e2` — integration test v2 checks** (13 → 23 checks).

5. **`ba18585` — DINOv2 swap.** See numbers below. Items are tagged with
   their embedder; legacy CLIP items would still show in the gallery but
   need re-adding (your items.json was empty, so nothing is affected).

6. **`5e3c2b8` — polish:** PWA manifest + icons (add to home screen,
   standalone app feel; routes 404 when the toggle is off, same
   invisibility contract), 🔦 flashlight toggle on cameras that support
   it, HATMASBOT.md FindIt section rewritten for v2.

## Benchmark numbers (66 COCO images, 14 household classes)

Detection (IoU 0.5, conf 0.2):

| | v1 | v2 |
|---|---|---|
| Overall recall | 66.7% | **70.4%** (synonym prompts) |
| False positives on negatives | 1 | 1 |
| Latency / frame | 13.9 ms | 13.6 ms |

Instance recognition ("MY cup, not any cup" — enroll one view, re-find
under brightness/rotation changes, reject a different same-class object):

| | CLIP @0.80 (v1) | DINOv2 @0.55 (v2) |
|---|---|---|
| Refind original | 78.6% | 85.7% |
| Refind augmented | 81.0% | 83.3% |
| **Wrong-object false positive** | **28.6%** | **14.3%** |

Why DINOv2 (from `tools/embed_bench.py`): CLIP scores a *different* object
of the same class at ~0.73 similarity — inside its own 0.80 threshold's
noise band. DINOv2 scores it at ~0.26 vs ~0.93 for the true object — a 3×
wider gap (hard-AUC 1.000 vs 0.995), and 15× faster per crop. Also note
the benchmark enrolls a **single view**; the guided add mode captures 4,
and matching takes the best view, so real-world recall should beat these
numbers. Also fixed en route: a 0.12-conf detector floor when searching
custom items (v1 could never find an item whose base class the detector
missed at your slider setting), a 0.04 match margin over the runner-up
item, and square-resize instead of center-crop for embeddings (center-crop
was cutting the edges off non-square objects).

## Test with your phone (5 minutes)

1. Merge (or test from the worktree), start the bot, flip the `findit`
   toggle on, open hatmaster.tv/FindIt. Enter your name when asked.
2. First worker start downloads DINOv2 (~90 MB, one-time — already cached
   from tonight's runs, so it should be instant).
3. ➕ → point at your keys → Capture → "keys" / "my keys" → capture 3 more
   angles. Check the 📦 drawer shows the thumbnail.
4. Search "my keys", let it find them, wait for the **Found it! 📍** pill,
   tap it, type "desk". Search again → banner should say
   "Usually in: desk (1×, just now)".
5. The real test of the DINOv2 swap: point the camera at your
   girlfriend's/mom's keys — it should box them as generic "keys" (green)
   but NOT claim them as "my keys" (yellow).
6. Share-to-home-screen for the app-like experience (PWA).
7. If matching feels too strict/loose: FINDIT_SIM_THRESHOLD in config.py
   (0.55 now; lower = more eager).

## Known limits / honest notes

- All reliability numbers are COCO stock images; **your real reference
  photos this morning are the ground truth that matters.** Instance recall
  under big viewpoint change (enrolled front, searching from the side) is
  the thing to probe.
- Detection recall (finding small/cluttered objects at all) is still the
  ceiling — YOLO-World misses ~30% of GT boxes. That's a detector-model
  question, in the research list for the 6:30 session.
- The drawer needs one connection before it has live data (cached copy
  shows instantly after the first visit).
- Voice search and auto-learning (silently adding views on confirmed
  finds) were discussed but deliberately not built — stretch goals lost to
  the DINOv2 work, which was worth more.

## Housekeeping (things I touched outside the worktree)

- **`.claude/launch.json` on main** gained a `findit-dev` entry (the
  preview tool only reads the main repo's copy). Additive; remove if
  unwanted. The worktree's committed copy has the same entry with relative
  paths.
- `data/findit/` and `.venv-findit` in the worktree: local copies/junction
  (gitignored, not in the branch). Your production `data/findit/items.json`
  was **never touched** — tests ran against isolated temp copies.
- Keepawake sentinel (pid 23236) holds the PC awake; it self-releases at
  8:30 AM at the latest, or when the 6:30 session finishes and deletes
  `keepawake.flag` in the session scratchpad.

## Morning review (done with James, ~8:15 AM)

The 6:33 cron never fired — the session it belonged to had ended, so it was
session-only (my mistake; a durable/cloud schedule would've survived). Ran
the review live instead: two background agents did an adversarial code
review and the web research, and I fixed everything actionable while James
was awake. Full writeups live alongside this file: `review_findings.md`,
`research_findings.md`.

**Review found 5 major + 11 minor. All majors fixed** (commit
`6269b55` "Harden FindIt after adversarial review"):

| # | What was wrong | Fix |
|---|---|---|
| F1 | A partial/hand-edited items.json (valid JSON, missing keys) KeyError'd in worker startup → /healthz stuck 503 for 240s → FindIt hard-down | Schema-validate on load; back up to `.corrupt.bak` and start fresh |
| F6 | **"Make private" on a shared item you didn't create transferred it to you** — silent theft | Immutable `creator` field; un-share is creator-only |
| F7 | Any device could rename/delete any *shared* item | Only the creator can mutate; others can still log locations |
| F10 | `enroll` was the one message with no try/except — a concurrent forget (or any GPU error) killed the socket | Wrapped + `add_view` tolerates a vanished id |
| F16 | First-ever start with no DINOv2 cache **and** no internet hung the worker at 503 forever | Degrade to detection-only, lazy-load embedder on first use |

Minor fixes shipped too: view cap (F5, newest 12 — bounds disk + per-frame
cost on a public endpoint), RecursionError on hostile nested JSON (F11),
and a client state leak (F17). Verified clean by the review: XSS (F13 —
esc() everywhere), generic-search behavior unchanged (F14), no PWA path
traversal (F21).

**Tests grew to cover the gaps the review named:** `test_items_store.py`
26 → **40** (creator/mine flags, the F6 steal scenario, view cap,
partial-v2 + non-dict recovery); `test_findit_public.py` 23 → **30** with a
two-connection cross-profile section that proves — through the real proxy —
that device-2 cannot see device-1's private item, cannot rename/forget/
steal device-1's shared item, but *can* still log where it found it.

Not fixed (documented, minor, your call): F8/F9 (the profile uuid is an
unauthenticated bearer token — fine for a family LAN tool; would matter if
FindIt goes truly public), F22 (icon cache-control vs the invisibility
contract — cosmetic).

## Future FindIt ideas (research)

Full cited writeup in `research_findings.md`. The three highest-leverage:

1. **YOLOE** (successor to our YOLO-World, already in ultralytics, AGPL):
   +accuracy, 1.4× faster, and — the reason it matters — **visual
   prompting**: prompt the detector with crops of your *actual* enrolled
   remote instead of the word "remote". Directly attacks our ~70% recall
   ceiling and the "detector must fire before recognition can" limitation.
   Next run's headline candidate, benchmark-gated like DINOv2 was.
2. **Free detection wins now:** run inference at 1280px (we have 14ms/frame
   of headroom on the 5090) — helps small objects (remote, scissors);
   optional SAHI tiled "sweep mode" for a slow deliberate room scan.
3. **DINOv3 ViT-S+/16** (Aug 2025) — same family as what we shipped,
   stronger dense features (what crop matching uses), near drop-in via
   torch.hub. Custom license (commercial-OK-with-attribution) vs DINOv2's
   Apache — fine for Hatmaster.tv.

Also: a CVPR 2025 paper (**IDOW**) is literally FindIt's problem statement
(few-shot personal-object re-ID in open scenes) with an adoptable recipe +
real benchmarks (ORBIT, InsDet) to replace my homemade COCO instance bench.
And the prior-art scan confirmed the **location memory is the
differentiator** — Microsoft's Find My Things and the dead Lighthouse/Pixie
apps validate the enrollment + query UX but none of them remember *where*.

Suggested next-run order: YOLOE + visual-prompt enrollment + the 1280px
bump (one run), then DINOv3 + IDOW rejection calibration (the run after).
