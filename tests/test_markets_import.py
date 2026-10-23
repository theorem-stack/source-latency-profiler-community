import tempfile
from pathlib import Path

import pytest
import yaml

from profiler.markets.import_spec import MarketsImportError, _coerce_numeric, import_nullone_spec


def test_import_nullone_spec_maps_brackets() -> None:
    spec_path = Path("tests/fixtures/nullone_spec.yaml")
    markets = import_nullone_spec(spec_path, expected_week_ending="2026-05-15")

    assert markets["kalshi_event_ticker"] == "KXGASSTORAGE-MAY15"
    assert len(markets["brackets"]) == 3
    assert markets["brackets"][0]["kalshi"]["market_ticker"] == "KXGASSTORAGE-MAY15-98"


def test_import_nullone_spec_rejects_week_mismatch() -> None:
    with pytest.raises(MarketsImportError, match="week_ending"):
        import_nullone_spec("tests/fixtures/nullone_spec.yaml", expected_week_ending="2026-06-15")


# ── _coerce_numeric unit tests ────────────────────────────────────────────────

def test_coerce_numeric_int_passthrough() -> None:
    assert _coerce_numeric(96) == 96
    assert isinstance(_coerce_numeric(96), int)


def test_coerce_numeric_float_preserved() -> None:
    result = _coerce_numeric(3.2)
    assert result == 3.2
    assert isinstance(result, float)


def test_coerce_numeric_float_stays_float_even_if_whole() -> None:
    result = _coerce_numeric(100.0)
    assert result == 100.0
    assert isinstance(result, float)


def test_coerce_numeric_string_float() -> None:
    result = _coerce_numeric("3.4")
    assert result == 3.4
    assert isinstance(result, float)


def test_coerce_numeric_invalid_string() -> None:
    with pytest.raises(MarketsImportError, match="not a valid number"):
        _coerce_numeric("abc")


def test_coerce_numeric_bool_rejected() -> None:
    with pytest.raises(MarketsImportError, match="bool"):
        _coerce_numeric(True)


# ── float-bracket import integration tests ───────────────────────────────────

def _write_spec(content: dict, tmp_path: Path) -> Path:
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(content), encoding="utf-8")
    return p


def test_import_float_brackets(tmp_path: Path) -> None:
    spec = {
        "event_id": "cpi_2026-07",
        "kalshi_event_ticker": "KXCPI-JUL26",
        "brackets": [
            {"label": "3.0-3.2", "min": 3.0, "max": 3.2, "kalshi": {"market_ticker": "KXCPI-JUL26-31"}},
            {"label": "3.3-3.5", "min": 3.3, "max": 3.5, "kalshi": {"market_ticker": "KXCPI-JUL26-34"}},
            {"label": "3.6-3.8", "min": 3.6, "max": 3.8, "kalshi": {"market_ticker": "KXCPI-JUL26-37"}},
        ],
    }
    markets = import_nullone_spec(_write_spec(spec, tmp_path))

    assert markets["brackets"][0]["min"] == 3.0
    assert isinstance(markets["brackets"][0]["min"], float)
    assert markets["brackets"][1]["min"] == 3.3


def test_import_integer_brackets_stay_int(tmp_path: Path) -> None:
    spec = {
        "week_ending": "2026-05-15",
        "kalshi_event_ticker": "KXGASSTORAGE-MAY15",
        "brackets": [
            {"label": "96-100", "min": 96, "max": 100, "kalshi": {"market_ticker": "KXGASSTORAGE-MAY15-98"}},
            {"label": "101-105", "min": 101, "max": 105, "kalshi": {"market_ticker": "KXGASSTORAGE-MAY15-103"}},
            {"label": "106-110", "min": 106, "max": 110, "kalshi": {"market_ticker": "KXGASSTORAGE-MAY15-108"}},
        ],
    }
    markets = import_nullone_spec(_write_spec(spec, tmp_path))

    assert markets["brackets"][0]["min"] == 96
    assert isinstance(markets["brackets"][0]["min"], int)


def test_import_rejects_overlapping_float_brackets(tmp_path: Path) -> None:
    spec = {
        "kalshi_event_ticker": "KXCPI-JUL26",
        "brackets": [
            {"label": "3.0-3.3", "min": 3.0, "max": 3.3, "kalshi": {"market_ticker": "KXCPI-JUL26-31"}},
            {"label": "3.2-3.5", "min": 3.2, "max": 3.5, "kalshi": {"market_ticker": "KXCPI-JUL26-34"}},
            {"label": "3.6-3.9", "min": 3.6, "max": 3.9, "kalshi": {"market_ticker": "KXCPI-JUL26-37"}},
        ],
    }
    with pytest.raises(MarketsImportError, match="overlap"):
        import_nullone_spec(_write_spec(spec, tmp_path))
