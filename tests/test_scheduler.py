from datetime import UTC, datetime

from profiler.markets.config import parse_markets_config
from profiler.scheduler import _mark_offset_complete, _migrate_state_if_needed, _record_failure, evaluate_due_actions
from profiler.strategy import load_strategy_profile

PINNED_STRATEGY = "strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml"
EVENT_ID = "eia_wngsr_2026-05-15"


def test_evaluate_due_actions_includes_refresh_and_warmup() -> None:
    strategy = load_strategy_profile(PINNED_STRATEGY)
    markets_config = parse_markets_config(strategy.markets)
    now = datetime.fromisoformat("2026-05-22T10:29:00-04:00").astimezone(UTC)

    actions = evaluate_due_actions(strategy, state={}, now=now)
    types = {action["type"] for action in actions}

    assert "market_refresh" in types
    assert "strategy_execute" in types
    assert len([action for action in actions if action["type"] == "market_refresh"]) == len(
        markets_config.refresh_offsets
    )


def test_evaluate_due_actions_skips_completed_offsets_new_format() -> None:
    """New event-keyed state format is respected."""
    strategy = load_strategy_profile(PINNED_STRATEGY)
    now = datetime.fromisoformat("2026-05-22T10:29:00-04:00").astimezone(UTC)
    state = {
        "events": {
            EVENT_ID: {"completed_offsets": ["T-7d", "T-3d", "T-1d", "T-6h", "T-30m"]},
        }
    }

    actions = evaluate_due_actions(strategy, state=state, now=now)
    assert actions == []


def test_evaluate_due_actions_skips_completed_offsets_legacy_format() -> None:
    """Old flat completed_offsets state is migrated and respected."""
    strategy = load_strategy_profile(PINNED_STRATEGY)
    now = datetime.fromisoformat("2026-05-22T10:29:00-04:00").astimezone(UTC)
    state = {"completed_offsets": ["T-7d", "T-3d", "T-1d", "T-6h", "T-30m"]}

    actions = evaluate_due_actions(strategy, state=state, now=now)
    assert actions == []


def test_evaluate_due_actions_other_event_does_not_suppress_actions() -> None:
    """Completed offsets for a different event_id do not block the current event."""
    strategy = load_strategy_profile(PINNED_STRATEGY)
    now = datetime.fromisoformat("2026-05-22T10:29:00-04:00").astimezone(UTC)
    state = {
        "events": {
            "eia_wngsr_2026-05-08": {"completed_offsets": ["T-7d", "T-3d", "T-1d", "T-6h", "T-30m"]},
        }
    }

    actions = evaluate_due_actions(strategy, state=state, now=now)
    types = {action["type"] for action in actions}
    assert "market_refresh" in types
    assert "strategy_execute" in types


def test_migrate_state_if_needed_legacy() -> None:
    old = {"completed_offsets": ["T-7d", "T-30m"]}
    migrated = _migrate_state_if_needed(old, "cpi_2026-07")
    assert migrated == {"events": {"cpi_2026-07": {"completed_offsets": ["T-7d", "T-30m"]}}, "failures": {}}


def test_migrate_state_if_needed_already_new_format() -> None:
    new = {"events": {"cpi_2026-07": {"completed_offsets": ["T-7d"]}}}
    result = _migrate_state_if_needed(new, "cpi_2026-07")
    assert result is new


def test_migrate_state_if_needed_empty() -> None:
    result = _migrate_state_if_needed({}, "cpi_2026-07")
    assert result == {"events": {}, "failures": {}}


def test_mark_offset_complete_creates_event_entry() -> None:
    state: dict = {"events": {}}
    _mark_offset_complete(state, "cpi_2026-07", "T-7d")
    assert state == {"events": {"cpi_2026-07": {"completed_offsets": ["T-7d"]}}}


def test_mark_offset_complete_idempotent() -> None:
    state: dict = {"events": {"cpi_2026-07": {"completed_offsets": ["T-7d"]}}}
    _mark_offset_complete(state, "cpi_2026-07", "T-7d")
    assert state["events"]["cpi_2026-07"]["completed_offsets"] == ["T-7d"]


def test_mark_offset_complete_separate_events_are_independent() -> None:
    state: dict = {"events": {}}
    _mark_offset_complete(state, "cpi_2026-07", "T-30m")
    _mark_offset_complete(state, "cpi_2026-08", "T-7d")
    assert state["events"]["cpi_2026-07"]["completed_offsets"] == ["T-30m"]
    assert state["events"]["cpi_2026-08"]["completed_offsets"] == ["T-7d"]


def test_record_failure_dedupes_repeated_errors() -> None:
    state: dict = {"events": {}, "failures": {}}
    should_log_first, first = _record_failure(
        state,
        event_id="wngsr_2026-06-12",
        action_type="market_refresh",
        offset="T-7d",
        error="discover failed",
    )
    assert should_log_first is True
    assert first == {"repeated": False, "repeat_count": 1}

    should_log_second, second = _record_failure(
        state,
        event_id="wngsr_2026-06-12",
        action_type="market_refresh",
        offset="T-7d",
        error="discover failed",
    )
    assert should_log_second is False
    assert second == {"repeated": True, "repeat_count": 2}
