from pathlib import Path

import pytest

from profiler.markets.config import parse_markets_config
from profiler.strategy import load_strategy_profile, patch_strategy_event, strategy_summary, write_strategy_artifacts

PINNED_STRATEGY = "strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml"


def test_load_strategy_profile_nullone_discover() -> None:
    strategy = load_strategy_profile("strategies/nullone-wngsr-kalshi.yaml", allow_template=True)

    assert strategy.name == "nullone_wngsr_kalshi"
    assert strategy.event_time == ""
    assert "week_ending" not in strategy.event
    config = parse_markets_config(strategy.markets)
    assert config.source == "nullone_discover"
    assert config.requires_discovery
    assert config.series_ticker == "KXGAS"
    assert "T-7d" in config.refresh_offsets


def test_load_strategy_profile_pinned() -> None:
    strategy = load_strategy_profile(PINNED_STRATEGY)

    assert strategy.strategy_metadata["event_id"] == "eia_wngsr_2026-05-15"
    assert len(strategy.markets["brackets"]) == 3
    assert strategy.execution["mode"] == "paper"


def test_strategy_derives_configs(tmp_path: Path) -> None:
    strategy = load_strategy_profile(PINNED_STRATEGY)

    target_config = strategy.to_target_config()
    runner_config = strategy.to_runner_config()
    placement_profile = strategy.to_placement_profile()
    artifacts = write_strategy_artifacts(strategy, tmp_path)

    assert len(target_config["targets"]) == 4
    assert len(runner_config["runners"]) == 1  # pinned fixture uses single region
    assert placement_profile.source_targets == ["eia_wngsr_json", "eia_wngsr_csv"]
    assert artifacts["targets"].exists()
    assert artifacts["runners"].exists()
    assert artifacts["placement_profile"].exists()


def test_strategy_summary_includes_operational_gates() -> None:
    strategy = load_strategy_profile(PINNED_STRATEGY)
    summary = strategy_summary(strategy)

    assert "strategy: nullone_wngsr_kalshi" in summary
    assert "market_brackets: 3 (pinned)" in summary
    assert "auto_deploy: True" in summary


def test_load_strategy_profile_requires_event_time_by_default() -> None:
    """Loading a template YAML without event_time raises without allow_template."""
    import yaml

    content = yaml.safe_dump(
        {
            "schema_version": 1,
            "strategy": {"name": "test_strategy"},
            "targets": {},
            "objective": {"weights": {"source_ttfb_p50": 1.0}},
            "automation": {},
        }
    )
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
        fh.write(content)
        tmp = fh.name

    with pytest.raises(ValueError, match="strategy.event_time"):
        load_strategy_profile(tmp)


def test_patch_strategy_event_overrides_timing_and_event_fields() -> None:
    strategy = load_strategy_profile(PINNED_STRATEGY)
    entry = {
        "event_id": "cpi_2026-07",
        "release_at_et": "2026-07-11T08:30:00-04:00",
        "discover_key": "2026-07",
        "event_overrides": {"release_month": "2026-07"},
    }

    patched = patch_strategy_event(strategy, entry)

    assert patched.event_time == "2026-07-11T08:30:00-04:00"
    assert patched.strategy_metadata["event_id"] == "cpi_2026-07"
    assert patched.strategy_metadata["release_at_et"] == "2026-07-11T08:30:00-04:00"
    assert patched.event["release_month"] == "2026-07"
    assert patched.event["discover_key"] == "2026-07"
    # Original event fields are preserved
    assert patched.event["week_ending"] == strategy.event["week_ending"]


def test_patch_strategy_event_without_discover_key() -> None:
    strategy = load_strategy_profile(PINNED_STRATEGY)
    entry = {"event_id": "nfp_2026-07", "release_at_et": "2026-07-03T08:30:00-04:00"}

    patched = patch_strategy_event(strategy, entry)

    assert patched.event_time == "2026-07-03T08:30:00-04:00"
    assert "discover_key" not in patched.event
