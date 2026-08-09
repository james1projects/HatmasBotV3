"""
tools/eval_struct_veto.py
=========================
Measure the structural-veto NCC distributions that back
``core.god_matcher.STRUCT_MIN_NCC``.

The veto's job: after the histogram ranking accepts a winner, reject
crops that share the winner's palette but not its shape (the god-select
lobby false-positive).  To pick the threshold we need the NCC score
distribution for:

  GENUINE  — real portrait crops vs their own god's fingerprints
             (Portrait_Source references, the _capture_audit frame,
             and any gameplay frames in the debug dumps whose histogram
             winner is confident)
  JUNK     — crops that could plausibly reach the histogram margin rule
             without being a portrait:
               * off-portrait crops of real frames (portrait region
                 shifted into the game world), scored vs whichever god
                 wins their histogram — this synthesizes the lobby
                 failure mode exactly
               * cross-god art (every base icon vs every other god's
                 templates) as a structural-similarity ceiling check

Run:
  python tools/eval_struct_veto.py

Prints per-sample lines plus summary percentiles for each bucket.
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import cv2  # noqa: E402

from core.god_matcher import (  # noqa: E402
    GodMatcher, PORTRAIT_REGION, MATCH_SIZE,
    MIN_CONFIDENCE, MIN_CONFIDENCE_MARGIN, MARGIN_GAP,
)


def crop_to_gray_flat(img: Image.Image, region) -> np.ndarray:
    portrait = img.crop(region)
    cv = cv2.cvtColor(np.array(portrait.convert("RGB")), cv2.COLOR_RGB2BGR)
    cv = cv2.resize(cv, MATCH_SIZE, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(cv, cv2.COLOR_BGR2GRAY).astype(np.float32).ravel()


def hist_winner(matcher: GodMatcher, img: Image.Image, region):
    """Re-run the histogram ranking on an arbitrary crop region and
    return (name, best, second) without the veto interfering."""
    portrait = img.crop(region)
    cv = cv2.cvtColor(np.array(portrait.convert("RGB")), cv2.COLOR_RGB2BGR)
    cv = cv2.resize(cv, MATCH_SIZE, interpolation=cv2.INTER_AREA)
    hist = matcher._compute_hist(cv)
    best_name, best, second = None, -1.0, -1.0
    for god, hl in matcher._icon_hists.items():
        s = max(cv2.compareHist(hist, h, cv2.HISTCMP_CORREL) for h in hl)
        if s > best:
            second = best
            best, best_name = s, god
        elif s > second:
            second = s
    return best_name, best, second


def hist_accepts(best, second):
    return best >= MIN_CONFIDENCE or (
        best >= MIN_CONFIDENCE_MARGIN and (best - second) >= MARGIN_GAP)


def main():
    matcher = GodMatcher(
        overlay_icons_dir=str(REPO / "assets" / "Custom God Icons"),
        reference_icons_dir=str(REPO / "assets" / "Portrait_Source"),
    )
    if not matcher.load_icons():
        print("No icons loaded — aborting")
        return

    genuine, junk = [], []

    # --- 1) Full frames from debug dumps + capture audit ---------------
    frame_dirs = [
        REPO / "assets" / "Portrait_Source" / "_capture_audit",
        REPO / "data" / "captured_frames",
    ]
    dbg = REPO / "data" / "killdetect_debug"
    if dbg.is_dir():
        frame_dirs += [d for d in dbg.iterdir() if d.is_dir()]
        frame_dirs.append(dbg)
    snaps = REPO / "data" / "detector_snapshots"
    if snaps.is_dir():
        frame_dirs += [d for d in snaps.iterdir() if d.is_dir()]

    x1, y1, x2, y2 = PORTRAIT_REGION
    # Off-portrait junk region: same size, shifted into the game world.
    junk_region = (x1 - 250, y1 - 300, x2 - 250, y2 - 300)

    n_frames = 0
    for d in frame_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.png"))[:400]:
            try:
                img = Image.open(p).convert("RGB")
            except Exception:
                continue
            if img.size != (1920, 1080):
                continue
            n_frames += 1

            # Genuine: portrait region, only when histogram is confident.
            # Flat crops (black frame / empty portrait area) are hist
            # false-accepts, not genuine portraits — bucket them as junk.
            name, best, second = hist_winner(matcher, img, PORTRAIT_REGION)
            if name and hist_accepts(best, second):
                gf = crop_to_gray_flat(img, PORTRAIT_REGION)
                ncc = matcher._struct_score(name, gf)
                if float(np.std(gf)) < 5.0:
                    junk.append((ncc, best, name, p.name, "flat-portrait"))
                else:
                    genuine.append((ncc, best, name, p.name, "portrait"))

            # Junk: off-portrait crop scored vs ITS OWN hist winner
            jname, jbest, jsecond = hist_winner(matcher, img, junk_region)
            if jname:
                gf = crop_to_gray_flat(img, junk_region)
                ncc = matcher._struct_score(jname, gf)
                junk.append((
                    ncc, jbest, jname, p.name,
                    "off-crop" + ("(ACCEPTS)" if hist_accepts(jbest, jsecond) else ""),
                ))

    # --- 2) Reference crops vs own base art (cross-pipeline genuine) ---
    ref_dir = REPO / "assets" / "Portrait_Source"
    for p in sorted(ref_dir.glob("*.png")):
        god = matcher._slug_to_name(p.stem)
        if god not in matcher._icon_templates:
            continue
        img_raw = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
        if img_raw is None:
            continue
        bgr, _ = matcher._split_bgr_alpha(img_raw)
        gf = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32).ravel()
        # Score vs base-library templates ONLY (drop the reference's own
        # near-1.0 self-match so we see the cross-pipeline number).
        base_tpls = [
            t for t, s in zip(matcher._icon_templates[god],
                              matcher._icon_sources[god])
            if s == "base" and t is not None
        ]
        for idx_v in base_tpls:
            i, v = idx_v
            c = gf[i]
            c = c - c.mean()
            n = float(np.linalg.norm(c))
            if n > 1e-6:
                genuine.append(
                    (float(np.dot(c / n, v)), 1.0, god, p.name, "ref-vs-base"))

    # --- 3) Cross-god art (structural similarity between DIFFERENT gods)
    gods = sorted(matcher._icon_templates.keys())
    cross = []
    for ga in gods:
        tpl_a = next((t for t, s in zip(matcher._icon_templates[ga],
                                        matcher._icon_sources[ga])
                      if s == "base" and t is not None), None)
        if tpl_a is None:
            continue
        # Rebuild god A's base art grayscale from its template vector is
        # not possible (masked); instead reload the icon file.
        for gb in gods:
            if gb == ga:
                continue
            cross_tpls = matcher._icon_templates[gb]
            ia, va = tpl_a
            # NCC between A's template vector and B's templates over the
            # intersection isn't well-defined; approximate by scoring on
            # shared circular-mask indices where both are full-circle.
            for tb in cross_tpls:
                if tb is None:
                    continue
                ib, vb = tb
                if ia.shape == ib.shape and np.array_equal(ia, ib):
                    cross.append(float(np.dot(va, vb)))

    def summarize(label, rows):
        if not rows:
            print(f"{label}: no samples")
            return
        arr = np.array([r[0] for r in rows])
        print(f"\n{label}  (n={len(arr)})")
        print(f"  min={arr.min():.3f}  p5={np.percentile(arr, 5):.3f}  "
              f"p25={np.percentile(arr, 25):.3f}  med={np.median(arr):.3f}  "
              f"p75={np.percentile(arr, 75):.3f}  max={arr.max():.3f}")

    print(f"\nScanned {n_frames} full frames")
    print("\n=== GENUINE samples ===")
    for ncc, hist, name, fname, kind in sorted(genuine):
        print(f"  ncc={ncc:+.3f} hist={hist:.3f} {name:<16} {kind:<12} {fname}")
    print("\n=== JUNK samples (off-portrait crops) ===")
    for ncc, hist, name, fname, kind in sorted(junk, reverse=True)[:40]:
        print(f"  ncc={ncc:+.3f} hist={hist:.3f} {name:<16} {kind:<18} {fname}")

    summarize("GENUINE", genuine)
    summarize("JUNK(off-crop)", junk)
    if cross:
        arr = np.array(cross)
        print(f"\nCROSS-GOD art NCC  (n={len(arr)})")
        print(f"  p50={np.median(arr):.3f}  p95={np.percentile(arr, 95):.3f}  "
              f"p99={np.percentile(arr, 99):.3f}  max={arr.max():.3f}")


if __name__ == "__main__":
    main()
