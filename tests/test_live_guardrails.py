import pytest

from profiler.live_guardrails import build_live_policy_report, enforce_live_policy
from profiler.strategy import AutomationPolicy, Objective, StrategyProfile


def test_live_policy_denies_live_without_explicit_approval() -> None:
    strategy = _strategy(mode="live", live_approved=False, capital=25.0)

    report = build_live_policy_report(
        strategy,
        allow_live=False,
        max_capital_usd=100.0,
        artifact_image="repo/nullone:latest",
    )

    assert report["decision"] == "deny"
    assert "cli_allow_live" in report["failures"]
    assert "profile_live_approved" in report["failures"]
    assert "immutable_image" in report["failures"]
    with pytest.raises(RuntimeError, match="live policy denied"):
        enforce_live_policy(report)


def test_live_policy_allows_approved_live_under_cap() -> None:
    strategy = _strategy(mode="live", live_approved=True, capital=25.0)

    report = build_live_policy_report(
        strategy,
        allow_live=True,
        max_capital_usd=100.0,
        artifact_image="repo/nullone:build-123",
    )

    assert report["decision"] == "allow"
    enforce_live_policy(report)


def _strategy(*, mode: str, live_approved: bool, capital: float) -> StrategyProfile:
    return StrategyProfile(
        name="nullone",
        event_time="2026-05-30T12:00:00+00:00",
        description=None,
        schema_version=1,
        strategy_metadata={},
        event={},
        markets={},
        execution={"mode": mode, "release_capital_usd": capital},
        source_targets=[],
        venue_targets=[],
        candidate_locations={},
        objective=Objective(weights={}),
        automation=AutomationPolicy(),
        deployment={"live_approved": live_approved},
    )
