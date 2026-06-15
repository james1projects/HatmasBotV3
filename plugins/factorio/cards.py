"""
CardStore: persisted Streamloots-card -> Factorio-action mappings.

Single source of truth is data/factorio_cards.json, editable live from
the card manager page at /factorio/cards on the bot webserver (port
8069). FACTORIO_CARD_MAP in config only SEEDS the file on first run —
after that the JSON file wins, so renaming a card in the Streamloots
dashboard is a UI edit, never a code or config change.

Lookups are case-insensitive and whitespace-trimmed so a card named
"adopt a pet " on Streamloots still matches "Adopt a Pet" here.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

from plugins.factorio.catalog import VALID_ACTIONS


def _norm(name: str) -> str:
    return str(name or "").strip().lower()


class CardStore:
    def __init__(self, path: Path, seed: Optional[Dict] = None):
        self.path = Path(path)
        self._cards: List[Dict] = []     # [{card, action, cooldown}]
        self._index: Dict[str, Dict] = {}
        self._load(seed or {})

    # ── persistence ─────────────────────────────────────────────────

    def _load(self, seed: Dict):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._cards = [c for c in data.get("cards", [])
                               if c.get("card")]
            except (json.JSONDecodeError, OSError) as e:
                print(f"[Factorio] Card store unreadable ({e}); "
                      f"starting empty (file left untouched)")
                self._cards = []
        else:
            # First run: seed from config and write the file.
            self._cards = [
                {"card": name,
                 "action": entry.get("action"),
                 "cooldown": float(entry.get("cooldown") or 0)}
                for name, entry in seed.items()
            ]
            self._save()
            if self._cards:
                print(f"[Factorio] Card store seeded with "
                      f"{len(self._cards)} mapping(s) from config")
        self._reindex()

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"version": 1, "cards": self._cards},
                           indent=2) + "\n",
                encoding="utf-8")
        except OSError as e:
            print(f"[Factorio] Card store save failed: {e}")

    def _reindex(self):
        self._index = {_norm(c["card"]): c for c in self._cards}

    # ── public api ──────────────────────────────────────────────────

    def all(self) -> List[Dict]:
        return [dict(c) for c in self._cards]

    def get(self, card_name: str) -> Optional[Dict]:
        entry = self._index.get(_norm(card_name))
        return dict(entry) if entry else None

    def set(self, card_name: str, action: str, cooldown: float) -> str:
        """Add or update a mapping. Returns '' on success or a
        plain-text error."""
        card_name = str(card_name or "").strip()
        if not card_name:
            return "card name is required"
        if action not in VALID_ACTIONS:
            return f"unknown action {action!r}"
        try:
            cooldown = max(0.0, float(cooldown))
        except (TypeError, ValueError):
            return "cooldown must be a number"
        existing = self._index.get(_norm(card_name))
        if existing:
            existing["card"] = card_name   # keep latest casing
            existing["action"] = action
            existing["cooldown"] = cooldown
        else:
            self._cards.append({"card": card_name, "action": action,
                                "cooldown": cooldown})
        self._reindex()
        self._save()
        return ""

    def remove(self, card_name: str) -> bool:
        key = _norm(card_name)
        before = len(self._cards)
        self._cards = [c for c in self._cards if _norm(c["card"]) != key]
        if len(self._cards) != before:
            self._reindex()
            self._save()
            return True
        return False
