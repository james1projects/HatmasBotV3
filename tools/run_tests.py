"""
run_tests.py — run the whole test suite, the only trustworthy way
=================================================================

House convention: every file in tests/ named test_*.py is a
self-running script (its own harness, its own fakes, exit 0 on full
pass — see the docstring of any of them). This runner executes each
one as a subprocess and fails loudly unless EVERY file exits 0.

Guards against the classic false-pass failure modes:
  - zero test files found          -> exit 2 (never "pass by vacancy")
  - a file hangs                   -> killed at PER_FILE_TIMEOUT, FAIL
  - a file dies before its harness -> nonzero exit is a FAIL, and its
    output tail is printed so CI logs show the reason

Used by: .github/workflows/ci.yml, streamdeck/ship_it.bat, and as the
verify_cmd for local-worker dispatches.

    python tools\run_tests.py             # whole suite
    python tools\run_tests.py web trade   # only files matching a term
"""

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PER_FILE_TIMEOUT = 300  # seconds; generous — slowest file is ~30s


def main() -> int:
    terms = [t.lower() for t in sys.argv[1:]]
    files = sorted((REPO / "tests").glob("test_*.py"))
    if terms:
        files = [f for f in files if any(t in f.name.lower() for t in terms)]

    if not files:
        print("[run_tests] FATAL: no test files matched under tests/ — "
              "refusing to report success on an empty run.")
        return 2

    # Children print unicode; keep them happy on Windows consoles and
    # CI log capture alike.
    import os
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    failures = []
    t0 = time.time()
    for path in files:
        start = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                cwd=REPO, env=env, timeout=PER_FILE_TIMEOUT,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            rc = -1
            out = ((exc.stdout or "") + (exc.stderr or "")
                   if isinstance(exc.stdout, str) else "")
            out += f"\n[run_tests] killed after {PER_FILE_TIMEOUT}s timeout"
        dur = time.time() - start

        if rc == 0:
            print(f"[PASS] {path.name}  ({dur:.1f}s)")
        else:
            print(f"[FAIL] {path.name}  (exit {rc}, {dur:.1f}s)")
            failures.append((path.name, rc, out))

    print("-" * 60)
    total = time.time() - t0
    if failures:
        for name, rc, out in failures:
            tail = "\n".join(out.strip().splitlines()[-25:])
            print(f"\n--- {name} (exit {rc}) — last lines: ---\n{tail}")
        print(f"\n[run_tests] {len(failures)}/{len(files)} FILES FAILED "
              f"({total:.0f}s): " + ", ".join(n for n, _, _ in failures))
        return 1
    print(f"[run_tests] all {len(files)} files passed ({total:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
