r"""
tools/chat_stats.py — what does chat actually do? (reads data/chat_log.db)

The 2026-09-04 audit could not answer "which features get used" because
nothing logged chat. plugins/cocaster/chatlog.py now records every
message locally; this prints the first answers. Read-only.

    python tools\chat_stats.py                 # last 30 days
    python tools\chat_stats.py --days 7
    python tools\chat_stats.py --db path\to\chat_log.db --top 15
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_rows(db_path: Path, since_ts: float) -> List[Dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT ts, user, display, text, is_command, is_mod FROM messages WHERE ts >= ?"
            " ORDER BY ts", (since_ts,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def summarize(rows: List[Dict], top: int = 10) -> Dict:
    """Pure function over chat rows -> the numbers the report prints."""
    if not rows:
        return {"messages": 0, "users": 0, "days": 0, "commands": {}, "top_users": [],
                "per_day": [], "command_share": 0.0, "lurker_share": 0.0}
    users = Counter(r["user"] for r in rows)
    commands = Counter()
    for r in rows:
        if r["is_command"]:
            name = (r["text"].split() or ["!"])[0].lower()
            commands[name] += 1
    per_day = Counter(datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d") for r in rows)
    chatty = sum(1 for _, n in users.items() if n >= 5)
    one_liners = sum(1 for _, n in users.items() if n == 1)
    return {
        "messages": len(rows),
        "users": len(users),
        "days": len(per_day),
        "commands": dict(commands.most_common(top)),
        "command_share": round(sum(commands.values()) / len(rows), 3),
        "top_users": users.most_common(top),
        "per_day": sorted(per_day.items()),
        "users_5plus": chatty,
        "one_message_users": one_liners,
        "lurker_share": round(one_liners / len(users), 3),
    }


def main(argv=None) -> int:
    from core import config
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(getattr(config, "CHAT_LOG_DB", config.DATA_DIR / "chat_log.db")))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    if not a.db.exists():
        print(f"no chat log yet at {a.db} (it appears once the bot runs with plugins/cocaster loaded)")
        return 0
    rows = load_rows(a.db, time.time() - a.days * 86400)
    s = summarize(rows, a.top)
    print(f"chat log: {s['messages']} messages from {s['users']} users over {s['days']} day(s) "
          f"(last {a.days} days)")
    if not rows:
        return 0
    print(f"commands: {s['command_share']:.0%} of messages; users with 5+ messages: {s['users_5plus']};"
          f" one-message users: {s['one_message_users']} ({s['lurker_share']:.0%})")
    print("\nper day:")
    for day, n in s["per_day"]:
        print(f"  {day}  {n:>5}")
    print("\ntop chatters:")
    for user, n in s["top_users"]:
        print(f"  {user:<24} {n:>5}")
    if s["commands"]:
        print("\ntop commands:")
        for cmd, n in s["commands"].items():
            print(f"  {cmd:<24} {n:>5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
