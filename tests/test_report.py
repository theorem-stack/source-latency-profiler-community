from profiler.report import build_report, percentile, winner_by_target


def test_percentile_interpolates_values() -> None:
    assert percentile([10, 20, 30, 40], 50) == 25
    assert percentile([10], 95) == 10
    assert percentile([], 95) is None


def test_report_groups_by_target_and_location() -> None:
    rows = build_report(
        [
            _result("eia", "aws", "us-east-1", 10),
            _result("eia", "aws", "us-east-1", 20),
            _result("eia", "aws", "us-east-2", 30),
            _result("eia", "aws", "us-east-2", None, error="timeout"),
        ]
    )

    assert len(rows) == 2
    east_1 = next(row for row in rows if row.region == "us-east-1")
    east_2 = next(row for row in rows if row.region == "us-east-2")
    assert east_1.success_rate == 1
    assert east_2.success_rate == 0.5
    assert winner_by_target(rows)["eia"].region == "us-east-1"


def _result(target: str, provider: str, region: str, total_ms: float | None, error: str | None = None) -> dict:
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
        "error": error,
    }
