from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from profiler.policy import relative_event_time
from profiler.strategy import StrategyProfile


def build_nullone_runtime_config(
    strategy: StrategyProfile,
    infrastructure_recommendation: dict[str, Any],
) -> dict[str, Any]:
    infrastructure = infrastructure_recommendation["recommended_infrastructure"]
    deployment = strategy.deployment

    return {
        "schema_version": 1,
        "engine": deployment.get("engine", "nullone"),
        "strategy": {
            "name": strategy.name,
            "event_id": strategy.strategy_metadata.get("event_id"),
            "release_at_et": strategy.strategy_metadata.get("release_at_et", strategy.event_time),
            "mode": strategy.strategy_metadata.get("mode", strategy.execution.get("mode")),
        },
        "event": strategy.event,
        "markets": strategy.markets,
        "execution": strategy.execution,
        "placement": {
            "provider": infrastructure["provider"],
            "region": infrastructure["region"],
            "runtime": infrastructure.get("runtime"),
            "instance_type": infrastructure.get("instance_type"),
            "network": infrastructure.get("network", {}),
        },
        "runtime": {
            "run_id": infrastructure_recommendation["run_id"],
            "event_time": strategy.event_time,
            "warmup_starts_at": _relative(strategy, strategy.automation.warmup_starts_at),
            "profiler_blackout_at": _relative(strategy, strategy.automation.profiler_stops_at),
            "latest_deploy_cutoff": _relative(strategy, strategy.automation.latest_deploy_cutoff),
            "execute_at": _relative(strategy, strategy.automation.execution_starts_at),
            "startup_command": infrastructure.get("startup_command"),
            "execution_command": infrastructure.get("execution_command"),
        },
        "sources": [_target_to_runtime(target) for target in strategy.source_targets],
        "venues": [_target_to_runtime(target) for target in strategy.venue_targets],
        "orchestration": {
            "confidence": infrastructure_recommendation.get("confidence"),
            "policy_decision": infrastructure_recommendation.get("policy_decision", {}),
            "generated_at": infrastructure_recommendation.get("generated_at"),
        },
    }


def write_runtime_config(path: str | Path, runtime_config: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(runtime_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _relative(strategy: StrategyProfile, expression: str) -> str:
    return relative_event_time(strategy.event_time, expression).isoformat()


def _target_to_runtime(target) -> dict[str, Any]:
    data = {
        "name": target.name,
        "type": target.type,
        "url": target.url,
        "method": target.method,
        "expected_status": target.expected_status,
        "timeout_ms": target.timeout_ms,
    }
    if target.subscription_message:
        data["subscription_message"] = target.subscription_message
    return {key: value for key, value in data.items() if value is not None}
