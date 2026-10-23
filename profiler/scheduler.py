from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from profiler.calendar import load_event_calendar, next_pending_event
from profiler.markets.config import parse_markets_config
from profiler.markets.discover import MarketsDiscoveryError, run_nullone_discover
from profiler.policy import relative_event_time
from profiler.secrets import load_kalshi_env_from_prefix
from profiler.strategy import StrategyProfile, load_strategy_profile, patch_strategy_event, write_patched_strategy_yaml

REPEATED_FAILURE_LOG_EVERY = 6


@dataclass(frozen=True)
class SchedulerOptions:
    strategy_path: str
    secrets_prefix: str | None = None
    state_path: str = ".generated/scheduler/state.json"
    events_path: str = ".generated/scheduler/events.jsonl"
    poll_seconds: int = 60
    once: bool = False
    dry_run: bool = False
    results_s3_prefix: str | None = None
    runtime_s3_prefix: str | None = None
    run_id: str | None = None
    teardown_mode: str = "after-execution"
    diagnostics: str = "none"
    execution_handoff: str = "host-timer"
    execution_lead_seconds: int = 60
    app_dir: str = "."
    event_calendar_path: str | None = None


def run_scheduler(options: SchedulerOptions) -> int:
    is_template = bool(options.event_calendar_path)
    strategy = load_strategy_profile(options.strategy_path, allow_template=is_template)
    state_path = Path(options.state_path)
    events_path = Path(options.events_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state(state_path)

    now = datetime.now(UTC)

    if options.event_calendar_path:
        calendar_events = load_event_calendar(options.event_calendar_path)
        entry = next_pending_event(
            calendar_events,
            state,
            now=now,
            execute_offset=strategy.automation.warmup_starts_at,
        )
        if entry is None:
            if not options.once:
                _poll_loop(options)
            return 0
        strategy = patch_strategy_event(strategy, entry)

    markets_config = parse_markets_config(strategy.markets)
    event_id = _event_id_for_strategy(strategy)
    state = _migrate_state_if_needed(state, event_id)

    event_time = _strategy_event_time(strategy)
    kalshi_env = _scheduler_env(options)

    due_actions = _due_actions(strategy, markets_config, state, now=now, event_time=event_time)
    if options.dry_run:
        for action in due_actions:
            print(json.dumps(action, sort_keys=True))
        return 0

    exit_code = 0
    try:
        for action in due_actions:
            try:
                if action["type"] == "market_refresh":
                    _run_market_refresh(strategy, markets_config, kalshi_env=kalshi_env)
                    _mark_offset_complete(state, event_id, action["offset"])
                    _write_scheduler_event(
                        events_path,
                        stage="market_refresh",
                        status="ok",
                        offset=action["offset"],
                        event_id=event_id,
                        week_ending=strategy.event.get("week_ending"),
                    )
                elif action["type"] == "strategy_execute":
                    _run_strategy_execute(options, strategy, action=action)
                    _mark_offset_complete(state, event_id, action["offset"])
                    _write_scheduler_event(
                        events_path,
                        stage="execute_started",
                        status="ok",
                        offset=action["offset"],
                        event_id=event_id,
                        run_id=action.get("run_id"),
                    )
            except (MarketsDiscoveryError, RuntimeError, ValueError) as exc:
                exit_code = 1
                should_log, failure_details = _record_failure(
                    state,
                    event_id=event_id,
                    action_type=action["type"],
                    offset=action.get("offset"),
                    error=str(exc),
                )
                _emit_scheduler_failure(action=action, event_id=event_id, error=str(exc), details=failure_details)
                if should_log:
                    _write_scheduler_event(
                        events_path,
                        stage=action["type"],
                        status="failed",
                        offset=action.get("offset"),
                        event_id=event_id,
                        error=str(exc),
                        repeated=failure_details["repeated"],
                        repeat_count=failure_details["repeat_count"],
                    )
                if action["type"] == "strategy_execute":
                    raise
    finally:
        _save_state(state_path, state)
    if options.once:
        return exit_code

    _poll_loop(options)
    return exit_code


def _poll_loop(options: SchedulerOptions) -> None:
    import time

    while True:
        time.sleep(max(1, options.poll_seconds))
        run_scheduler(
            SchedulerOptions(
                strategy_path=options.strategy_path,
                secrets_prefix=options.secrets_prefix,
                state_path=options.state_path,
                events_path=options.events_path,
                poll_seconds=options.poll_seconds,
                once=True,
                dry_run=options.dry_run,
                results_s3_prefix=options.results_s3_prefix,
                runtime_s3_prefix=options.runtime_s3_prefix,
                run_id=options.run_id,
                teardown_mode=options.teardown_mode,
                diagnostics=options.diagnostics,
                execution_handoff=options.execution_handoff,
                execution_lead_seconds=options.execution_lead_seconds,
                app_dir=options.app_dir,
                event_calendar_path=options.event_calendar_path,
            )
        )


def evaluate_due_actions(
    strategy: StrategyProfile,
    *,
    state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    markets_config = parse_markets_config(strategy.markets)
    raw_state = state or {}
    event_id = _event_id_for_strategy(strategy)
    normalized = _migrate_state_if_needed(raw_state, event_id)
    return _due_actions(
        strategy,
        markets_config,
        normalized,
        now=now or datetime.now(UTC),
        event_time=_strategy_event_time(strategy),
    )


def _due_actions(
    strategy: StrategyProfile,
    markets_config: Any,
    state: dict[str, Any],
    *,
    now: datetime,
    event_time: datetime,
) -> list[dict[str, Any]]:
    event_id = _event_id_for_strategy(strategy)
    event_state = state.get("events", {}).get(event_id, {})
    completed = set(event_state.get("completed_offsets", []))
    actions: list[dict[str, Any]] = []

    for offset in markets_config.refresh_offsets:
        if offset in completed:
            continue
        scheduled_at = relative_event_time(strategy.event_time, offset)
        scheduled_utc = _to_utc(scheduled_at)
        if now >= scheduled_utc:
            actions.append(
                {
                    "type": "market_refresh",
                    "offset": offset,
                    "scheduled_at": scheduled_utc.isoformat().replace("+00:00", "Z"),
                }
            )

    warmup_offset = strategy.automation.warmup_starts_at
    if warmup_offset not in completed:
        scheduled_at = relative_event_time(strategy.event_time, warmup_offset)
        scheduled_utc = _to_utc(scheduled_at)
        if now >= scheduled_utc:
            actions.append(
                {
                    "type": "strategy_execute",
                    "offset": warmup_offset,
                    "scheduled_at": scheduled_utc.isoformat().replace("+00:00", "Z"),
                }
            )

    return actions


def _run_market_refresh(
    strategy: StrategyProfile,
    markets_config: Any,
    *,
    kalshi_env: dict[str, str] | None,
) -> Path | None:
    if markets_config.source == "pinned":
        return None
    discover_key_field = getattr(markets_config, "discover_key_field", "week_ending")
    discover_key = str(strategy.event.get(discover_key_field, "")).strip()
    if not discover_key:
        raise ValueError(f"event.{discover_key_field} is required for market refresh")
    return run_nullone_discover(markets_config, discover_key=discover_key, env=kalshi_env)


def _run_strategy_execute(
    options: SchedulerOptions,
    strategy: StrategyProfile,
    *,
    action: dict[str, Any],
) -> None:
    if not options.results_s3_prefix or not options.runtime_s3_prefix:
        raise ValueError("results_s3_prefix and runtime_s3_prefix are required for scheduled execute")

    run_id = options.run_id or _default_run_id(strategy)

    if options.event_calendar_path:
        generated_dir = Path(options.app_dir) / ".generated" / "scheduler"
        strategy_path = str(write_patched_strategy_yaml(options.strategy_path, strategy, generated_dir))
    else:
        strategy_path = options.strategy_path

    command = [
        sys.executable,
        "-m",
        "profiler.cli",
        "strategy",
        "execute",
        "--strategy",
        strategy_path,
        "--run-id",
        run_id,
        "--results-s3-prefix",
        options.results_s3_prefix,
        "--runtime-s3-prefix",
        options.runtime_s3_prefix,
        "--apply",
        "--teardown-mode",
        options.teardown_mode,
        "--diagnostics",
        options.diagnostics,
        "--execution-handoff",
        options.execution_handoff,
        "--execution-lead-seconds",
        str(options.execution_lead_seconds),
    ]
    if options.secrets_prefix:
        command.extend(["--secrets-prefix", options.secrets_prefix])

    result = subprocess.run(
        command,
        cwd=options.app_dir,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"strategy execute failed: {detail}")


def _scheduler_env(options: SchedulerOptions) -> dict[str, str] | None:
    if not options.secrets_prefix:
        return None
    return load_kalshi_env_from_prefix(options.secrets_prefix)


def _strategy_event_time(strategy: StrategyProfile) -> datetime:
    return relative_event_time(strategy.event_time, "T+0")


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _event_id_for_strategy(strategy: StrategyProfile) -> str:
    return str(strategy.strategy_metadata.get("event_id", strategy.name))


def _migrate_state_if_needed(state: dict[str, Any], event_id: str) -> dict[str, Any]:
    """Migrate old flat completed_offsets state to event-keyed structure."""
    if "completed_offsets" in state and "events" not in state:
        old_offsets = [str(item) for item in state.get("completed_offsets", []) if isinstance(item, str)]
        return {"events": {event_id: {"completed_offsets": old_offsets}}, "failures": {}}
    if "events" not in state:
        return {"events": {}, "failures": {}}
    if "failures" not in state:
        state["failures"] = {}
    return state


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"events": {}, "failures": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"events": {}, "failures": {}}
    if not isinstance(data, dict):
        return {"events": {}, "failures": {}}
    if "events" not in data or not isinstance(data.get("events"), dict):
        data["events"] = {}
    if "failures" not in data or not isinstance(data.get("failures"), dict):
        data["failures"] = {}
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _mark_offset_complete(state: dict[str, Any], event_id: str, offset: str) -> None:
    events = state.setdefault("events", {})
    event_state = events.setdefault(event_id, {"completed_offsets": []})
    completed = list(event_state.get("completed_offsets", []))
    if offset not in completed:
        completed.append(offset)
    event_state["completed_offsets"] = completed


def _record_failure(
    state: dict[str, Any],
    *,
    event_id: str,
    action_type: str,
    offset: str | None,
    error: str,
) -> tuple[bool, dict[str, Any]]:
    key = f"{event_id}:{action_type}:{offset or ''}"
    failures = state.setdefault("failures", {})
    entry = failures.get(key)
    repeated = bool(entry and entry.get("last_error") == error)
    repeat_count = int(entry.get("repeat_count", 0)) + 1 if repeated else 1
    failures[key] = {"last_error": error, "repeat_count": repeat_count}
    should_log = not repeated or repeat_count % REPEATED_FAILURE_LOG_EVERY == 0
    return should_log, {"repeated": repeated, "repeat_count": repeat_count}


def _emit_scheduler_failure(*, action: dict[str, Any], event_id: str, error: str, details: dict[str, Any]) -> None:
    offset = action.get("offset")
    suffix = f" (repeat #{details['repeat_count']})" if details["repeated"] else ""
    print(
        f"scheduler failure: event_id={event_id} action={action['type']} offset={offset}{suffix} error={error}",
        file=sys.stderr,
        flush=True,
    )


def _write_scheduler_event(path: Path, *, stage: str, status: str, **details: Any) -> None:
    event = {
        "schema_version": 1,
        "stage": stage,
        "status": status,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "details": details,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True))
        handle.write("\n")


def _default_run_id(strategy: StrategyProfile) -> str:
    event_id = strategy.strategy_metadata.get("event_id", strategy.name)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{event_id}-{stamp}"
