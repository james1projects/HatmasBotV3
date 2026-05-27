"""
tools/cleanup_god_names.py
==========================
One-shot migration to strip the "Gods." Unreal-class-path prefix from
god names that leaked through tracker.gg's display-name mapping
(e.g. "Gods.Atlas" -> "Atlas"). The fix at the ingestion boundary
lives in plugins/smite/history.py:_clean_god_name; this script catches
the existing rows that were stored before that helper was wired in.

What it does
------------
1. Backs up `data/economy.db` to a timestamped `.backup-N` file.
2. Discovers every table with a `god_name` column via sqlite_master /
   PRAGMA table_info — so future tables get picked up automatically.
3. For each table, finds rows where `god_name LIKE 'Gods.%'` and
   renames them to the stripped version.
4. Where a UNIQUE / PRIMARY KEY constraint on `god_name` (or a
   composite including it) would collide with a clean row that
   already exists, merges instead of overwriting:

     - god_prices (PK god_name)
         Clean row wins. Dirty row is dropped — we don't try to
         reconcile two parallel price histories.
     - youtube_holdings (PK yt_channel_id + god_name)
         Shares sum, avg_cost becomes share-weighted across the
         dirty + clean rows. Dirty row is then deleted.
     - god_pool (PK god_name)
         vote_count sums. Dirty row deleted.
     - god_pool_votes / price_history / youtube_transactions /
       youtube_video_gods / pending_yt_nominations: simple UPDATE,
       no merge needed (no UNIQUE on god_name in those tables, or
       collision is implausible).

Idempotent. Safe to re-run — if there are no `Gods.%` rows left
the script reports "nothing to do" and exits.

Usage
-----
    python tools/cleanup_god_names.py --dry-run   # preview
    python tools/cleanup_god_names.py             # actually rename
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# Bootstrap path so we can import core.config like every other tool.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.config import ECONOMY_DB_PATH


PREFIX = "Gods."


def _clean(name: str) -> str:
    """Strip the 'Gods.' prefix if present. Idempotent."""
    if name and name.startswith(PREFIX):
        return name[len(PREFIX):]
    return name


def _discover_tables_with_god_name(conn: sqlite3.Connection):
    """Return list of table names that have a 'god_name' column."""
    tables = []
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (name,) in rows:
        cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
        if any(c[1] == "god_name" for c in cols):
            tables.append(name)
    return tables


# ──────────────────────────────────────────────────────────────────
#   PER-TABLE MERGE HANDLERS
# ──────────────────────────────────────────────────────────────────
# Each handler takes (conn, dirty_name, clean_name) and performs the
# rename — either as a straight UPDATE or, if a clean row already
# exists, a table-specific merge that combines the two before dropping
# the dirty row.

def _merge_god_prices(conn, dirty, clean_name):
    """god_prices PK is god_name. If both rows exist, clean wins —
    we drop the dirty row outright. Two parallel price histories for
    the same god isn't a thing worth reconciling."""
    has_clean = conn.execute(
        "SELECT 1 FROM god_prices WHERE god_name = ?",
        (clean_name,)).fetchone()
    if has_clean:
        conn.execute("DELETE FROM god_prices WHERE god_name = ?", (dirty,))
    else:
        conn.execute(
            "UPDATE god_prices SET god_name = ? WHERE god_name = ?",
            (clean_name, dirty))


def _merge_youtube_holdings(conn, dirty, clean_name):
    """Composite PK is (yt_channel_id, god_name). Per holder, if a
    clean row exists alongside the dirty one, sum shares and use a
    share-weighted average cost. Then delete the dirty row."""
    rows = conn.execute(
        "SELECT yt_channel_id, shares, avg_cost "
        "  FROM youtube_holdings WHERE god_name = ?",
        (dirty,)).fetchall()
    for chan, dshares, davg in rows:
        clean_row = conn.execute(
            "SELECT shares, avg_cost FROM youtube_holdings "
            " WHERE yt_channel_id = ? AND god_name = ?",
            (chan, clean_name)).fetchone()
        if clean_row is None:
            conn.execute(
                "UPDATE youtube_holdings SET god_name = ? "
                " WHERE yt_channel_id = ? AND god_name = ?",
                (clean_name, chan, dirty))
            continue
        cshares, cavg = clean_row
        dshares = float(dshares or 0)
        davg = float(davg or 0)
        cshares = float(cshares or 0)
        cavg = float(cavg or 0)
        total = dshares + cshares
        new_avg = ((dshares * davg) + (cshares * cavg)) / total \
                  if total > 0 else 0.0
        conn.execute(
            "UPDATE youtube_holdings SET shares = ?, avg_cost = ? "
            " WHERE yt_channel_id = ? AND god_name = ?",
            (total, new_avg, chan, clean_name))
        conn.execute(
            "DELETE FROM youtube_holdings "
            " WHERE yt_channel_id = ? AND god_name = ?",
            (chan, dirty))


def _merge_god_pool(conn, dirty, clean_name):
    """god_pool PK is god_name. Sum vote counts and keep the clean
    row's added_by (or take dirty's if no clean row exists)."""
    drow = conn.execute(
        "SELECT vote_count FROM god_pool WHERE god_name = ?",
        (dirty,)).fetchone()
    if drow is None:
        return
    dvotes = int(drow[0] or 0)
    crow = conn.execute(
        "SELECT vote_count FROM god_pool WHERE god_name = ?",
        (clean_name,)).fetchone()
    if crow is None:
        conn.execute(
            "UPDATE god_pool SET god_name = ? WHERE god_name = ?",
            (clean_name, dirty))
        return
    conn.execute(
        "UPDATE god_pool SET vote_count = vote_count + ? "
        " WHERE god_name = ?",
        (dvotes, clean_name))
    conn.execute("DELETE FROM god_pool WHERE god_name = ?", (dirty,))


MERGE_HANDLERS = {
    "god_prices":         _merge_god_prices,
    "youtube_holdings":   _merge_youtube_holdings,
    "god_pool":           _merge_god_pool,
}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--dry-run", action="store_true",
                   help="Preview the rename plan without writing.")
    args = p.parse_args()

    db_path = Path(ECONOMY_DB_PATH)
    if not db_path.exists():
        print(f"[!] DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)
    print(f"[i] DB: {db_path}")

    # Backup BEFORE we touch anything. Backup file name includes a
    # unix timestamp so multiple runs don't overwrite each other.
    if not args.dry_run:
        backup = db_path.parent / f"{db_path.name}.backup-{int(time.time())}"
        shutil.copy2(db_path, backup)
        print(f"[i] Backup: {backup}")

    conn = sqlite3.connect(db_path)
    try:
        tables = _discover_tables_with_god_name(conn)
        print(f"[i] Found {len(tables)} table(s) with a god_name column:")
        for t in tables:
            print(f"      - {t}")

        # Pass 1 — scan. Build a {table: [dirty_name, ...]} map so we
        # can print a complete preview before applying anything.
        plan = {}
        total_rows = 0
        for t in tables:
            dirty_names = [
                r[0] for r in conn.execute(
                    f"SELECT DISTINCT god_name FROM {t} "
                    " WHERE god_name LIKE 'Gods.%'"
                ).fetchall()
            ]
            if not dirty_names:
                continue
            plan[t] = dirty_names
            for d in dirty_names:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE god_name = ?",
                    (d,)).fetchone()[0]
                total_rows += n

        if not plan:
            print("\n[+] No 'Gods.*' rows found. Nothing to do.")
            return

        print(f"\n[i] Plan ({total_rows} row(s) across {len(plan)} table(s)):")
        for t, dirty_names in plan.items():
            for d in dirty_names:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE god_name = ?",
                    (d,)).fetchone()[0]
                handler = "merge" if t in MERGE_HANDLERS else "update"
                print(f"      {t}: '{d}' -> '{_clean(d)}'  "
                      f"({n} row{'s' if n != 1 else ''}, {handler})")

        if args.dry_run:
            print("\n[i] Dry run — no changes written.")
            return

        # Pass 2 — apply.
        print("\n[i] Applying...")
        for t, dirty_names in plan.items():
            handler = MERGE_HANDLERS.get(t)
            for d in dirty_names:
                clean_name = _clean(d)
                if clean_name == d:
                    continue
                if handler is not None:
                    handler(conn, d, clean_name)
                else:
                    try:
                        conn.execute(
                            f"UPDATE {t} SET god_name = ? "
                            " WHERE god_name = ?",
                            (clean_name, d))
                    except sqlite3.IntegrityError as e:
                        print(f"      [!] {t}: UNIQUE collision on "
                              f"'{d}' — skipping ({e})")
        conn.commit()
        print("[+] Done.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
