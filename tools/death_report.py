r"""
tools/death_report.py — what were you saying right before you died?

A private review tool over the Ask the VOD index: for every death the
detector found, pulls the transcript lines from the seconds before it
(your mic only by default), groups them by god, and prints them so the
pattern is visible ("I have no mana" eight times out of ten). Optionally
asks a LOCAL model (Ollama) for a two-paragraph read of the pattern.
Nothing here touches the public site or leaves the PC.

    python tools\death_report.py                       # every god, last 90 days
    python tools\death_report.py --god Sylvanus --days 30
    python tools\death_report.py --god Atlas --summarize      # + local Ollama summary
    python tools\death_report.py --before 40 --friends        # wider window, include friends' lines
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vodsearch.store import Store  # noqa: E402

STOP = {"the", "a", "an", "i", "im", "i'm", "to", "and", "of", "that", "it", "is", "in", "on",
        "he", "she", "they", "we", "you", "me", "my", "oh", "okay", "ok", "yeah", "no", "so",
        "just", "like", "was", "this", "for", "with", "have", "got", "get", "go", "gonna", "be",
        "what", "there", "here", "up", "out", "at", "not", "do", "dont", "don't", "can", "cant",
        "can't", "all", "its", "it's", "are", "but", "if", "him", "his", "her", "them", "us",
        "one", "two", "know", "think", "right", "now", "well", "come", "let", "lets", "let's"}


def collect(store: Store, god: Optional[str], since_iso: Optional[str], before_s: float,
            after_s: float, include_friends: bool) -> List[Dict]:
    """One record per death: god, date, ts, and the lines around it."""
    sql = ("SELECT e.id, e.recording_id, e.ts_s, r.god, r.recorded_at, r.stem"
           " FROM events e JOIN recordings r ON r.id = e.recording_id"
           " WHERE e.kind = 'death' AND r.status = 'done'")
    params: list = []
    if god:
        sql += " AND r.god = ?"; params.append(god)
    if since_iso:
        sql += " AND r.recorded_at >= ?"; params.append(since_iso)
    sql += " ORDER BY r.recorded_at, e.ts_s"
    deaths = []
    for row in store.conn.execute(sql, params).fetchall():
        rec_id, ts = int(row["recording_id"]), float(row["ts_s"])
        near = store.conn.execute(
            "SELECT speaker, start_s, text FROM segments WHERE recording_id=? AND end_s >= ?"
            " AND start_s <= ? ORDER BY start_s", (rec_id, ts - before_s, ts + after_s)).fetchall()
        lines = [dict(r) for r in near if include_friends or r["speaker"] == "hatmaster"]
        deaths.append({"event_id": int(row["id"]), "recording_id": rec_id, "god": row["god"],
                       "recorded_at": row["recorded_at"], "stem": row["stem"], "ts_s": ts,
                       "before": [l for l in lines if l["start_s"] < ts],
                       "after": [l for l in lines if l["start_s"] >= ts]})
    return deaths


def word_patterns(deaths: List[Dict], top: int = 12) -> List[tuple]:
    """Most common meaningful words in the seconds before deaths."""
    c: Counter = Counter()
    for d in deaths:
        seen = set()
        for l in d["before"]:
            for w in l["text"].lower().replace("'", "'").split():
                w = "".join(ch for ch in w if ch.isalnum() or ch == "'")
                if len(w) > 2 and w not in STOP:
                    seen.add(w)
        c.update(seen)                     # count deaths a word appears before, not repeats
    return c.most_common(top)


def fmt_clock(s: float) -> str:
    s = int(max(0, s)); m, sec = divmod(s, 60); h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def render(deaths: List[Dict], top_words: int = 12, max_deaths: int = 200) -> str:
    if not deaths:
        return "No deaths with transcript in that range."
    by_god: Dict[str, List[Dict]] = defaultdict(list)
    for d in deaths:
        by_god[d["god"] or "Unknown"].append(d)
    out: List[str] = [f"{len(deaths)} death(s) across {len(by_god)} god(s)\n"]
    for god, ds in sorted(by_god.items(), key=lambda kv: -len(kv[1])):
        out.append(f"=== {god}: {len(ds)} deaths ===")
        pats = word_patterns(ds, top_words)
        if pats:
            out.append("  said before dying: " + ", ".join(f"{w} ({n})" for w, n in pats))
        for d in ds[:max_deaths]:
            when = (d["recorded_at"] or "")[:10]
            out.append(f"  {when} {d['stem']} @ {fmt_clock(d['ts_s'])}  (event e{d['event_id']})")
            for l in d["before"][-4:]:
                who = "you" if l["speaker"] == "hatmaster" else "friend"
                out.append(f"      -{fmt_clock(d['ts_s'] - l['start_s']):>5} {who}: {l['text']}")
            for l in d["after"][:1]:
                out.append(f"      +{fmt_clock(l['start_s'] - d['ts_s']):>5} you: {l['text']}")
        out.append("")
    return "\n".join(out)


def summarize_local(text: str, host: str = "http://localhost:11434", model: str = "qwen3.6:27b",
                    timeout_s: float = 180.0) -> str:
    """Two short paragraphs from a LOCAL model. Nothing leaves the PC."""
    prompt = ("You are a SMITE 2 coach reading a streamer's own words in the seconds before each "
              "of his deaths. Below is the report. Write two short paragraphs: (1) the two or three "
              "recurring situations or habits you can actually see in the words (quote them), "
              "(2) one concrete thing to try next stream. No praise, no filler, no bullet lists.\n\n"
              + text[:12000])
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "think": False,
                       "options": {"num_predict": 400, "temperature": 0.4}}).encode("utf-8")
    req = urllib.request.Request(host.rstrip("/") + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        data = json.loads(r.read().decode("utf-8"))
    return str(data.get("response") or "").strip()


def main(argv=None) -> int:
    from core import config
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(config.VOD_DB_PATH))
    ap.add_argument("--god")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--before", type=float, default=25.0, help="seconds of speech before each death")
    ap.add_argument("--after", type=float, default=4.0)
    ap.add_argument("--friends", action="store_true", help="include friends' lines")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--summarize", action="store_true", help="add a local Ollama read of the pattern")
    ap.add_argument("--model", default=getattr(config, "COCASTER_OLLAMA_MODEL", "qwen3.6:27b"))
    ap.add_argument("--host", default=getattr(config, "COCASTER_OLLAMA_HOST", "http://localhost:11434"))
    a = ap.parse_args(argv)
    if not a.db.exists():
        print(f"no index at {a.db}")
        return 2
    since = (datetime.now() - timedelta(days=a.days)).strftime("%Y-%m-%dT%H:%M:%S") if a.days else None
    with Store(a.db) as store:
        deaths = collect(store, a.god, since, a.before, a.after, a.friends)
    report = render(deaths, a.top)
    print(report)
    if a.summarize and deaths:
        print("\n--- local model read (" + a.model + ") ---")
        try:
            print(summarize_local(report, a.host, a.model))
        except Exception as e:
            print(f"(summary unavailable: {type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
