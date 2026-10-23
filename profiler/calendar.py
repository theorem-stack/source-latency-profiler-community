from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class CalendarError(ValueError):
    """Raised when an event calendar file is malformed or missing."""


def load_event_calendar(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate a JSON event calendar file.

    Each entry must have at minimum:
      - event_id: str
      - release_at_et: ISO-8601 datetime string
    Optional fields:
      - discover_key: str (defaults to event_id)
      - event_overrides: dict merged into strategy.event at patch time
    """
    p = Path(path)
    if not p.exists():
        raise CalendarError(f"event calendar not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalendarError(f"event calendar is not valid JSON: {p}: {exc}") from exc

    if not isinstance(raw, list):
        raise CalendarError(f"event calendar must be a JSON array: {p}")

    events: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CalendarError(f"event calendar entry[{index}] must be an object")
        if not entry.get("event_id"):
            raise CalendarError(f"event calendar entry[{index}] missing required field: event_id")
        if not entry.get("release_at_et"):
            raise CalendarError(f"event calendar entry[{index}] missing required field: release_at_et")
        try:
            datetime.fromisoformat(str(entry["release_at_et"]))
        except ValueError as exc:
            raise CalendarError(
                f"event calendar entry[{index}] has invalid release_at_et: {entry['release_at_et']}"
            ) from exc
        events.append(entry)

    seen_ids: set[str] = set()
    for entry in events:
        eid = entry["event_id"]
        if eid in seen_ids:
            raise CalendarError(f"duplicate event_id in calendar: {eid}")
        seen_ids.add(eid)

    return events


def next_pending_event(
    events: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    now: datetime,
    execute_offset: str,
) -> dict[str, Any] | None:
    """Return the earliest calendar event that still has pending work.

    An event is considered complete when its execute_offset appears in the
    per-event completed_offsets in state. Events whose release time is more
    than 4 hours in the past are skipped (assumed missed / no longer actionable).

    Returns None if no pending events exist.
    """
    now_utc = now if now.tzinfo else now.replace(tzinfo=UTC)
    state_events = state.get("events", {})

    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for entry in events:
        release = datetime.fromisoformat(str(entry["release_at_et"]))
        release_utc = release if release.tzinfo else release.replace(tzinfo=UTC)

        # Skip events that are too far in the past to be actionable.
        seconds_past = (now_utc - release_utc).total_seconds()
        if seconds_past > 4 * 3600:
            logging.warning(
                "Calendar event %s skipped: release was %.1fh ago (past 4h actionability window)",
                entry["event_id"],
                seconds_past / 3600,
            )
            continue

        event_id = str(entry["event_id"])
        event_state = state_events.get(event_id, {})
        completed = set(event_state.get("completed_offsets", []))

        # Skip events whose execute step has already fired.
        if execute_offset in completed:
            continue

        candidates.append((release_utc, entry))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]
