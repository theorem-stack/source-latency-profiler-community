from datetime import UTC, datetime, timedelta

import pytest

from profiler.execution_handoff import (
    execute_at_from_runtime_config,
    normalize_execution_handoff,
    wait_until_lead_window,
)


def test_normalize_execution_handoff_accepts_known_modes() -> None:
    assert normalize_execution_handoff("host-timer") == "host-timer"
    assert normalize_execution_handoff("SSM-LEAD") == "ssm-lead"


def test_normalize_execution_handoff_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unsupported execution handoff mode"):
        normalize_execution_handoff("cron")


def test_execute_at_from_runtime_config() -> None:
    execute_at = execute_at_from_runtime_config(
        {"runtime": {"execute_at": "2026-05-22T14:30:00+00:00"}}
    )
    assert execute_at == datetime(2026, 5, 22, 14, 30, tzinfo=UTC)


def test_wait_until_lead_window_sleeps_until_lead_time(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("profiler.execution_handoff.time.sleep", lambda seconds: sleeps.append(seconds))
    fixed_now = datetime(2026, 5, 22, 14, 28, 0, tzinfo=UTC)
    monkeypatch.setattr("profiler.execution_handoff.datetime", type("dt", (), {"now": staticmethod(lambda tz=None: fixed_now)}))
    execute_at = datetime(2026, 5, 22, 14, 30, 0, tzinfo=UTC)
    slept = wait_until_lead_window(execute_at, lead_seconds=60)
    assert slept == pytest.approx(60.0)
    assert sleeps == [pytest.approx(60.0)]
