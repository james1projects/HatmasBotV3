"""Regression runner for the KDA reader against canned fixtures.

A fixture is a pair of files under data/test_fixtures/kda/:

    <name>.png   — a full 1920x1080 game frame (live or VOD).
    <name>.json  — sidecar describing what the reader SHOULD produce.

The sidecar is human-readable and self-documenting; see
data/test_fixtures/kda/atlas_4_0_0_live_1080p.json for the canonical
example.

Run:
    python tools/test_kda_fixture.py                 # all fixtures
    python tools/test_kda_fixture.py atlas           # filter by substring
    python tools/test_kda_fixture.py --verbose       # dump per-digit margins
    python tools/test_kda_fixture.py --save-binary   # write the binarised
                                                       8x crop next to each
                                                       fixture for eyeballing

Exit code is 0 if every fixture matches, 1 otherwise — drop into CI / a
Stream Deck "are we still reading KDA?" sanity-check button.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

# Make `core` importable when running from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.kda_reader import KdaReader  # noqa: E402


FIXTURE_DIR = REPO_ROOT / "data" / "test_fixtures" / "kda"


# --- Tiny ANSI helpers (skip on non-tty Windows consoles) ------------------

_USE_COLOR = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _ok(text: str) -> str:    return _c(text, "32")
def _bad(text: str) -> str:   return _c(text, "31")
def _warn(text: str) -> str:  return _c(text, "33")
def _dim(text: str) -> str:   return _c(text, "2")


# --- Comparison ------------------------------------------------------------


def _compare(actual: dict, expected: dict, tolerance: dict) -> list[str]:
    """Return a list of failure messages; empty list means the fixture passed."""
    failures: list[str] = []

    exp_kda = tuple(expected.get("kda") or ())
    act_kda = tuple(actual.get("kda") or ()) if actual.get("kda") is not None else None

    # KDA tuple must match exactly (or both be None for an expected-failure
    # fixture).
    if exp_kda and act_kda != exp_kda:
        failures.append(f"kda mismatch: got {act_kda}, expected {exp_kda}")
    elif not exp_kda and act_kda is not None:
        failures.append(f"expected reader to fail (kda=null), but got {act_kda}")

    exp_reason = expected.get("failure_reason")
    act_reason = actual.get("failure_reason")
    if exp_reason != act_reason:
        # Treat None == None as equal; only flag actual disagreements.
        if exp_reason or act_reason:
            failures.append(
                f"failure_reason mismatch: got {act_reason!r}, expected {exp_reason!r}"
            )

    exp_groups = expected.get("groups") or []
    act_groups = actual.get("groups") or []
    if exp_groups and len(act_groups) != len(exp_groups):
        failures.append(
            f"group count mismatch: got {len(act_groups)}, expected {len(exp_groups)}"
        )
        return failures  # don't bother with per-group checks past this point

    dist_slack = float(tolerance.get("max_distance_slack", 0.05))
    margin_slack = float(tolerance.get("min_margin_slack", 0.05))

    for exp_g, act_g in zip(exp_groups, act_groups):
        label = exp_g.get("label", "?")
        if exp_g.get("concatenated") != act_g.get("concatenated"):
            failures.append(
                f"group {label}: digits mismatch "
                f"(got {act_g.get('concatenated')!r}, expected {exp_g.get('concatenated')!r})"
            )
            continue
        if exp_g.get("digit_count") is not None:
            if len(act_g.get("digits") or []) != exp_g["digit_count"]:
                failures.append(
                    f"group {label}: digit count mismatch "
                    f"(got {len(act_g.get('digits') or [])}, expected {exp_g['digit_count']})"
                )

        for i, exp_d in enumerate(exp_g.get("digits") or []):
            try:
                act_d = act_g["digits"][i]
            except (KeyError, IndexError):
                failures.append(f"group {label}[{i}]: missing in actual output")
                continue
            if exp_d.get("best") and exp_d["best"] != act_d.get("best"):
                failures.append(
                    f"group {label}[{i}]: best digit mismatch "
                    f"(got {act_d.get('best')!r}, expected {exp_d['best']!r})"
                )
            if exp_d.get("verdict") and exp_d["verdict"] != act_d.get("verdict"):
                failures.append(
                    f"group {label}[{i}]: verdict mismatch "
                    f"(got {act_d.get('verdict')!r}, expected {exp_d['verdict']!r})"
                )
            md = exp_d.get("max_distance")
            if md is not None:
                ad = float(act_d.get("distance") or 0.0)
                if ad > md + dist_slack:
                    failures.append(
                        f"group {label}[{i}]: distance {ad:.4f} exceeds "
                        f"max {md:.4f} (+ slack {dist_slack:.4f})"
                    )
            mm = exp_d.get("min_margin")
            if mm is not None:
                am = float(act_d.get("margin") or 0.0)
                if am < mm - margin_slack:
                    failures.append(
                        f"group {label}[{i}]: margin {am:.4f} below "
                        f"min {mm:.4f} (- slack {margin_slack:.4f})"
                    )

    return failures


# --- Per-fixture run -------------------------------------------------------


def _run_fixture(reader: KdaReader, json_path: Path, verbose: bool,
                 save_binary: bool) -> bool:
    spec = json.loads(json_path.read_text(encoding="utf-8"))
    img_path = json_path.with_name(spec.get("image") or (json_path.stem + ".png"))
    if not img_path.exists():
        print(f"  {_bad('MISSING')} image not found: {img_path.name}")
        return False

    img = Image.open(img_path).convert("RGB")
    details = reader.read_kda_with_details(img)

    actual_payload = {
        "kda":            list(details["kda"]) if details["kda"] else None,
        "failure_reason": details.get("failure_reason"),
        "groups": [
            {
                "label":        g.get("label"),
                "concatenated": g.get("concatenated"),
                "digits": [
                    {
                        "best":     d.get("best"),
                        "verdict":  d.get("verdict"),
                        "distance": d.get("distance"),
                        "margin":   d.get("margin"),
                        "method":   d.get("method"),
                    }
                    for d in (g.get("digits") or [])
                ],
            }
            for g in (details.get("groups") or [])
        ],
    }

    failures = _compare(
        actual_payload,
        spec.get("expected", {}),
        spec.get("tolerance", {}),
    )

    label = f"  {json_path.stem}"
    if failures:
        print(f"  {_bad('FAIL')} {json_path.stem}")
        for f in failures:
            print(f"      - {f}")
    else:
        bits = []
        if actual_payload["kda"]:
            bits.append("kda=" + "/".join(str(n) for n in actual_payload["kda"]))
        else:
            bits.append(f"kda=None ({actual_payload['failure_reason'] or 'no reason'})")
        bits.append(f"{details.get('elapsed_ms', 0):.1f}ms")
        print(f"  {_ok('PASS')} {json_path.stem}   {_dim(' · '.join(bits))}")

    if verbose:
        for g in actual_payload["groups"]:
            for i, d in enumerate(g["digits"] or []):
                print(
                    f"      {g['label']}[{i}] "
                    f"best={d.get('best')!r:>4} "
                    f"verdict={d.get('verdict'):<8} "
                    f"method={d.get('method'):<10} "
                    f"distance={float(d.get('distance') or 0):.4f} "
                    f"margin={float(d.get('margin') or 0):.4f}"
                )

    if save_binary and details.get("binary_8x") is not None:
        out = json_path.with_name(json_path.stem + ".binary_8x.png")
        details["binary_8x"].save(out)
        print(f"      {_dim('binary saved → ' + out.name)}")

    return not failures


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Run the KDA reader against fixture frames and "
                    "diff actual vs expected output.",
    )
    parser.add_argument(
        "filter",
        nargs="?",
        default="",
        help="Substring filter on fixture name (default: all fixtures).",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=FIXTURE_DIR,
        help=f"Override fixture directory (default: {FIXTURE_DIR}).",
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-digit margins/distances.")
    parser.add_argument("--save-binary", action="store_true",
                        help="Save the binarised 8x crop next to each fixture.")
    args = parser.parse_args(argv)

    fixture_dir = args.fixture_dir
    if not fixture_dir.exists():
        print(_bad(f"Fixture dir missing: {fixture_dir}"))
        return 1

    fixtures = sorted(
        p for p in fixture_dir.glob("*.json")
        if args.filter.lower() in p.stem.lower()
    )
    if not fixtures:
        print(_warn(f"No fixtures matched filter {args.filter!r} in {fixture_dir}"))
        return 1

    print(f"KDA fixture runner — {len(fixtures)} fixture(s) in {fixture_dir}")
    reader = KdaReader()
    if not reader.is_ready:
        print(_bad("KdaReader not ready — need digit templates or Tesseract installed."))
        return 1

    passed = 0
    for fx in fixtures:
        if _run_fixture(reader, fx, args.verbose, args.save_binary):
            passed += 1

    total = len(fixtures)
    summary = f"{passed}/{total} passed"
    if passed == total:
        print(_ok(f"\n{summary}"))
        return 0
    print(_bad(f"\n{summary}"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
