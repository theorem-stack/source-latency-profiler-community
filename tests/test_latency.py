import json

from profiler.latency import build_latency_summary


def test_build_latency_summary_computes_phases_and_gaps(tmp_path) -> None:
    trace = tmp_path / "execution-trace.jsonl"
    events = [
        {"event": "process_start", "timestamp": "2026-05-30T12:00:00.000Z"},
        {"event": "config_load_done", "timestamp": "2026-05-30T12:00:00.100Z"},
        {"event": "pipeline_init_done", "timestamp": "2026-05-30T12:00:00.160Z"},
        {"event": "execute_start", "timestamp": "2026-05-30T12:00:00.200Z"},
        {"event": "source_fetch_start", "timestamp": "2026-05-30T12:00:00.230Z"},
        {"event": "source_fetch_done", "timestamp": "2026-05-30T12:00:00.250Z", "duration_ms": 20.0},
        {"event": "order_submit_start", "timestamp": "2026-05-30T12:00:00.280Z"},
        {"event": "order_submit_done", "timestamp": "2026-05-30T12:00:00.310Z"},
    ]
    trace.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    summary = build_latency_summary(trace, budgets_ms={"hot_path_ms": 50})

    assert summary["status"] == "failed"
    assert summary["total_wall_clock_ms"] == 310.0
    assert summary["hot_path_ms"] == 110.0
    assert summary["derived_gaps_ms"]["process_to_config_ms"] == 100.0
    assert summary["derived_gaps_ms"]["engine_wakeup_ms"] == 30.0
    assert summary["derived_gaps_ms"]["source_fetch_transit_ms"] == 20.0
    assert summary["derived_gaps_ms"]["decision_pipeline_ms"] == 30.0
    assert summary["derived_gaps_ms"]["venue_submit_ack_ms"] == 30.0
    assert summary["phases_ms"]["source_fetch"] == 20.0
    assert summary["largest_gap"]["metric"] == "process_to_config_ms"
    assert summary["budget_failures"] == [{"metric": "hot_path_ms", "actual_ms": 110.0, "budget_ms": 50}]
