from profiler.config import PlacementProfile
from profiler.recommend import build_recommendation


def test_recommendation_selects_lowest_weighted_location() -> None:
    profile = PlacementProfile(
        name="test_profile",
        objective="source_to_venue",
        source_targets=["source"],
        venue_targets=["venue"],
        weights={
            "source_ttfb_p50": 0.25,
            "source_ttfb_p95": 0.25,
            "venue_total_p50": 0.25,
            "venue_total_p95": 0.25,
        },
        minimum_samples=2,
    )
    results = [
        _result("source", "aws", "us-east-1", 10),
        _result("venue", "aws", "us-east-1", 20),
        _result("source", "aws", "us-east-2", 30),
        _result("venue", "aws", "us-east-2", 40),
    ]

    recommendation = build_recommendation(results, profile)

    assert recommendation["recommended_location"] == {"provider": "aws", "region": "us-east-1"}
    assert recommendation["sample_window"]["minimum_samples_met"] is True
    assert recommendation["confidence"] > 0


def _result(target: str, provider: str, region: str, total_ms: float) -> dict:
    return {
        "schema_version": 1,
        "target": target,
        "target_type": "http",
        "provider": provider,
        "region": region,
        "runner_id": f"{provider}-{region}",
        "timestamp": "2026-05-24T22:00:00Z",
        "total_ms": total_ms,
        "dns_ms": 1.0,
        "remote_ip": "203.0.113.10",
        "error": None,
    }
