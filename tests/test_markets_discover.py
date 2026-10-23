from pathlib import Path
from typing import Any

import pytest

from profiler.markets.config import parse_markets_config
from profiler.markets.discover import MarketsDiscoveryError, run_nullone_discover


def _fake_run_factory(spec_path: Path, spec_content: str = "week_ending: '2026-05-15'\nbrackets: []\n"):
    def fake_run(command: list[str], **kwargs: Any):
        spec_path.write_text(spec_content, encoding="utf-8")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    return fake_run


def test_run_nullone_discover_invokes_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    spec_path = spec_dir / "eia_wngsr_2026-05-15.yaml"

    monkeypatch.setattr("profiler.markets.discover.subprocess.run", _fake_run_factory(spec_path))
    monkeypatch.setattr("profiler.markets.discover.shutil.which", lambda _: "/usr/bin/nullone")

    config = parse_markets_config(
        {
            "source": "nullone_discover",
            "nullone": {"root": str(tmp_path), "spec_template": "specs/eia_wngsr_{week_ending}.yaml"},
            "refresh": {"offsets": ["T-7d"]},
        }
    )

    path = run_nullone_discover(config, week_ending="2026-05-15")
    assert path == spec_path


def test_run_nullone_discover_uses_discover_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """discover_key param produces the same result as week_ending for default config."""
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    spec_path = spec_dir / "eia_wngsr_2026-05-15.yaml"

    monkeypatch.setattr("profiler.markets.discover.subprocess.run", _fake_run_factory(spec_path))
    monkeypatch.setattr("profiler.markets.discover.shutil.which", lambda _: "/usr/bin/nullone")

    config = parse_markets_config(
        {
            "source": "nullone_discover",
            "nullone": {"root": str(tmp_path), "spec_template": "specs/eia_wngsr_{week_ending}.yaml"},
            "refresh": {"offsets": ["T-7d"]},
        }
    )

    path = run_nullone_discover(config, discover_key="2026-05-15")
    assert path == spec_path


def test_run_nullone_discover_custom_command_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Custom discover_command_template is formatted and used as CLI args."""
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    spec_path = spec_dir / "kalshi_cpi_2026-07.yaml"

    captured: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any):
        captured.append(command)
        spec_path.write_text("brackets: []\n", encoding="utf-8")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("profiler.markets.discover.subprocess.run", fake_run)
    monkeypatch.setattr("profiler.markets.discover.shutil.which", lambda _: "/usr/bin/nullone")

    config = parse_markets_config(
        {
            "source": "nullone_discover",
            "nullone": {
                "root": str(tmp_path),
                "spec_template": "specs/kalshi_cpi_{discover_key}.yaml",
                "discover_key_field": "release_month",
                "discover_command_template": "discover --event cpi --month {discover_key}",
            },
            "refresh": {"offsets": ["T-7d"]},
        }
    )

    assert config.discover_key_field == "release_month"
    assert config.discover_command_template == "discover --event cpi --month {discover_key}"

    path = run_nullone_discover(config, discover_key="2026-07")
    assert path == spec_path
    assert captured[0] == [
        "nullone",
        "discover",
        "--event",
        "cpi",
        "--month",
        "2026-07",
        "--output",
        str(spec_path),
    ]


def test_run_nullone_discover_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("profiler.markets.discover.shutil.which", lambda _: None)
    config = parse_markets_config(
        {
            "source": "nullone_discover",
            "nullone": {"root": "/tmp", "spec_template": "specs/eia_wngsr_{week_ending}.yaml"},
            "refresh": {"offsets": ["T-7d"]},
        }
    )
    with pytest.raises(MarketsDiscoveryError, match="not found on PATH"):
        run_nullone_discover(config, week_ending="2026-05-15")


def test_run_nullone_discover_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("profiler.markets.discover.shutil.which", lambda _: "/usr/bin/nullone")
    config = parse_markets_config(
        {
            "source": "nullone_discover",
            "nullone": {"root": "/tmp"},
            "refresh": {"offsets": ["T-7d"]},
        }
    )
    with pytest.raises(ValueError, match="discover_key or week_ending"):
        run_nullone_discover(config)
