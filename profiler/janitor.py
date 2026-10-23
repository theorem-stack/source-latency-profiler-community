from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeCleanupCandidate:
    run_id: str
    instance_id: str
    strategy: str
    expires_at: str
    state: str

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "strategy": self.strategy,
            "expires_at": self.expires_at,
            "state": self.state,
        }


def find_expired_strategy_runtimes(
    *,
    region: str,
    now: datetime | None = None,
    project_name: str | None = None,
) -> list[RuntimeCleanupCandidate]:
    client = _boto3_client("ec2", region)
    filters = [
        {"Name": "tag:Component", "Values": ["strategy-runtime"]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ]
    if project_name:
        filters.append({"Name": "tag:Project", "Values": [project_name]})
    response = client.describe_instances(Filters=filters)
    cutoff = now or datetime.now(UTC)
    candidates: list[RuntimeCleanupCandidate] = []
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            tags = _tag_map(instance.get("Tags", []))
            expires_at = tags.get("ExpiresAt", "")
            if not expires_at:
                continue
            expires = _parse_rfc3339(expires_at)
            if expires and expires <= cutoff:
                candidates.append(
                    RuntimeCleanupCandidate(
                        run_id=tags.get("RunId", ""),
                        instance_id=instance["InstanceId"],
                        strategy=tags.get("Strategy", "unknown"),
                        expires_at=expires_at,
                        state=instance.get("State", {}).get("Name", "unknown"),
                    )
                )
    return sorted(candidates, key=lambda candidate: (candidate.expires_at, candidate.run_id))


def run_strategy_runtime_janitor(
    *,
    region: str,
    app_dir: Path,
    dry_run: bool = True,
    project_name: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    from profiler.orchestrator import teardown_strategy_runtime

    actions: list[dict[str, Any]] = []
    for candidate in find_expired_strategy_runtimes(region=region, now=now, project_name=project_name):
        action = candidate.to_dict() | {"action": "would_teardown" if dry_run else "teardown"}
        actions.append(action)
        if not dry_run and candidate.run_id:
            teardown_strategy_runtime(candidate.run_id, app_dir=app_dir, yes=True)
    return actions


def _tag_map(tags: list[dict[str, str]]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in tags if "Key" in tag and "Value" in tag}


def _parse_rfc3339(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _boto3_client(service: str, region: str):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to enable janitor cleanup") from exc
    return boto3.client(service, region_name=region)
