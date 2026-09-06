"""
plugins/bingo/pool.py — the square pool, card generation, and bingo math.

Pure functions, no I/O except `load_pool` / `save_pool`, so the rules are
unit-testable and the pool file (data/bingo/pool.json) can be edited by
hand between streams.

A square is {"id", "label", "source", "weight"}:
  source "auto"   — fired by the bot (kill detector, economy settle, ...)
  source "manual" — fired by James from the Stream Deck / dashboard
  weight          — relative chance of landing on a card (higher = common)

Cards are 5x5, index 12 is the FREE space, and every card is a seeded
weighted sample without replacement, so the same (round, login, seq)
always regenerates the same card.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

FREE = "free"
FREE_INDEX = 12
CARD_SIZE = 25

# (id, label, source, weight)
DEFAULT_POOL: List[Tuple[str, str, str, float]] = [
    # automatic: kill detector
    ("kill", "Gets a kill", "auto", 10),
    ("death", "Dies", "auto", 10),
    ("assist", "Gets an assist", "auto", 8),
    ("double", "Double kill", "auto", 5),
    ("triple", "Triple kill", "auto", 3),
    ("quadra", "Quadra kill", "auto", 1),
    ("penta", "PENTA KILL", "auto", 0.5),
    ("first_blood", "First blood", "auto", 4),
    ("kills_10", "10 kills in one match", "auto", 3),
    ("deaths_5", "5 deaths in one match", "auto", 3),
    ("deathless", "Deathless match", "auto", 1.5),
    # automatic: economy
    ("match_win", "Wins a match", "auto", 5),
    ("match_loss", "Loses a match", "auto", 5),
    ("new_god", "Plays a god he has not played this stream", "auto", 4),
    # manual: only James can judge these (deck / dashboard buttons)
    ("no_mana", "Says \"no mana\"", "manual", 6),
    ("blames_jungle", "Blames the jungler", "manual", 6),
    ("blames_chat", "Blames chat", "manual", 4),
    ("blames_lag", "Blames lag", "manual", 3),
    ("dies_loki", "Dies to a Loki", "manual", 3),
    ("tower_dive", "Tower dives", "manual", 3),
    ("steal", "Steals an objective", "manual", 2),
    ("gets_stolen", "Gets an objective stolen", "manual", 2),
    ("water", "Drinks water", "manual", 6),
    ("hat", "Adjusts the hat", "manual", 4),
    ("rage", "Rage moment", "manual", 4),
    ("sorry", "Says \"sorry\"", "manual", 5),
    ("baron", "Says \"Baron\"", "manual", 3),
    ("gg", "Says \"GG\"", "manual", 4),
    ("misclick", "Misclicks an ability", "manual", 4),
    ("wrong_button", "Presses the wrong deck button", "manual", 2),
    ("song_skip", "Skips a song", "manual", 3),
    ("raid", "Gets raided", "manual", 2),
    ("new_viewer", "Welcomes a new viewer", "manual", 4),
    ("jackpot", "Someone hits the gamble jackpot", "manual", 2),
    ("bm", "BMs the enemy", "manual", 3),
    ("clutch", "Clutch escape", "manual", 3),
]


def default_pool() -> List[Dict]:
    return [{"id": i, "label": lab, "source": src, "weight": float(w)}
            for i, lab, src, w in DEFAULT_POOL]


def validate_pool(pool: Iterable[Dict]) -> List[Dict]:
    """Keep well-formed, unique, positively weighted squares."""
    out: List[Dict] = []
    seen: Set[str] = set()
    for sq in pool:
        try:
            sid = str(sq["id"]).strip()
            label = str(sq["label"]).strip()
            src = str(sq.get("source", "manual")).strip().lower()
            weight = float(sq.get("weight", 1))
        except (KeyError, TypeError, ValueError):
            continue
        if not sid or not label or sid == FREE or sid in seen or weight <= 0:
            continue
        if src not in ("auto", "manual"):
            src = "manual"
        seen.add(sid)
        out.append({"id": sid, "label": label[:60], "source": src, "weight": weight})
    return out


def load_pool(path: Path) -> List[Dict]:
    """Read the pool file, writing the defaults first if it is missing.
    Falls back to the defaults when the file is unusable."""
    path = Path(path)
    if not path.exists():
        pool = default_pool()
        save_pool(path, pool)
        return pool
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pool = validate_pool(data.get("squares", data) if isinstance(data, dict) else data)
    except (OSError, ValueError):
        pool = []
    return pool if len(pool) >= CARD_SIZE - 1 else default_pool()


def save_pool(path: Path, pool: Sequence[Dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"squares": list(pool)}, indent=2), encoding="utf-8")


def card_seed(round_id: int, login: str, seq: int) -> int:
    """Deterministic seed: the same viewer's Nth card in a round is
    always the same card, even after a bot restart."""
    h = 0
    for ch in f"{round_id}:{login.lower()}:{seq}":
        h = (h * 131 + ord(ch)) % (2 ** 31 - 1)
    return h


def make_card(pool: Sequence[Dict], seed: int) -> List[str]:
    """25 square ids (index 12 = 'free'): a weighted sample without
    replacement so common squares appear on most cards and rare ones on
    a few. Deterministic for a given pool + seed."""
    rng = random.Random(seed)
    remaining = [dict(sq) for sq in pool]
    picked: List[str] = []
    need = CARD_SIZE - 1
    while remaining and len(picked) < need:
        total = sum(sq["weight"] for sq in remaining)
        r = rng.uniform(0, total)
        acc = 0.0
        for i, sq in enumerate(remaining):
            acc += sq["weight"]
            if r <= acc:
                picked.append(sq["id"])
                remaining.pop(i)
                break
        else:
            picked.append(remaining.pop()["id"])
    if len(picked) < need:
        raise ValueError(f"pool too small: {len(pool)} squares, need {need}")
    rng.shuffle(picked)
    picked.insert(FREE_INDEX, FREE)
    return picked


LINES: List[Tuple[int, ...]] = (
    [tuple(range(r * 5, r * 5 + 5)) for r in range(5)]          # rows
    + [tuple(range(c, 25, 5)) for c in range(5)]                 # columns
    + [(0, 6, 12, 18, 24), (4, 8, 12, 16, 20)]                   # diagonals
)


def marked_indexes(card: Sequence[str], called: Iterable[str]) -> Set[int]:
    """Indexes of squares on `card` whose id has been called (+ free)."""
    called_set = set(called)
    return {i for i, sid in enumerate(card) if sid == FREE or sid in called_set}


def winning_lines(marks: Set[int]) -> List[Tuple[int, ...]]:
    return [line for line in LINES if all(i in marks for i in line)]


def has_bingo(marks: Set[int]) -> bool:
    return any(all(i in marks for i in line) for line in LINES)


def squares_to_bingo(marks: Set[int]) -> int:
    """Fewest additional marks needed for a line (0 = bingo)."""
    return min(sum(1 for i in line if i not in marks) for line in LINES)


def label_of(pool: Sequence[Dict], sid: str) -> str:
    if sid == FREE:
        return "FREE"
    for sq in pool:
        if sq["id"] == sid:
            return sq["label"]
    return sid


def next_card_price(seq: int, prices: Sequence[int]) -> Optional[int]:
    """Price of a viewer's card number `seq` (1 = free). None = not for sale."""
    if seq <= 1:
        return 0
    idx = seq - 2
    if idx < len(prices):
        return int(prices[idx])
    return None
