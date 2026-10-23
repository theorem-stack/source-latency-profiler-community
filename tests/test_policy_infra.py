from datetime import datetime

from profiler.infra_profile import build_infrastructure_recommendation
from profiler.policy import evaluate_policy, relative_event_time
from profiler.strategy import load_strategy_profile


def test_relative_event_time() -> None:
    assert relative_event_time("2026-05-22T10:30:00-04:00", "T-3m").isoformat() == "2026-05-22T10:27:00-04:00"
    assert relative_event_time("2026-05-22T10:30:00-04:00", "T+0").isoformat() == "2026-05-22T10:30:00-04:00"
    assert relative_event_time("2026-05-22T10:30:00-04:00", "T-7d").isoformat() == "2026-05-15T10:30:00-04:00"


def test_policy_allows_deploy_when_gates_pass() -> None:
    strategy = load_strategy_profile("strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml")
    recommendation = _recommendation(confidence=0.9, minimum_samples_met=True, score=10, alternate_score=12)

    decision = evaluate_policy(
        strategy,
        recommendation,
        now=datetime.fromisoformat("2026-05-22T10:20:00-04:00"),
    )

    assert decision.decision == "deploy"
    assert decision.reasons == []


def test_policy_uses_fallback_when_confidence_fails() -> None:
    strategy = load_strategy_profile("strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml")
    recommendation = _recommendation(confidence=0.5, minimum_samples_met=True, score=10, alternate_score=12)

    decision = evaluate_policy(
        strategy,
        recommendation,
        now=datetime.fromisoformat("2026-05-22T10:20:00-04:00"),
    )

    assert decision.decision == "deploy_fallback"
    assert "minimum confidence threshold is not met" in decision.reasons


def test_build_infrastructure_recommendation() -> None:
    strategy = load_strategy_profile("strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml")
    recommendation = _recommendation(confidence=0.9, minimum_samples_met=True, score=10, alternate_score=12)

    infra = build_infrastructure_recommendation(
        strategy,
        recommendation,
        run_id="run-123",
        now=datetime.fromisoformat("2026-05-22T10:20:00-04:00"),
    )

    assert infra["recommended_infrastructure"]["provider"] == "aws"
    assert infra["recommended_infrastructure"]["region"] == "us-east-1"
    assert infra["recommended_infrastructure"]["instance_type"] == "c7i.large"
    assert infra["policy_decision"]["decision"] == "deploy"


def test_infrastructure_recommendation_uses_fallback_location_when_policy_fails() -> None:
    strategy = load_strategy_profile("strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml")
    recommendation = _recommendation(confidence=0.5, minimum_samples_met=False, score=10, alternate_score=12)

    infra = build_infrastructure_recommendation(
        strategy,
        recommendation,
        run_id="run-123",
        now=datetime.fromisoformat("2026-05-22T10:20:00-04:00"),
    )

    assert infra["policy_decision"]["decision"] == "deploy_fallback"
    assert infra["recommended_infrastructure"]["provider"] == "aws"
    assert infra["recommended_infrastructure"]["region"] == "us-east-1"
    assert infra["recommended_infrastructure"]["instance_type"] == "c7i.large"


def _recommendation(
    *,
    confidence: float,
    minimum_samples_met: bool,
    score: float,
    alternate_score: float,
) -> dict:
    return {
        "recommended_location": {"provider": "aws", "region": "us-east-1"},
        "confidence": confidence,
        "score": score,
        "alternates": [{"provider": "aws", "region": "us-east-2", "score": alternate_score}],
        "sample_window": {"minimum_samples_met": minimum_samples_met},
    }
