from datetime import datetime

from profiler.infra_profile import build_infrastructure_recommendation
from profiler.runtime_config import build_nullone_runtime_config
from profiler.strategy import load_strategy_profile


def test_build_nullone_runtime_config() -> None:
    strategy = load_strategy_profile("strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml")
    infra = build_infrastructure_recommendation(
        strategy,
        _recommendation(),
        run_id="run-123",
        now=datetime.fromisoformat("2026-05-22T10:20:00-04:00"),
    )

    runtime = build_nullone_runtime_config(strategy, infra)

    assert runtime["engine"] == "nullone"
    assert runtime["strategy"]["event_id"] == "eia_wngsr_2026-05-15"
    assert runtime["event"]["metric"] == "net_change_bcf"
    assert len(runtime["markets"]["brackets"]) == 3
    assert runtime["execution"]["release_capital_usd"] == 100
    assert runtime["placement"]["provider"] == "aws"
    assert runtime["runtime"]["run_id"] == "run-123"
    assert runtime["runtime"]["profiler_blackout_at"] == "2026-05-22T10:28:00-04:00"
    assert runtime["sources"][0]["name"] == "eia_wngsr_json"
    assert runtime["venues"][0]["name"] == "kalshi_status"


def _recommendation() -> dict:
    return {
        "recommended_location": {"provider": "aws", "region": "us-east-1"},
        "confidence": 0.9,
        "score": 10,
        "alternates": [{"provider": "aws", "region": "us-east-2", "score": 12}],
        "sample_window": {"minimum_samples_met": True},
    }
