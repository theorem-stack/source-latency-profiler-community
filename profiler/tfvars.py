from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from profiler.artifacts import validate_artifact
from profiler.storage import parse_s3_uri

RUNTIME_ENV_SECRET_SUFFIX = "runtime/env"
KALSHI_PRIVATE_KEY_SECRET_SUFFIX = "kalshi/private_key_pem"


def build_strategy_runtime_tfvars(
    infrastructure_recommendation: dict[str, Any],
    *,
    runtime_config_s3_uri: str,
    secret_arns: list[str] | None = None,
    secret_names: list[str] | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    bucket, key = parse_s3_uri(runtime_config_s3_uri)
    infrastructure = infrastructure_recommendation["recommended_infrastructure"]
    network = infrastructure.get("network", {})
    artifact = infrastructure.get("artifact", {})
    validate_artifact(artifact)

    return {
        "aws_region": infrastructure["region"],
        "create_results_bucket": False,
        "results_bucket_name": bucket,
        "runner_count": 0,
        "strategy_runtime_enabled": True,
        "run_id": infrastructure_recommendation["run_id"],
        "strategy_name": infrastructure_recommendation["strategy"],
        "strategy_instance_type": infrastructure.get("instance_type") or "c7i.large",
        "strategy_artifact_type": artifact.get("type", "container"),
        "strategy_artifact_image": artifact.get("image", ""),
        "strategy_runtime_config_bucket": bucket,
        "strategy_runtime_config_key": key,
        "strategy_startup_command": infrastructure.get("startup_command") or "nullone ready --config /etc/nullone/runtime.json",
        "strategy_execution_command": infrastructure.get("execution_command")
        or "nullone execute --config /etc/nullone/runtime.json",
        "strategy_require_public_ip": bool(network.get("public_ip", False)),
        "strategy_create_private_endpoints": not bool(network.get("public_ip", False)),
        "strategy_secret_arns": secret_arns or [],
        "strategy_runtime_env_secret_id": _secret_name_by_suffix(secret_names or [], RUNTIME_ENV_SECRET_SUFFIX),
        "strategy_private_key_secret_id": _secret_name_by_suffix(secret_names or [], KALSHI_PRIVATE_KEY_SECRET_SUFFIX),
        "strategy_runtime_expires_at": expires_at or "",
    }


def write_tfvars(path: str | Path, tfvars: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(tfvars, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _secret_name_by_suffix(secret_names: list[str], suffix: str) -> str:
    for name in secret_names:
        if name.endswith(suffix):
            return name
    return ""
