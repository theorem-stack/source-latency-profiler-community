from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from profiler.config import PlacementProfile
from profiler.report import ReportRow, build_report


def build_recommendation(results: list[dict[str, Any]], profile: PlacementProfile) -> dict[str, Any]:
    rows = [row for row in build_report(results) if row.target in profile.targets]
    by_location: dict[tuple[str, str], list[ReportRow]] = defaultdict(list)
    for row in rows:
        by_location[row.location_key].append(row)

    scored = [
        _score_location(provider, region, location_rows, profile)
        for (provider, region), location_rows in by_location.items()
    ]
    scored = [item for item in scored if item is not None]
    scored.sort(key=lambda item: item["score"])

    best = scored[0] if scored else None
    second = scored[1] if len(scored) > 1 else None
    confidence = _confidence(best, second, profile) if best else 0.0

    recommendation = {
        "schema_version": 1,
        "profile": profile.name,
        "recommended_location": None,
        "confidence": confidence,
        "score": None,
        "rationale": [],
        "alternates": [],
        "sample_window": {
            "started_at": _min_timestamp(results),
            "ended_at": _max_timestamp(results),
            "minimum_samples_met": bool(best and best["minimum_samples_met"]),
        },
        "measured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }

    if best:
        recommendation["recommended_location"] = {
            "provider": best["provider"],
            "region": best["region"],
        }
        recommendation["score"] = best["score"]
        recommendation["rationale"] = _rationale(best, second)
        recommendation["alternates"] = [
            {"provider": item["provider"], "region": item["region"], "score": item["score"]}
            for item in scored[1:4]
        ]

    return recommendation


def write_recommendation(path: str | Path, recommendation: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(recommendation, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _score_location(
    provider: str,
    region: str,
    rows: list[ReportRow],
    profile: PlacementProfile,
) -> dict[str, Any] | None:
    rows_by_target = {row.target: row for row in rows}
    missing_targets = sorted(profile.targets - set(rows_by_target))
    if missing_targets:
        return None

    source_rows = [rows_by_target[target] for target in profile.source_targets]
    venue_rows = [rows_by_target[target] for target in profile.venue_targets]
    all_rows = source_rows + venue_rows

    features = {
        "source_ttfb_p50": _avg_metric(source_rows, "ttfb_p50", fallback_attribute="p50"),
        "source_ttfb_p95": _avg_metric(source_rows, "ttfb_p95", fallback_attribute="p95"),
        "venue_total_p50": _avg_metric(venue_rows, "p50"),
        "venue_total_p95": _avg_metric(venue_rows, "p95"),
        "jitter": _avg_metric(all_rows, "mad_ms"),
        "failure_rate": 1 - _avg_value([row.success_rate for row in all_rows]),
    }

    score = 0.0
    for name, weight in profile.weights.items():
        value = features.get(name)
        if value is None:
            score += weight * 1_000_000
        elif name == "failure_rate":
            score += weight * value * 10_000
        else:
            score += weight * value

    samples = sum(row.count for row in all_rows)
    return {
        "provider": provider,
        "region": region,
        "score": round(score, 3),
        "features": features,
        "sample_count": samples,
        "minimum_samples_met": samples >= profile.minimum_samples,
        "success_rate": _avg_value([row.success_rate for row in all_rows]),
    }


def _confidence(best: dict[str, Any] | None, second: dict[str, Any] | None, profile: PlacementProfile) -> float:
    if not best or not best["minimum_samples_met"]:
        return 0.0
    success_component = max(0.0, min(1.0, best["success_rate"]))
    sample_component = min(1.0, best["sample_count"] / max(profile.minimum_samples, 1))
    if not second:
        margin_component = 0.5
    else:
        margin = max(0.0, second["score"] - best["score"])
        margin_component = min(1.0, margin / max(best["score"], 1.0))
    return round((success_component * 0.45) + (sample_component * 0.25) + (margin_component * 0.30), 3)


def _rationale(best: dict[str, Any], second: dict[str, Any] | None) -> list[str]:
    rationale = [f"Best weighted profile score: {best['score']}"]
    if second:
        rationale.append(f"Next alternate score: {second['score']}")
    if best["minimum_samples_met"]:
        rationale.append("Minimum sample threshold met")
    else:
        rationale.append("Minimum sample threshold not met")
    rationale.append(f"Average success rate: {best['success_rate']:.2%}")
    return rationale


def _avg_metric(rows: list[ReportRow], attribute: str, fallback_attribute: str | None = None) -> float | None:
    values: list[float | None] = []
    for row in rows:
        value = getattr(row, attribute)
        if value is None and fallback_attribute is not None:
            value = getattr(row, fallback_attribute)
        values.append(value)
    return _avg_value(values)


def _avg_value(values: list[float | None]) -> float:
    numeric = [float(value) for value in values if value is not None]
    return sum(numeric) / len(numeric) if numeric else 0.0


def _min_timestamp(results: list[dict[str, Any]]) -> str | None:
    timestamps = sorted(str(result["timestamp"]) for result in results if result.get("timestamp"))
    return timestamps[0] if timestamps else None


def _max_timestamp(results: list[dict[str, Any]]) -> str | None:
    timestamps = sorted(str(result["timestamp"]) for result in results if result.get("timestamp"))
    return timestamps[-1] if timestamps else None
