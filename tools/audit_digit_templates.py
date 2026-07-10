"""
Digit Template Audit
====================
Until 2026-07-10 the live detector enrolled digit crops from ANY read
that passed its plausibility checks — including the phantom reads the
delta-confirm gate now suppresses. That means data/digit_templates/
may contain garbage: mislabeled digits, VFX blobs, half-rendered
transition frames. Garbage templates lower the distance floor for
wrong digits and cause margin rejections against right ones.

This tool audits the library structurally (no video needed):

  MISFIT     nearest neighbor across the whole library belongs to a
             DIFFERENT digit — the template resembles another class
             more than its own. Mislabeled or garbage. High confidence.
  BAD_HOLES  the template's own hole count is impossible for its label
             (e.g. a "7" with an enclosed hole). Structural garbage.
  ISOLATED   unusually far from every other template of its own class
             (min own-class distance > --isolation-threshold, default
             0.28 ≈ the matcher's own MAX_MATCH_DISTANCE). Suspicious
             but can also be a legitimately rare background — review
             the contact sheet before quarantining these.

Outputs a ranked report + a contact sheet PNG (all templates grouped
by digit, flagged ones outlined) at rendered/template_audit.png.

    python tools/audit_digit_templates.py                 # report only
    python tools/audit_digit_templates.py --quarantine    # move MISFIT+BAD_HOLES
    python tools/audit_digit_templates.py --quarantine --aggressive
                                                          # also ISOLATED

Quarantined files MOVE to data/digit_templates_quarantine/ (never
deleted) — restore any of them by moving them back and restarting the
bot. The live bot picks changes up on its next restart.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from core.digit_matcher import (  # noqa: E402
    DigitMatcher, HOLES_TO_DIGITS, TEMPLATE_H, TEMPLATE_W,
)

TEMPLATE_DIR = REPO_ROOT / "data" / "digit_templates"
QUARANTINE_DIR = REPO_ROOT / "data" / "digit_templates_quarantine"
SHEET_PATH = REPO_ROOT / "rendered" / "template_audit.png"

# Digits whose legal hole counts include the template's own count.
DIGIT_LEGAL_HOLES = {}
for holes, digits in HOLES_TO_DIGITS.items():
    for d in digits:
        DIGIT_LEGAL_HOLES.setdefault(d, set()).add(holes)


def load_library() -> list[dict]:
    entries = []
    for p in sorted(TEMPLATE_DIR.glob("*.png")):
        label = p.stem.split("_")[0]
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"[warn] unreadable template skipped: {p.name}")
            continue
        if img.shape != (TEMPLATE_H, TEMPLATE_W):
            img = cv2.resize(img, (TEMPLATE_W, TEMPLATE_H),
                             interpolation=cv2.INTER_CUBIC)
        _, img = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
        entries.append({
            "path": p, "label": label, "img": img,
            "holes": DigitMatcher._count_holes(img),
        })
    return entries


def xor_dist(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sum(cv2.bitwise_xor(a, b) > 0) / (TEMPLATE_H * TEMPLATE_W))


def audit(entries: list[dict], isolation_threshold: float) -> None:
    n = len(entries)
    dists = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            d = xor_dist(entries[i]["img"], entries[j]["img"])
            dists[i][j] = dists[j][i] = d

    for i, e in enumerate(entries):
        own = [dists[i][j] for j in range(n)
               if j != i and entries[j]["label"] == e["label"]]
        other = [(dists[i][j], entries[j]["label"]) for j in range(n)
                 if entries[j]["label"] != e["label"]]
        e["min_own"] = min(own) if own else None
        if other:
            e["min_other"], e["nearest_other"] = min(other)
        else:
            e["min_other"], e["nearest_other"] = None, ""

        e["flags"] = []
        legal = DIGIT_LEGAL_HOLES.get(e["label"], set())
        if legal and e["holes"] not in legal:
            e["flags"].append("BAD_HOLES")
        if e["min_own"] is not None and e["min_other"] is not None:
            gap = e["min_own"] - e["min_other"]
            if e["min_other"] < e["min_own"] and gap > 0.01:
                # Decisively closer to another class than its own:
                # this is the impostor (mislabeled enrollment).
                e["flags"].append("MISFIT")
            elif e["min_other"] < 0.02:
                # Near-tie with another class — usually the VICTIM of
                # an impostor sitting in its cluster. Informational;
                # resolves once the impostor is quarantined.
                e["flags"].append("CONFUSED")
        if e["min_own"] is not None and e["min_own"] > isolation_threshold:
            e["flags"].append("ISOLATED")


def contact_sheet(entries: list[dict]) -> None:
    """All templates grouped by digit; flagged ones outlined."""
    per_row = 20
    cell_w, cell_h, pad = TEMPLATE_W + 8, TEMPLATE_H + 24, 6
    digits = sorted({e["label"] for e in entries})
    rows = []
    for d in digits:
        group = [e for e in entries if e["label"] == d]
        rows.append((d, group))
    width = pad * 2 + cell_w * per_row
    height = pad + sum(cell_h + 30 for _ in rows) + pad
    sheet = Image.new("RGB", (width, height), (32, 44, 57))
    draw = ImageDraw.Draw(sheet)
    y = pad
    for d, group in rows:
        draw.text((pad, y + 4), f"digit {d}  ({len(group)} templates)",
                  fill=(223, 160, 110))
        y += 24
        for i, e in enumerate(group):
            x = pad + (i % per_row) * cell_w
            tile = Image.fromarray(e["img"]).convert("RGB")
            sheet.paste(tile, (x + 4, y))
            color = None
            if "MISFIT" in e["flags"] or "BAD_HOLES" in e["flags"]:
                color = (255, 107, 115)      # red — quarantine class
            elif "ISOLATED" in e["flags"]:
                color = (255, 200, 80)       # amber — review class
            elif "CONFUSED" in e["flags"]:
                color = (125, 152, 161)      # steel — victim, informational
            if color:
                draw.rectangle((x + 2, y - 2, x + 6 + TEMPLATE_W,
                                y + 2 + TEMPLATE_H), outline=color, width=3)
            draw.text((x + 4, y + TEMPLATE_H + 2),
                      e["path"].stem, fill=(159, 180, 187))
        y += cell_h + 6
    SHEET_PATH.parent.mkdir(exist_ok=True)
    sheet.save(SHEET_PATH)
    print(f"\ncontact sheet: {SHEET_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarantine", action="store_true",
                    help="Move MISFIT + BAD_HOLES templates to quarantine")
    ap.add_argument("--aggressive", action="store_true",
                    help="With --quarantine: also move ISOLATED templates")
    ap.add_argument("--isolation-threshold", type=float, default=0.28)
    args = ap.parse_args()

    entries = load_library()
    print(f"{len(entries)} templates loaded from {TEMPLATE_DIR}")
    audit(entries, args.isolation_threshold)

    flagged = [e for e in entries if e["flags"]]
    flagged.sort(key=lambda e: (("MISFIT" in e["flags"]) * 2
                                + ("BAD_HOLES" in e["flags"]) * 2
                                + 1), reverse=True)
    if not flagged:
        print("library is clean — no flags.")
    else:
        print(f"\n{len(flagged)} flagged template(s):")
        print(f"{'file':>12}  {'flags':24} {'min_own':>8} {'min_other':>9}  nearest")
        for e in flagged:
            mo = f"{e['min_own']:.3f}" if e["min_own"] is not None else "  n/a"
            mt = f"{e['min_other']:.3f}" if e["min_other"] is not None else "  n/a"
            print(f"{e['path'].stem:>12}  {','.join(e['flags']):24} "
                  f"{mo:>8} {mt:>9}  '{e['nearest_other']}'")

    contact_sheet(entries)

    if args.quarantine and flagged:
        QUARANTINE_DIR.mkdir(exist_ok=True)
        moved = 0
        for e in flagged:
            hard = "MISFIT" in e["flags"] or "BAD_HOLES" in e["flags"]
            if hard or (args.aggressive and "ISOLATED" in e["flags"]):
                shutil.move(str(e["path"]), str(QUARANTINE_DIR / e["path"].name))
                moved += 1
        print(f"\nquarantined {moved} template(s) -> {QUARANTINE_DIR}")
        print("restart the bot to load the cleaned library; restore any "
              "file by moving it back.")


if __name__ == "__main__":
    main()
