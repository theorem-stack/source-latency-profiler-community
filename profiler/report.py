from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ReportRow:
    target: str
    provider: str
    region: str
    count: int
    success_rate: float
    p50: float | None
    p90: float | None
    p95: float | None
    p99: float | None
    ttfb_p50: float | None
    ttfb_p95: float | None
    min_ms: float | None
    max_ms: float | None
    stddev_ms: float | None
    mad_ms: float | None
    dns_variance_ms: float | None
    remote_ip_distribution: dict[str, int]

    @property
    def location_key(self) -> tuple[str, str]:
        return self.provider, self.region


def build_report(results: Iterable[dict[str, Any]]) -> list[ReportRow]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        groups[(result["target"], result["provider"], result["region"])].append(result)

    rows = [_summarize_group(target, provider, region, items) for (target, provider, region), items in groups.items()]
    return sorted(rows, key=lambda row: (row.target, _sort_none_last(row.p95), _sort_none_last(row.p50)))


def print_report(rows: list[ReportRow]) -> str:
    if not rows:
        return "No probe results found."

    headers = ["target", "provider", "region", "count", "success", "p50", "p95", "p99", "jitter"]
    lines = ["\t".join(headers)]
    for row in rows:
        lines.append(
            "\t".join(
                [
                    row.target,
                    row.provider,
                    row.region,
                    str(row.count),
                    f"{row.success_rate:.2%}",
                    _fmt_ms(row.p50),
                    _fmt_ms(row.p95),
                    _fmt_ms(row.p99),
                    _fmt_ms(row.mad_ms),
                ]
            )
        )
    return "\n".join(lines)


def winner_by_target(rows: list[ReportRow]) -> dict[str, ReportRow]:
    winners: dict[str, ReportRow] = {}
    for row in rows:
        current = winners.get(row.target)
        if current is None or _row_score(row) < _row_score(current):
            winners[row.target] = row
    return winners


def _summarize_group(target: str, provider: str, region: str, items: list[dict[str, Any]]) -> ReportRow:
    successes = [item for item in items if item.get("error") in {None, ""} and item.get("total_ms") is not None]
    totals = [float(item["total_ms"]) for item in successes]
    ttfbs = [float(item["ttfb_ms"]) for item in successes if item.get("ttfb_ms") is not None]
    dns_values = [float(item["dns_ms"]) for item in successes if item.get("dns_ms") is not None]
    remote_ips = Counter(str(item.get("remote_ip")) for item in successes if item.get("remote_ip"))
    median = statistics.median(totals) if totals else None
    deviations = [abs(value - median) for value in totals] if median is not None else []

    return ReportRow(
        target=target,
        provider=provider,
        region=region,
        count=len(items),
        success_rate=len(successes) / len(items) if items else 0.0,
        p50=percentile(totals, 50),
        p90=percentile(totals, 90),
        p95=percentile(totals, 95),
        p99=percentile(totals, 99),
        ttfb_p50=percentile(ttfbs, 50),
        ttfb_p95=percentile(ttfbs, 95),
        min_ms=min(totals) if totals else None,
        max_ms=max(totals) if totals else None,
        stddev_ms=statistics.stdev(totals) if len(totals) > 1 else None,
        mad_ms=statistics.median(deviations) if deviations else None,
        dns_variance_ms=statistics.variance(dns_values) if len(dns_values) > 1 else None,
        remote_ip_distribution=dict(remote_ips),
    )


def percentile(values: list[float], percentile_value: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * (percentile_value / 100)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(ordered[int(rank)], 3)
    weight = rank - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def _row_score(row: ReportRow) -> float:
    failure_penalty = (1 - row.success_rate) * 10_000
    return (row.p95 if row.p95 is not None else 1_000_000) + failure_penalty


def _fmt_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _sort_none_last(value: float | None) -> float:
    return value if value is not None else float("inf")
