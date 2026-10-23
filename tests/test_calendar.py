from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from profiler.calendar import CalendarError, load_event_calendar, next_pending_event

FUTURE = "2099-01-01T08:30:00-05:00"
RECENT = "2026-06-05T08:30:00-04:00"
OLD = "2020-01-01T08:30:00-05:00"


def _write_calendar(events: list[dict], tmp_path: Path) -> Path:
    p = tmp_path / "calendar.json"
    p.write_text(json.dumps(events), encoding="utf-8")
    return p


def test_load_event_calendar_valid(tmp_path: Path) -> None:
    events = [
        {"event_id": "cpi_2026-07", "release_at_et": "2026-07-11T08:30:00-04:00"},
        {"event_id": "cpi_2026-08", "release_at_et": "2026-08-13T08:30:00-04:00", "discover_key": "2026-08"},
    ]
    path = _write_calendar(events, tmp_path)
    loaded = load_event_calendar(path)
    assert len(loaded) == 2
    assert loaded[0]["event_id"] == "cpi_2026-07"


def test_load_event_calendar_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CalendarError, match="not found"):
        load_event_calendar(tmp_path / "nope.json")


def test_load_event_calendar_not_array(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text('{"event_id": "x"}', encoding="utf-8")
    with pytest.raises(CalendarError, match="JSON array"):
        load_event_calendar(p)


def test_load_event_calendar_missing_event_id(tmp_path: Path) -> None:
    events = [{"release_at_et": "2026-07-11T08:30:00-04:00"}]
    with pytest.raises(CalendarError, match="event_id"):
        load_event_calendar(_write_calendar(events, tmp_path))


def test_load_event_calendar_invalid_release_at(tmp_path: Path) -> None:
    events = [{"event_id": "x", "release_at_et": "not-a-date"}]
    with pytest.raises(CalendarError, match="invalid release_at_et"):
        load_event_calendar(_write_calendar(events, tmp_path))


def test_next_pending_event_returns_nearest_upcoming() -> None:
    events = [
        {"event_id": "cpi_2026-08", "release_at_et": "2026-08-13T08:30:00-04:00"},
        {"event_id": "cpi_2026-07", "release_at_et": "2026-07-11T08:30:00-04:00"},
    ]
    now = datetime(2026, 7, 11, 8, 0, 0, tzinfo=UTC)
    result = next_pending_event(events, state={}, now=now, execute_offset="T-30m")
    assert result is not None
    assert result["event_id"] == "cpi_2026-07"


def test_next_pending_event_skips_completed_events() -> None:
    events = [
        {"event_id": "cpi_2026-07", "release_at_et": "2026-07-11T08:30:00-04:00"},
        {"event_id": "cpi_2026-08", "release_at_et": "2026-08-13T08:30:00-04:00"},
    ]
    state = {"events": {"cpi_2026-07": {"completed_offsets": ["T-30m"]}}}
    now = datetime(2026, 7, 11, 8, 0, 0, tzinfo=UTC)
    result = next_pending_event(events, state=state, now=now, execute_offset="T-30m")
    assert result is not None
    assert result["event_id"] == "cpi_2026-08"


def test_next_pending_event_returns_none_when_all_complete() -> None:
    events = [{"event_id": "cpi_2026-07", "release_at_et": "2026-07-11T08:30:00-04:00"}]
    state = {"events": {"cpi_2026-07": {"completed_offsets": ["T-30m"]}}}
    now = datetime(2026, 7, 11, 8, 0, 0, tzinfo=UTC)
    result = next_pending_event(events, state=state, now=now, execute_offset="T-30m")
    assert result is None


def test_next_pending_event_skips_events_older_than_4h() -> None:
    events = [{"event_id": "cpi_old", "release_at_et": "2026-07-10T08:30:00-04:00"}]
    now = datetime(2026, 7, 10, 17, 0, 0, tzinfo=UTC)  # > 4h after 12:30 UTC
    result = next_pending_event(events, state={}, now=now, execute_offset="T-30m")
    assert result is None


def test_next_pending_event_empty_calendar() -> None:
    result = next_pending_event([], state={}, now=datetime.now(UTC), execute_offset="T-30m")
    assert result is None
