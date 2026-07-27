"""
core/events_store.py — data layer for the Events tab.
=====================================================

One JSON file (data/events.json) holds every event, upcoming and
past. The public /events page and the broadcaster-only /admin/events
editor both read through this module; only the admin APIs write.

Design notes:
  * Stdlib only, no aiohttp imports — the webserver owns HTTP
    concerns, this module owns the schema + file, so it unit-tests
    in isolation (tests/test_events_store.py), mirroring
    core/web_session.py.
  * load_events() RAISES on a corrupt file instead of returning [].
    Every write is load-modify-save; a tolerant load would let one
    bad hand-edit silently wipe the whole calendar on the next save.
    The webserver catches the error and surfaces it to the admin.
  * validate_event() is the only entry point for untrusted input.
    It returns (event, None) or (None, "human-readable error") and
    never raises on garbage.
  * The v1 schema already carries roster/result fields so future
    self-serve signups and brackets are a behavior change, not a
    migration.

File shape: {"events": [ {...}, ... ]}
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from core.atomic_io import atomic_write_json

EVENT_TYPES = ("tournament", "community", "special")
EVENT_STATUSES = ("draft", "announced", "live", "completed", "cancelled")

# Statuses visible on the public page. Cancelled stays visible on
# purpose — viewers who saw the announcement should see the change,
# not a silent disappearance.
PUBLIC_STATUSES = ("announced", "live", "completed", "cancelled")

MAX_EVENTS = 500
MAX_TITLE = 80
MAX_TEXT = 2000          # description / rules
MAX_SHORT = 200          # prize
MAX_LINK = 300
MAX_RESULT = 300
MAX_ROSTER = 128
MAX_ROSTER_NAME = 40


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(text) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or
    None. Accepts the trailing-Z form JS Date.toISOString() emits."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm_iso(text) -> Optional[str]:
    """Normalize a timestamp to the canonical stored form."""
    dt = parse_iso_utc(text)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def load_events(path) -> List[dict]:
    """All events from `path`. Missing file = no events yet = [].
    Corrupt/unexpected content raises ValueError (see module note)."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"events file is not valid JSON: {e}") from e
    events = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(events, list):
        raise ValueError('events file must be {"events": [...]}')
    return [e for e in events if isinstance(e, dict)]


def save_events(path, events: List[dict]) -> None:
    atomic_write_json(Path(path), {"events": events}, indent=2)


def public_events(events: List[dict]) -> List[dict]:
    """Drafts stripped, sorted soonest-first. Past events keep their
    place in the list; the page splits upcoming/past itself using
    status + starts_at."""
    out = [e for e in events if e.get("status") in PUBLIC_STATUSES]
    out.sort(key=lambda e: e.get("starts_at") or "")
    return out


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "event"


def make_id(title: str, existing_ids) -> str:
    base = _slugify(title)
    if base not in existing_ids:
        return base
    n = 2
    while f"{base}-{n}" in existing_ids:
        n += 1
    return f"{base}-{n}"


def _clean_str(value, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def validate_event(raw, existing: List[dict]) -> Tuple[Optional[dict], Optional[str]]:
    """Build a clean event from an untrusted body dict.

    If raw["id"] matches an existing event this is an update (its
    created_at survives); otherwise a create (id generated from the
    title). Returns (event, None) or (None, error)."""
    if not isinstance(raw, dict):
        return None, "Bad request body."

    title = _clean_str(raw.get("title"), MAX_TITLE)
    if not title:
        return None, "Title is required."

    ev_type = _clean_str(raw.get("type"), 20).lower() or "special"
    if ev_type not in EVENT_TYPES:
        return None, f"Type must be one of: {', '.join(EVENT_TYPES)}."

    status = _clean_str(raw.get("status"), 20).lower() or "draft"
    if status not in EVENT_STATUSES:
        return None, f"Status must be one of: {', '.join(EVENT_STATUSES)}."

    starts_at = _norm_iso(raw.get("starts_at"))
    if starts_at is None:
        return None, "Start time is required (ISO-8601)."
    ends_at = None
    if str(raw.get("ends_at") or "").strip():
        ends_at = _norm_iso(raw.get("ends_at"))
        if ends_at is None:
            return None, "End time must be ISO-8601 (or empty)."
        if ends_at <= starts_at:
            return None, "End time must be after the start time."

    link = _clean_str(raw.get("link"), MAX_LINK)
    if link and not (link.startswith("https://") or link.startswith("http://")):
        return None, "Link must start with http:// or https://."

    roster_raw = raw.get("roster")
    if roster_raw is None:
        roster_raw = []
    if not isinstance(roster_raw, list):
        return None, "Roster must be a list of names."
    roster = []
    for name in roster_raw:
        name = _clean_str(name, MAX_ROSTER_NAME)
        if name and name not in roster:
            roster.append(name)
    if len(roster) > MAX_ROSTER:
        return None, f"Roster is capped at {MAX_ROSTER} names."

    existing_ids = {e.get("id") for e in existing}
    event_id = _clean_str(raw.get("id"), 80)
    prior = next((e for e in existing if e.get("id") == event_id), None) \
        if event_id else None
    if event_id and prior is None:
        return None, "Unknown event id."
    if prior is None:
        if len(existing) >= MAX_EVENTS:
            return None, f"Event list is capped at {MAX_EVENTS}."
        event_id = make_id(title, existing_ids)

    now = _utc_now_iso()
    return {
        "id": event_id,
        "title": title,
        "type": ev_type,
        "status": status,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "description": _clean_str(raw.get("description"), MAX_TEXT),
        "rules": _clean_str(raw.get("rules"), MAX_TEXT),
        "prize": _clean_str(raw.get("prize"), MAX_SHORT),
        "link": link,
        "featured": bool(raw.get("featured")),
        "roster": roster,
        "result": _clean_str(raw.get("result"), MAX_RESULT),
        "created_at": (prior or {}).get("created_at") or now,
        "updated_at": now,
    }, None


def upsert_event(events: List[dict], event: dict) -> List[dict]:
    """Replace the event with the same id, or append. Returns the
    same list (mutated) for load-modify-save call sites."""
    for i, existing in enumerate(events):
        if existing.get("id") == event["id"]:
            events[i] = event
            return events
    events.append(event)
    return events


def delete_event(events: List[dict], event_id: str) -> bool:
    """Remove by id. True if something was removed."""
    before = len(events)
    events[:] = [e for e in events if e.get("id") != event_id]
    return len(events) < before
