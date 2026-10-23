from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from profiler.strategy import StrategyProfile


@dataclass(frozen=True)
class PolicyDecision:
    auto_deploy_allowed: bool
    minimum_samples_met: bool
    minimum_confidence_met: bool
    minimum_score_margin_met: bool
    before_latest_deploy_cutoff: bool
    decision: str
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "auto_deploy_allowed": self.auto_deploy_allowed,
            "minimum_samples_met": self.minimum_samples_met,
            "minimum_confidence_met": self.minimum_confidence_met,
            "minimum_score_margin_met": self.minimum_score_margin_met,
            "before_latest_deploy_cutoff": self.before_latest_deploy_cutoff,
            "decision": self.decision,
            "reasons": self.reasons,
        }


def evaluate_policy(
    strategy: StrategyProfile,
    recommendation: dict[str, Any],
    *,
    now: datetime | None = None,
) -> PolicyDecision:
    auto_deploy_allowed = strategy.automation.allow_auto_deploy
    minimum_samples_met = bool(recommendation.get("sample_window", {}).get("minimum_samples_met"))
    confidence = float(recommendation.get("confidence") or 0.0)
    minimum_confidence_met = confidence >= strategy.objective.minimum_confidence
    score_margin_pct = _score_margin_pct(recommendation)
    minimum_score_margin_met = score_margin_pct >= strategy.objective.minimum_score_margin_pct
    before_latest_deploy_cutoff = _before_cutoff(strategy, now or datetime.now(_event_time(strategy).tzinfo))

    checks = {
        "auto deploy is not enabled": auto_deploy_allowed,
        "minimum sample threshold is not met": minimum_samples_met,
        "minimum confidence threshold is not met": minimum_confidence_met,
        "minimum score margin threshold is not met": minimum_score_margin_met,
        "latest deploy cutoff has passed": before_latest_deploy_cutoff,
    }
    reasons = [reason for reason, passed in checks.items() if not passed]
    decision = "deploy" if not reasons else _fallback_decision(strategy)

    return PolicyDecision(
        auto_deploy_allowed=auto_deploy_allowed,
        minimum_samples_met=minimum_samples_met,
        minimum_confidence_met=minimum_confidence_met,
        minimum_score_margin_met=minimum_score_margin_met,
        before_latest_deploy_cutoff=before_latest_deploy_cutoff,
        decision=decision,
        reasons=reasons,
    )


def _score_margin_pct(recommendation: dict[str, Any]) -> float:
    score = recommendation.get("score")
    alternates = recommendation.get("alternates") or []
    if score in {None, 0}:
        return 0.0
    if not alternates:
        return 100.0
    alternate_score = alternates[0].get("score")
    if alternate_score is None:
        return 0.0
    return max(0.0, ((float(alternate_score) - float(score)) / float(score)) * 100)


def _fallback_decision(strategy: StrategyProfile) -> str:
    mode = strategy.automation.fallback.get("mode", "abort")
    if mode == "deploy_default_region":
        return "deploy_fallback"
    if mode == "deploy_multiple_standbys":
        return "deploy_standbys"
    return "abort"


def _before_cutoff(strategy: StrategyProfile, now: datetime) -> bool:
    cutoff = relative_event_time(strategy.event_time, strategy.automation.latest_deploy_cutoff)
    comparable_now = now if now.tzinfo else now.replace(tzinfo=cutoff.tzinfo)
    return comparable_now <= cutoff


def relative_event_time(event_time: str, expression: str) -> datetime:
    event = _event_time_from_string(event_time)
    if expression == "T+0":
        return event
    if not expression.startswith("T") or len(expression) < 4:
        raise ValueError(f"Invalid relative time expression: {expression}")
    sign = expression[1]
    amount = expression[2:]
    delta = _duration(amount)
    if sign == "-":
        return event - delta
    if sign == "+":
        return event + delta
    raise ValueError(f"Invalid relative time expression: {expression}")


def _event_time(strategy: StrategyProfile) -> datetime:
    return _event_time_from_string(strategy.event_time)


def _event_time_from_string(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _duration(value: str) -> timedelta:
    unit = value[-1]
    amount = int(value[:-1])
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    raise ValueError(f"Unsupported duration unit in {value}")
