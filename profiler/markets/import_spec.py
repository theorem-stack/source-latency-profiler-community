from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from profiler.config import load_yaml


class MarketsImportError(ValueError):
    """Raised when a NullOne spec cannot be imported."""


def import_nullone_spec(spec_path: str | Path, *, expected_week_ending: str | None = None) -> dict[str, Any]:
    path = Path(spec_path)
    if not path.exists():
        raise MarketsImportError(f"NullOne spec not found: {path}")

    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise MarketsImportError(f"invalid spec format in {path}")

    week_ending = str(raw.get("week_ending", "")).strip()
    if expected_week_ending and week_ending != expected_week_ending:
        raise MarketsImportError(
            f"spec week_ending={week_ending} does not match expected {expected_week_ending}"
        )

    brackets_raw = raw.get("brackets")
    if not isinstance(brackets_raw, list):
        raise MarketsImportError(f"spec {path} is missing brackets list")

    brackets = [_normalize_bracket(item, index=index) for index, item in enumerate(brackets_raw)]
    _validate_brackets(brackets)

    return {
        "brackets": brackets,
        "kalshi_event_ticker": raw.get("kalshi_event_ticker"),
        "event_id": raw.get("event_id"),
        "week_ending": week_ending,
        "imported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "spec_path": str(path.resolve()),
    }


def _coerce_numeric(value: Any) -> int | float:
    """Coerce a bracket bound to int or float, preserving the original type.

    - Python ``int`` values are returned unchanged.
    - Python ``float`` and string values are converted to ``float``.
    - ``bool`` is rejected (it is a subclass of int in Python but not a valid bound).
    """
    if isinstance(value, bool):
        raise MarketsImportError(f"bracket bound must be a number, got bool: {value!r}")
    if isinstance(value, int):
        return value
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise MarketsImportError(f"bracket bound is not a valid number: {value!r}") from exc


def _normalize_bracket(raw: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MarketsImportError(f"bracket[{index}] must be a mapping")

    label = raw.get("label")
    min_value = raw.get("min")
    max_value = raw.get("max")
    kalshi_raw = raw.get("kalshi") or {}
    if not isinstance(kalshi_raw, dict):
        kalshi_raw = {}

    ticker = str(kalshi_raw.get("market_ticker", "")).strip()
    if not ticker or ticker == "TBD":
        raise MarketsImportError(f"bracket[{index}] missing kalshi.market_ticker")

    coerced_min = _coerce_numeric(min_value)
    coerced_max = _coerce_numeric(max_value)

    bracket: dict[str, Any] = {
        "label": str(label or f"{min_value}-{max_value}"),
        "min": coerced_min,
        "max": coerced_max,
        "kalshi": {"market_ticker": ticker},
    }

    poly_raw = raw.get("polymarket")
    if isinstance(poly_raw, dict) and poly_raw.get("token_id") and poly_raw["token_id"] != "TBD":
        bracket["polymarket"] = {
            "token_id": str(poly_raw["token_id"]),
            "tick_size": str(poly_raw.get("tick_size", "0.01")),
            "neg_risk": bool(poly_raw.get("neg_risk", False)),
        }

    return bracket


def _validate_brackets(brackets: list[dict[str, Any]]) -> None:
    if len(brackets) < 3:
        raise MarketsImportError(f"expected at least 3 brackets, found {len(brackets)}")

    ordered = sorted(brackets, key=lambda item: float(item["min"]))
    for index in range(1, len(ordered)):
        previous = ordered[index - 1]
        current = ordered[index]
        if float(current["min"]) <= float(previous["max"]):
            raise MarketsImportError(
                f"bracket overlap: {previous['label']} and {current['label']}"
            )
