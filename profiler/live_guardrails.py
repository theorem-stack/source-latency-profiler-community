from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from profiler.strategy import StrategyProfile


def build_live_policy_report(
    strategy: StrategyProfile,
    *,
    allow_live: bool,
    max_capital_usd: float,
    artifact_image: str | None = None,
    kill_switch_file: str | None = None,
) -> dict[str, Any]:
    live_requested = _live_requested(strategy)
    checks = [
        {
            "name": "live_requested",
            "passed": live_requested,
            "required_for_live": False,
        },
        {
            "name": "cli_allow_live",
            "passed": (not live_requested) or allow_live,
            "required_for_live": True,
        },
        {
            "name": "profile_live_approved",
            "passed": (not live_requested) or bool(strategy.deployment.get("live_approved")),
            "required_for_live": True,
        },
        {
            "name": "capital_cap",
            "passed": (not live_requested) or _capital_usd(strategy) <= max_capital_usd,
            "required_for_live": True,
            "actual_usd": _capital_usd(strategy),
            "cap_usd": max_capital_usd,
        },
        {
            "name": "kill_switch_clear",
            "passed": not _kill_switch_active(kill_switch_file),
            "required_for_live": True,
        },
        {
            "name": "immutable_image",
            "passed": (not live_requested) or _immutable_image(artifact_image),
            "required_for_live": True,
            "image": artifact_image,
        },
    ]
    failures = [check["name"] for check in checks if check["required_for_live"] and not check["passed"]]
    return {
        "schema_version": 1,
        "strategy": strategy.name,
        "live_requested": live_requested,
        "decision": "allow" if not failures else "deny",
        "checks": checks,
        "failures": failures,
    }


def enforce_live_policy(report: dict[str, Any]) -> None:
    if report["live_requested"] and report["decision"] != "allow":
        raise RuntimeError(f"live policy denied execution: {', '.join(report['failures'])}")


def write_reconciliation_report(path: str | Path, *, run_id: str, execution_status: dict[str, Any] | None) -> None:
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "execution_status": (execution_status or {}).get("status"),
        "command_id": (execution_status or {}).get("command_id"),
        "exit_code": (execution_status or {}).get("exit_code"),
        "order_observed": _last_json_object((execution_status or {}).get("output", "")),
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _live_requested(strategy: StrategyProfile) -> bool:
    values = [
        strategy.execution.get("mode"),
        strategy.deployment.get("mode"),
        strategy.strategy_metadata.get("mode"),
    ]
    return any(str(value).lower() == "live" for value in values if value is not None)


def _capital_usd(strategy: StrategyProfile) -> float:
    return float(strategy.execution.get("release_capital_usd") or strategy.execution.get("max_capital_usd") or 0.0)


def _kill_switch_active(kill_switch_file: str | None) -> bool:
    env_value = os.environ.get("SOURCE_LATENCY_PROFILER_KILL_SWITCH", "").lower()
    if env_value in {"1", "true", "yes", "on"}:
        return True
    return bool(kill_switch_file and Path(kill_switch_file).exists())


def _immutable_image(image: str | None) -> bool:
    if not image:
        return False
    if "@sha256:" in image:
        return True
    tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else ""
    return bool(tag and tag != "latest")


def _last_json_object(output: str) -> dict[str, Any] | None:
    parsed = None
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
    return parsed
