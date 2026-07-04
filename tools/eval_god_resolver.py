"""
tools/eval_god_resolver.py — accuracy report for core/god_resolver.py.

Runs every case in tools/god_resolver_eval_cases.json through the
resolver against the canonical SMITE2_GODS list and prints per-tier
and per-kind accuracy. Junk cases count as correct only when the
resolver returns None — a resolver that guesses on garbage is worse
than one that says "no idea".

Run:
    python tools/eval_god_resolver.py           # tiers 1-5 (no LLM)
    python tools/eval_god_resolver.py --llm     # include the Ollama tier
                                                  (needs Ollama up; slow)

Exit code 0 when non-junk accuracy >= 90% AND no junk input resolves
to a god; 1 otherwise. Safe to wire into a pre-stream check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.god_resolver import resolve, resolve_sync  # noqa: E402
from plugins.godrequest import SMITE2_GODS  # noqa: E402

CASES_FILE = Path(__file__).parent / "god_resolver_eval_cases.json"

PASS_ACCURACY = 0.90


async def run(use_llm: bool) -> int:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]

    tier_hits = Counter()
    kind_total = Counter()
    kind_correct = Counter()
    failures = []
    junk_resolved = []

    for case in cases:
        text, expect, kind = case["input"], case["expect"], case["kind"]
        kind_total[kind] += 1
        if use_llm:
            hit = await resolve(text, SMITE2_GODS)
        else:
            hit = resolve_sync(text, SMITE2_GODS)

        got = hit[0] if hit else None
        tier = hit[1] if hit else "-"

        if expect is None:
            if got is None:
                kind_correct[kind] += 1
            else:
                junk_resolved.append((text, got, tier))
        elif got == expect:
            kind_correct[kind] += 1
            tier_hits[tier] += 1
        else:
            failures.append((text, expect, got, tier))

    print("Per-kind accuracy:")
    for kind in sorted(kind_total):
        c, t = kind_correct[kind], kind_total[kind]
        print(f"  {kind:<10} {c:>3}/{t:<3} ({c / t:.0%})")

    print("\nResolving tier for correct non-junk cases:")
    for tier, n in tier_hits.most_common():
        print(f"  {tier:<10} {n}")

    if failures:
        print(f"\nMisses ({len(failures)}):")
        for text, expect, got, tier in failures:
            print(f"  {text!r:<28} expected {expect!r}, got {got!r} [{tier}]")
    if junk_resolved:
        print(f"\nJunk wrongly resolved ({len(junk_resolved)}):")
        for text, got, tier in junk_resolved:
            print(f"  {text!r:<28} -> {got!r} [{tier}]")

    non_junk_total = sum(t for k, t in kind_total.items() if k != "junk")
    non_junk_correct = sum(c for k, c in kind_correct.items() if k != "junk")
    accuracy = non_junk_correct / non_junk_total if non_junk_total else 0.0
    print(f"\nNon-junk accuracy: {non_junk_correct}/{non_junk_total} "
          f"({accuracy:.1%}); junk false-positives: {len(junk_resolved)}")

    ok = accuracy >= PASS_ACCURACY and not junk_resolved
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true",
                        help="also use the Ollama tier for unresolved cases")
    args = parser.parse_args()
    return asyncio.run(run(args.llm))


if __name__ == "__main__":
    sys.exit(main())
