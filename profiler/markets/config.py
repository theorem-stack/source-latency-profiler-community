from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SPEC_TEMPLATE = "specs/eia_wngsr_{week_ending}.yaml"
DEFAULT_REFRESH_OFFSETS = ("T-7d", "T-3d", "T-1d", "T-6h", "T-30m")
DEFAULT_DISCOVER_KEY_FIELD = "week_ending"
DEFAULT_DISCOVER_COMMAND_TEMPLATE = "discover --week {discover_key}"


@dataclass(frozen=True)
class MarketsConfig:
    source: str
    spec_template: str
    nullone_root: Path
    series_ticker: str | None
    refresh_offsets: tuple[str, ...]
    pinned_brackets: list[dict[str, Any]] | None
    discover_key_field: str = DEFAULT_DISCOVER_KEY_FIELD
    discover_command_template: str = DEFAULT_DISCOVER_COMMAND_TEMPLATE

    @property
    def requires_discovery(self) -> bool:
        return self.source == "nullone_discover"


def parse_markets_config(markets: dict[str, Any], *, nullone_root: str | Path | None = None) -> MarketsConfig:
    if not isinstance(markets, dict):
        raise ValueError("markets must be a mapping")

    brackets = markets.get("brackets")
    if brackets is not None:
        if not isinstance(brackets, list) or not brackets:
            raise ValueError("markets.brackets must be a non-empty list when provided")
        refresh = markets.get("refresh", {})
        if not isinstance(refresh, dict):
            refresh = {}
        offsets_raw = refresh.get("offsets", DEFAULT_REFRESH_OFFSETS)
        if isinstance(offsets_raw, tuple):
            offsets_raw = list(offsets_raw)
        if not isinstance(offsets_raw, list) or not offsets_raw:
            raise ValueError("markets.refresh.offsets must be a non-empty list")
        return MarketsConfig(
            source="pinned",
            spec_template=DEFAULT_SPEC_TEMPLATE,
            nullone_root=_resolve_nullone_root(nullone_root),
            series_ticker=None,
            refresh_offsets=tuple(str(item) for item in offsets_raw),
            pinned_brackets=brackets,
        )

    source = str(markets.get("source", "nullone_discover")).strip().lower()
    if source not in {"nullone_discover", "pinned"}:
        raise ValueError(f"unsupported markets.source: {source}")

    nullone = markets.get("nullone", {})
    if not isinstance(nullone, dict):
        nullone = {}

    refresh = markets.get("refresh", {})
    if not isinstance(refresh, dict):
        refresh = {}
    offsets_raw = refresh.get("offsets", DEFAULT_REFRESH_OFFSETS)
    if not isinstance(offsets_raw, list) or not offsets_raw:
        raise ValueError("markets.refresh.offsets must be a non-empty list")

    root_override = nullone_root or nullone.get("root")
    return MarketsConfig(
        source=source,
        spec_template=str(nullone.get("spec_template", DEFAULT_SPEC_TEMPLATE)),
        nullone_root=_resolve_nullone_root(root_override),
        series_ticker=nullone.get("series_ticker"),
        refresh_offsets=tuple(str(item) for item in offsets_raw),
        pinned_brackets=None,
        discover_key_field=str(nullone.get("discover_key_field", DEFAULT_DISCOVER_KEY_FIELD)),
        discover_command_template=str(
            nullone.get("discover_command_template", DEFAULT_DISCOVER_COMMAND_TEMPLATE)
        ),
    )


def spec_path_for_discover_key(config: MarketsConfig, discover_key: str) -> Path:
    """Return the spec file path for the given discover key.

    The spec_template is formatted with both ``discover_key`` and ``week_ending``
    set to the same value so that both ``{discover_key}`` and ``{week_ending}``
    placeholders are supported in templates.
    """
    relative = config.spec_template.format(discover_key=discover_key, week_ending=discover_key)
    if Path(relative).is_absolute():
        return Path(relative)
    return config.nullone_root / relative


def spec_path_for_week(config: MarketsConfig, week_ending: str) -> Path:
    """Backward-compatible alias for spec_path_for_discover_key."""
    return spec_path_for_discover_key(config, week_ending)


def _resolve_nullone_root(value: str | Path | None) -> Path:
    if value is not None:
        return Path(value)
    env_root = os.environ.get("NULLONE_ROOT")
    if env_root:
        return Path(env_root)
    return Path("/opt/nullone")
