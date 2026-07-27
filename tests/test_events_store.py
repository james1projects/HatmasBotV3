"""Test script for core/events_store.py"""
import sys
from pathlib import Path
import tempfile
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.events_store as es

def test_parse_iso_utc():
    # Happy path
    assert es.parse_iso_utc('2026-08-01T18:00:00Z') is not None
    assert es.parse_iso_utc('2026-08-01T18:00:00+00:00') is not None
    
    # Naive datetime treated as UTC
    dt = es.parse_iso_utc('2026-08-01T18:00:00')
    assert dt is not None
    assert dt.tzinfo == timezone.utc

    # Invalid inputs
    assert es.parse_iso_utc(None) is None
    assert es.parse_iso_utc('') is None
    assert es.parse_iso_utc('not-a-date') is None
    assert es.parse_iso_utc('2026-08-01T18:00:00') is not None  # naive

def test_validate_event_happy_path_create():
    raw = {
        "title": "Test Event",
        "starts_at": "2026-08-01T18:00:00Z"
    }
    events = []
    event, error = es.validate_event(raw, events)
    assert event is not None
    assert error is None
    assert 'id' in event
    assert event['title'] == 'Test Event'
    assert event['type'] == 'special'
    assert event['status'] == 'draft'
    assert event['starts_at'] == '2026-08-01T18:00:00Z'
    assert 'created_at' in event
    assert 'updated_at' in event

def test_validate_event_happy_path_update():
    existing = [{
        "id": "test-event",
        "title": "Test Event",
        "starts_at": "2026-08-01T18:00:00Z",
        "created_at": "2026-07-01T18:00:00Z"
    }]
    raw = {
        "id": "test-event",
        "title": "Updated Test Event",
        "starts_at": "2026-08-01T18:00:00Z"
    }
    event, error = es.validate_event(raw, existing)
    assert event is not None
    assert error is None
    assert event['id'] == 'test-event'
    assert event['title'] == 'Updated Test Event'
    assert event['created_at'] == '2026-07-01T18:00:00Z'

def test_validate_event_missing_title():
    raw = {
        "starts_at": "2026-08-01T18:00:00Z"
    }
    event, error = es.validate_event(raw, [])
    assert event is None
    assert error == "Title is required."

def test_validate_event_bad_type():
    raw = {
        "title": "Test Event",
        "type": "invalid-type",
        "starts_at": "2026-08-01T18:00:00Z"
    }
    event, error = es.validate_event(raw, [])
    assert event is None
    assert "Type must be one of:" in error

def test_validate_event_bad_status():
    raw = {
        "title": "Test Event",
        "status": "invalid-status",
        "starts_at": "2026-08-01T18:00:00Z"
    }
    event, error = es.validate_event(raw, [])
    assert event is None
    assert "Status must be one of:" in error

def test_validate_event_missing_starts_at():
    raw = {
        "title": "Test Event"
    }
    event, error = es.validate_event(raw, [])
    assert event is None
    assert error == "Start time is required (ISO-8601)."

def test_validate_event_ends_at_before_starts_at():
    raw = {
        "title": "Test Event",
        "starts_at": "2026-08-01T18:00:00Z",
        "ends_at": "2026-08-01T17:00:00Z"
    }
    event, error = es.validate_event(raw, [])
    assert event is None
    assert error == "End time must be after the start time."

def test_validate_event_invalid_link():
    raw = {
        "title": "Test Event",
        "starts_at": "2026-08-01T18:00:00Z",
        "link": "not-a-url"
    }
    event, error = es.validate_event(raw, [])
    assert event is None
    assert error == "Link must start with http:// or https://."

def test_validate_event_invalid_roster():
    raw = {
        "title": "Test Event",
        "starts_at": "2026-08-01T18:00:00Z",
        "roster": "not-a-list"
    }
    event, error = es.validate_event(raw, [])
    assert event is None
    assert error == "Roster must be a list of names."

def test_validate_event_unknown_id():
    raw = {
        "id": "unknown-event",
        "title": "Test Event",
        "starts_at": "2026-08-01T18:00:00Z"
    }
    event, error = es.validate_event(raw, [])
    assert event is None
    assert error == "Unknown event id."

def test_make_id_disambiguation():
    existing_ids = {"duel"}
    result = es.make_id("Duel", existing_ids)
    assert result == "duel-2"
    
    existing_ids = {"duel", "duel-2"}
    result = es.make_id("Duel", existing_ids)
    assert result == "duel-3"

def test_roster_processing():
    raw = {
        "title": "Test Event",
        "starts_at": "2026-08-01T18:00:00Z",
        "roster": ["  Alice   ", "Bob", "Alice", "Charlie"]
    }
    event, error = es.validate_event(raw, [])
    assert event is not None
    assert error is None
    assert event['roster'] == ['Alice', 'Bob', 'Charlie']

def test_title_truncation():
    long_title = "A" * 100
    raw = {
        "title": long_title,
        "starts_at": "2026-08-01T18:00:00Z"
    }
    event, error = es.validate_event(raw, [])
    assert event is not None
    assert len(event['title']) == 80

def test_public_events_filtering():
    events = [
        {"status": "draft", "starts_at": "2026-08-01T18:00:00Z"},
        {"status": "announced", "starts_at": "2026-08-01T18:00:00Z"},
        {"status": "live", "starts_at": "2026-08-01T18:00:00Z"},
        {"status": "completed", "starts_at": "2026-08-01T18:00:00Z"},
        {"status": "cancelled", "starts_at": "2026-08-01T18:00:00Z"}
    ]
    result = es.public_events(events)
    assert len(result) == 4
    assert all(e['status'] != 'draft' for e in result)

def test_public_events_sorting():
    events = [
        {"status": "announced", "starts_at": "2026-08-02T18:00:00Z"},
        {"status": "announced", "starts_at": "2026-08-01T18:00:00Z"}
    ]
    result = es.public_events(events)
    assert result[0]['starts_at'] == '2026-08-01T18:00:00Z'
    assert result[1]['starts_at'] == '2026-08-02T18:00:00Z'

def test_upsert_event_replace():
    events = [{"id": "test", "title": "Old"}]
    new_event = {"id": "test", "title": "New"}
    result = es.upsert_event(events, new_event)
    assert len(result) == 1
    assert result[0]['title'] == 'New'

def test_upsert_event_append():
    events = [{"id": "test", "title": "Old"}]
    new_event = {"id": "new", "title": "New"}
    result = es.upsert_event(events, new_event)
    assert len(result) == 2
    assert result[1]['title'] == 'New'

def test_delete_event_found():
    events = [{"id": "test", "title": "Test"}]
    result = es.delete_event(events, "test")
    assert result is True
    assert len(events) == 0

def test_delete_event_not_found():
    events = [{"id": "test", "title": "Test"}]
    result = es.delete_event(events, "missing")
    assert result is False
    assert len(events) == 1

def test_load_events_missing_file():
    result = es.load_events("/nonexistent/path.json")
    assert result == []

def test_load_events_invalid_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "events.json"
        path.write_text("invalid json")
        try:
            es.load_events(path)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "events file is not valid JSON" in str(e)

def test_load_events_wrong_format():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "events.json"
        path.write_text('{"not-events": []}')
        try:
            es.load_events(path)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert 'events file must be {"events": [...]}' in str(e)

def test_load_events_round_trip():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "events.json"
        original = [{"id": "test", "title": "Test Event"}]
        es.save_events(path, original)
        loaded = es.load_events(path)
        assert loaded == original

def test_save_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "events.json"
        events = [{"id": "test", "title": "Test Event"}]
        es.save_events(path, events)
        content = path.read_text()
        assert '"events"' in content
        assert 'Test Event' in content

# Collected AFTER the definitions — at module top this list is empty
# and the suite "passes" having run nothing.
TESTS = [v for k, v in sorted(globals().items()) if k.startswith('test_')]


def main() -> int:
    passed = 0
    failed = 0
    for test_func in TESTS:
        try:
            test_func()
            print(f"PASS  {test_func.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {test_func.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {test_func.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if not TESTS:
        print("FAIL  no tests were collected")
        return 1
    return 1 if failed > 0 else 0

if __name__ == '__main__':
    sys.exit(main())
