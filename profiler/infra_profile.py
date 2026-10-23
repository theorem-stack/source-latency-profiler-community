from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from profiler.artifacts import validate_artifact
from profiler.policy import evaluate_policy
from profiler.strategy import StrategyProfile


def build_infrastructure_recommendation(
    strategy: StrategyProfile,
    recommendation: dict[str, Any],
    *,
    run_id: str,
    now: datetime | None = None,
    artifact_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generated_at = now or datetime.now(UTC)
    policy_decision = evaluate_policy(strategy, recommendation, now=generated_at)
    selected_location = _selected_location(strategy, recommendation, policy_decision.decision)
    provider = selected_location["provider"]
    region = selected_location["region"]
    provider_config = strategy.candidate_locations.get(provider, {})
    artifact = artifact_override or strategy.deployment.get("artifact", {})
    validate_artifact(artifact)

    return {
        "schema_version": 1,
        "strategy": strategy.name,
        "run_id": run_id,
        "event_time": strategy.event_time,
        "recommended_infrastructure": {
            "provider": provider,
            "region": region,
            "runtime": strategy.deployment.get("runtime", "ec2"),
            "instance_type": _instance_type(provider_config),
            "network": {
                "public_ip": bool(strategy.deployment.get("require_public_ip", False)),
                "placement": strategy.deployment.get("placement", "single_az_low_latency"),
            },
            "artifact": artifact,
            "startup_command": strategy.deployment.get("startup_command"),
            "execution_command": strategy.deployment.get("execution_command"),
        },
        "confidence": recommendation.get("confidence", 0.0),
        "policy_decision": policy_decision.to_dict(),
        "fallback": strategy.automation.fallback,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
    }


def write_infrastructure_recommendation(path: str | Path, recommendation: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(recommendation, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _selected_location(strategy: StrategyProfile, recommendation: dict[str, Any], decision: str) -> dict[str, str]:
    if decision == "deploy_fallback":
        fallback = strategy.automation.fallback
        if fallback.get("provider") and fallback.get("region"):
            return {"provider": fallback["provider"], "region": fallback["region"]}

    recommended = recommendation.get("recommended_location")
    if recommended:
        return {"provider": recommended["provider"], "region": recommended["region"]}

    fallback = strategy.automation.fallback
    if fallback.get("provider") and fallback.get("region"):
        return {"provider": fallback["provider"], "region": fallback["region"]}

    for provider, config in strategy.candidate_locations.items():
        regions = config.get("regions") or []
        if regions:
            return {"provider": provider, "region": regions[0]}

    raise ValueError("No recommended or fallback location is available")


def _instance_type(provider_config: dict[str, Any]) -> str | None:
    instance_types = provider_config.get("instance_types") or []
    return instance_types[0] if instance_types else None
