from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from profiler.markets.config import MarketsConfig, parse_markets_config
from profiler.markets.discover import MarketsDiscoveryError, run_nullone_discover
from profiler.markets.import_spec import import_nullone_spec
from profiler.strategy import StrategyProfile, load_strategy_profile


def resolve_markets(
    strategy: StrategyProfile,
    *,
    markets_config: MarketsConfig | None = None,
    env: dict[str, str] | None = None,
    audit_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Discover via NullOne and import brackets (mandatory for nullone_discover source)."""
    config = markets_config or parse_markets_config(strategy.markets)

    if config.source == "pinned" and config.pinned_brackets:
        return {
            "brackets": config.pinned_brackets,
            "source": "pinned",
            "imported_at": None,
            "spec_path": None,
        }

    discover_key_field = config.discover_key_field
    discover_key = str(strategy.event.get(discover_key_field, "")).strip()
    if not discover_key:
        raise ValueError(
            f"event.{discover_key_field} is required for nullone_discover markets "
            f"(configure markets.nullone.discover_key_field to use a different event field)"
        )

    spec_path = run_nullone_discover(config, discover_key=discover_key, env=env)

    # Only validate week_ending in the spec when the strategy is keyed on that field,
    # preserving the integrity check for WNGSR while not breaking other strategies.
    expected_week_ending = discover_key if discover_key_field == "week_ending" else None
    markets = import_nullone_spec(spec_path, expected_week_ending=expected_week_ending)
    markets["source"] = "nullone_discover"

    if audit_dir is not None:
        _write_audit_copy(markets, spec_path, audit_dir, strategy_name=strategy.name, discover_key=discover_key)

    return markets


def resolve_strategy_profile(
    path: str | Path,
    *,
    env: dict[str, str] | None = None,
    audit_dir: str | Path | None = None,
    skip_discovery: bool = False,
) -> StrategyProfile:
    strategy = load_strategy_profile(path)
    config = parse_markets_config(strategy.markets)

    if skip_discovery and config.source == "pinned":
        return strategy

    if config.source == "pinned" and config.pinned_brackets:
        return strategy

    resolved = resolve_markets(
        strategy,
        markets_config=config,
        env=env,
        audit_dir=audit_dir,
    )
    return replace(strategy, markets=resolved)


def _write_audit_copy(
    markets: dict[str, Any],
    spec_path: Path,
    audit_dir: str | Path,
    *,
    strategy_name: str,
    discover_key: str,
) -> None:
    output = Path(audit_dir)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{strategy_name}_{discover_key}"
    audit_spec = output / f"{stem}.yaml"
    shutil.copy2(spec_path, audit_spec)
    summary_path = output / f"{stem}-markets.json"
    summary_path.write_text(json.dumps(markets, indent=2, sort_keys=True) + "\n", encoding="utf-8")
