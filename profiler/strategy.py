from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from profiler.config import PlacementProfile, Runner, Target, load_yaml


@dataclass(frozen=True)
class Objective:
    weights: dict[str, float]
    minimum_samples: int = 1
    minimum_confidence: float = 0.0
    minimum_score_margin_pct: float = 0.0
    lookback: str | None = None


@dataclass(frozen=True)
class AutomationPolicy:
    allow_auto_deploy: bool = False
    warmup_starts_at: str = "T-30m"
    profiler_stops_at: str = "T-2m"
    latest_deploy_cutoff: str = "T-3m"
    execution_starts_at: str = "T+0"
    auto_teardown: bool = True
    fallback: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyProfile:
    name: str
    event_time: str
    description: str | None
    schema_version: int
    strategy_metadata: dict[str, Any]
    event: dict[str, Any]
    markets: dict[str, Any]
    execution: dict[str, Any]
    source_targets: list[Target]
    venue_targets: list[Target]
    candidate_locations: dict[str, dict[str, Any]]
    objective: Objective
    automation: AutomationPolicy
    deployment: dict[str, Any]

    @property
    def targets(self) -> list[Target]:
        return [*self.source_targets, *self.venue_targets]

    @property
    def target_names(self) -> set[str]:
        return {target.name for target in self.targets}

    def to_target_config(self) -> dict[str, Any]:
        return {"targets": [_target_to_dict(target) for target in self.targets]}

    def to_runner_config(self) -> dict[str, Any]:
        runners: list[dict[str, str]] = []
        for provider, config in self.candidate_locations.items():
            for region in config.get("regions", []):
                runners.append(
                    {
                        "provider": provider,
                        "region": region,
                        "runtime": config.get("probe_runtime", "vm"),
                        "runner_id": f"{provider}-{region}",
                    }
                )
        return {"runners": runners}

    def to_placement_profile(self) -> PlacementProfile:
        return PlacementProfile(
            name=self.name,
            objective="source_to_venue",
            source_targets=[target.name for target in self.source_targets],
            venue_targets=[target.name for target in self.venue_targets],
            weights=self.objective.weights,
            minimum_samples=self.objective.minimum_samples,
            lookback=self.objective.lookback,
        )

    def to_placement_profile_config(self) -> dict[str, Any]:
        profile = self.to_placement_profile()
        return {
            "profile": {
                "name": profile.name,
                "objective": profile.objective,
                "source_targets": profile.source_targets,
                "venue_targets": profile.venue_targets,
                "weights": profile.weights,
                "minimum_samples": profile.minimum_samples,
                "lookback": profile.lookback,
            }
        }

    def to_runners(self) -> list[Runner]:
        return [Runner(**runner) for runner in self.to_runner_config()["runners"]]


def load_strategy_profile(path: str | Path, *, allow_template: bool = False) -> StrategyProfile:
    """Load a strategy profile from a YAML file.

    When allow_template=True, the profile may omit strategy.event_time and
    strategy.release_at_et. The caller is responsible for supplying timing via
    patch_strategy_event() before the profile is used for scheduling or execution.
    """
    data = load_yaml(path)
    strategy = data.get("strategy")
    if not isinstance(strategy, dict):
        raise ValueError("strategy profile must include a strategy mapping")

    targets = data.get("targets", {})
    if not isinstance(targets, dict):
        raise ValueError("strategy targets must be a mapping")

    objective = data.get("objective", {})
    automation = data.get("automation", {})
    candidate_locations = data.get("candidate_locations", {})
    deployment = data.get("deployment", {})
    event_time = strategy.get("event_time") or strategy.get("release_at_et") or ""
    if not event_time and not allow_template:
        raise ValueError("strategy profile must include strategy.event_time or strategy.release_at_et")

    return StrategyProfile(
        name=strategy["name"],
        description=strategy.get("description"),
        event_time=event_time,
        schema_version=int(data.get("schema_version", 1)),
        strategy_metadata=strategy,
        event=data.get("event", {}),
        markets=data.get("markets", {}),
        execution=data.get("execution", {}),
        source_targets=[Target(**target) for target in targets.get("sources", [])],
        venue_targets=[Target(**target) for target in targets.get("venues", [])],
        candidate_locations=candidate_locations,
        objective=Objective(**objective),
        automation=AutomationPolicy(**automation),
        deployment=deployment if isinstance(deployment, dict) else {},
    )


def patch_strategy_event(strategy: StrategyProfile, event_entry: dict[str, Any]) -> StrategyProfile:
    """Return a new StrategyProfile with timing and event fields from a calendar entry.

    event_entry must contain:
      - event_id: str
      - release_at_et: ISO-8601 datetime string
    Optional:
      - discover_key: str  (stored in event dict as per discover_key_field)
      - event_overrides: dict  (merged into the strategy.event block)
    """
    release_at = str(event_entry["release_at_et"])
    event_id = str(event_entry["event_id"])
    event_overrides = event_entry.get("event_overrides") or {}
    discover_key = event_entry.get("discover_key")

    new_metadata = {
        **strategy.strategy_metadata,
        "event_id": event_id,
        "event_time": release_at,
        "release_at_et": release_at,
    }
    new_event = {**strategy.event, **event_overrides}
    if discover_key is not None:
        new_event["discover_key"] = str(discover_key)

    return replace(strategy, event_time=release_at, strategy_metadata=new_metadata, event=new_event)


def write_patched_strategy_yaml(
    original_path: str | Path,
    strategy: StrategyProfile,
    output_dir: str | Path,
) -> Path:
    """Write a concrete YAML from a patched StrategyProfile for subprocess handoff.

    The returned path points to a self-contained YAML that includes the injected
    event_time and event fields, safe to pass to `strategy execute` without
    allow_template.
    """
    raw = load_yaml(original_path)
    strat_block = raw.setdefault("strategy", {})
    strat_block["event_id"] = strategy.strategy_metadata.get("event_id", strategy.name)
    strat_block["event_time"] = strategy.event_time
    strat_block["release_at_et"] = strategy.event_time
    raw["event"] = dict(strategy.event)
    event_id = strat_block["event_id"]
    out = Path(output_dir) / f"strategy-{event_id}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml(out, raw)
    return out


def write_strategy_artifacts(strategy: StrategyProfile, output_dir: str | Path) -> dict[str, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "targets": output / "targets.yaml",
        "runners": output / "runners.yaml",
        "placement_profile": output / "placement-profile.yaml",
    }
    _write_yaml(artifacts["targets"], strategy.to_target_config())
    _write_yaml(artifacts["runners"], strategy.to_runner_config())
    _write_yaml(artifacts["placement_profile"], strategy.to_placement_profile_config())
    return artifacts


def strategy_summary(strategy: StrategyProfile) -> str:
    runner_count = len(strategy.to_runner_config()["runners"])
    event_id = strategy.strategy_metadata.get("event_id", "-")
    return "\n".join(
        [
            f"strategy: {strategy.name}",
            f"event_id: {event_id}",
            f"event_time: {strategy.event_time}",
            f"market_brackets: {len(strategy.markets.get('brackets', []))} ({strategy.markets.get('source', 'unknown')})",
            f"sources: {len(strategy.source_targets)}",
            f"venues: {len(strategy.venue_targets)}",
            f"candidate_runners: {runner_count}",
            f"auto_deploy: {strategy.automation.allow_auto_deploy}",
            f"minimum_confidence: {strategy.objective.minimum_confidence}",
            f"profiler_blackout: {strategy.automation.profiler_stops_at}",
        ]
    )


def _target_to_dict(target: Target) -> dict[str, Any]:
    data = {
        "name": target.name,
        "type": target.type,
        "url": target.url,
        "method": target.method,
        "expected_status": target.expected_status,
        "timeout_ms": target.timeout_ms,
    }
    if target.headers:
        data["headers"] = target.headers
    if target.subscription_message:
        data["subscription_message"] = target.subscription_message
    return {key: value for key, value in data.items() if value is not None}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
