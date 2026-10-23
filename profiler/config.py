from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Target:
    name: str
    type: str
    url: str
    method: str = "GET"
    expected_status: int | None = 200
    timeout_ms: int = 2000
    headers: dict[str, str] = field(default_factory=dict)
    subscription_message: str | None = None


@dataclass(frozen=True)
class Runner:
    provider: str
    region: str
    runtime: str = "local"
    runner_id: str | None = None


@dataclass(frozen=True)
class PlacementProfile:
    name: str
    objective: str
    source_targets: list[str]
    venue_targets: list[str]
    weights: dict[str, float]
    minimum_samples: int = 1
    lookback: str | None = None

    @property
    def targets(self) -> set[str]:
        return set(self.source_targets) | set(self.venue_targets)


def load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping at top level in {path}")
    return loaded


def load_targets(path: str | Path) -> list[Target]:
    data = load_yaml(path)
    targets = data.get("targets", [])
    if not isinstance(targets, list):
        raise ValueError("targets must be a list")
    return [Target(**target) for target in targets]


def load_runners(path: str | Path | None) -> list[Runner]:
    if path is None:
        return [Runner(provider="local", region="local", runtime="local", runner_id="local")]

    data = load_yaml(path)
    runners = data.get("runners", [])
    if not isinstance(runners, list):
        raise ValueError("runners must be a list")
    return [Runner(**runner) for runner in runners]


def load_placement_profile(path: str | Path) -> PlacementProfile:
    data = load_yaml(path)
    profile = data.get("profile", data)
    if not isinstance(profile, dict):
        raise ValueError("profile must be a mapping")
    return PlacementProfile(**profile)
