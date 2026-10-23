from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


PHASE_EVENTS = [
    ("source_fetch", "source_fetch_start", "source_fetch_done"),
    ("parse", "parse_start", "parse_done"),
    ("map", "map_start", "map_done"),
    ("vector", "vector_start", "vector_done"),
    ("kelly", "kelly_start", "kelly_done"),
    ("order_submit", "order_submit_start", "order_submit_done"),
]


DERIVED_GAPS = [
    ("process_to_config_ms", "process_start", "config_load_done"),
    ("config_to_pipeline_init_ms", "config_load_done", "pipeline_init_done"),
    ("engine_wakeup_ms", "execute_start", "source_fetch_start"),
    ("source_fetch_transit_ms", "source_fetch_start", "source_fetch_done"),
    ("decision_pipeline_ms", "source_fetch_done", "order_submit_start"),
    ("venue_submit_ack_ms", "order_submit_start", "order_submit_done"),
]


def build_latency_summary(trace_path: str | Path, *, budgets_ms: dict[str, float] | None = None) -> dict[str, Any]:
    events = read_trace_events(trace_path)
    by_name = {str(event.get("event")): event for event in events if event.get("event")}
    phases = {
        name: _phase_duration_ms(by_name, start_event, done_event)
        for name, start_event, done_event in PHASE_EVENTS
    }
    derived = {
        name: _gap_ms(by_name, start_event, done_event)
        for name, start_event, done_event in DERIVED_GAPS
    }
    total_wall_clock_ms = _total_wall_clock_ms(events)
    hot_path_ms = _gap_ms(by_name, "execute_start", "order_submit_done")
    largest_gap = _largest_gap(derived | phases)
    budgets = budgets_ms or {}
    failures = [
        {"metric": metric, "actual_ms": value, "budget_ms": budget}
        for metric, budget in sorted(budgets.items())
        if (value := _summary_value(metric, phases, derived, total_wall_clock_ms, hot_path_ms)) is not None
        and value > budget
    ]
    return {
        "schema_version": 1,
        "event_count": len(events),
        "status": "failed" if failures else "passed",
        "total_wall_clock_ms": total_wall_clock_ms,
        "hot_path_ms": hot_path_ms,
        "derived_gaps_ms": derived,
        "phases_ms": phases,
        "largest_gap": largest_gap,
        "budget_failures": failures,
    }


def read_trace_events(trace_path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in Path(trace_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events


def write_latency_summary(trace_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    summary = build_latency_summary(trace_path)
    Path(output_path).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def _phase_duration_ms(by_name: dict[str, dict[str, Any]], start_event: str, done_event: str) -> float | None:
    done = by_name.get(done_event, {})
    if "duration_ms" in done:
        return _float(done.get("duration_ms"))
    return _gap_ms(by_name, start_event, done_event)


def _gap_ms(by_name: dict[str, dict[str, Any]], start_event: str, done_event: str) -> float | None:
    start = _event_time(by_name.get(start_event, {}))
    done = _event_time(by_name.get(done_event, {}))
    if not start or not done:
        return None
    return max(0.0, (done - start).total_seconds() * 1000)


def _total_wall_clock_ms(events: list[dict[str, Any]]) -> float | None:
    times = [_event_time(event) for event in events]
    present = [value for value in times if value is not None]
    if len(present) < 2:
        return None
    return max(0.0, (max(present) - min(present)).total_seconds() * 1000)


def _largest_gap(values: dict[str, float | None]) -> dict[str, float] | None:
    present = {key: value for key, value in values.items() if value is not None}
    if not present:
        return None
    metric, value = max(present.items(), key=lambda item: item[1])
    return {"metric": metric, "duration_ms": value}


def _summary_value(
    metric: str,
    phases: dict[str, float | None],
    derived: dict[str, float | None],
    total_wall_clock_ms: float | None,
    hot_path_ms: float | None,
) -> float | None:
    if metric == "total_wall_clock_ms":
        return total_wall_clock_ms
    if metric == "hot_path_ms":
        return hot_path_ms
    if metric in phases:
        return phases[metric]
    return derived.get(metric)


def _event_time(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp") or event.get("time") or event.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
