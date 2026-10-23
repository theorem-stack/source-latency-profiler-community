from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from profiler.markets.config import MarketsConfig, spec_path_for_discover_key as _spec_path_for_discover_key


class MarketsDiscoveryError(RuntimeError):
    """Raised when nullone discover fails."""


def run_nullone_discover(
    config: MarketsConfig,
    *,
    discover_key: str | None = None,
    week_ending: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 120,
) -> Path:
    """Run nullone discover and return the written spec path.

    Pass ``discover_key`` for generic strategies or ``week_ending`` for
    backward compatibility. Both refer to the same underlying key concept;
    ``week_ending`` is accepted as an alias and takes precedence when both
    are provided.
    """
    resolved_key = week_ending or discover_key
    if not resolved_key:
        raise ValueError("run_nullone_discover requires discover_key or week_ending")

    spec_path = _spec_path_for_discover_key(config, resolved_key)
    spec_path.parent.mkdir(parents=True, exist_ok=True)

    binary = os.environ.get("NULLONE_BIN", "nullone")
    if shutil.which(binary) is None:
        raise MarketsDiscoveryError(
            f"{binary} not found on PATH; install NullOne or set NULLONE_BIN"
        )

    discover_args = shlex.split(config.discover_command_template.format(discover_key=resolved_key))
    command = [binary, *discover_args, "--output", str(spec_path)]

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    if config.series_ticker:
        merged_env["KALSHI_GAS_STORAGE_SERIES"] = config.series_ticker
    merged_env.setdefault("NULLONE_ROOT", os.fspath(config.nullone_root))

    pem_cleanup: Path | None = None
    try:
        pem_cleanup = _apply_temp_pem_env(merged_env)
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=merged_env,
            timeout=timeout_seconds,
            cwd=os.fspath(config.nullone_root) if Path(config.nullone_root).exists() else None,
        )
    finally:
        if pem_cleanup and pem_cleanup.exists():
            pem_cleanup.unlink(missing_ok=True)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise MarketsDiscoveryError(
            f"nullone discover failed for discover_key={resolved_key}: {detail}"
        )

    if not spec_path.exists():
        raise MarketsDiscoveryError(f"nullone discover did not write spec to {spec_path}")

    return spec_path


def _apply_temp_pem_env(env: dict[str, str]) -> Path | None:
    pem_inline = env.pop("KALSHI_PRIVATE_KEY_PEM", None)
    if not pem_inline:
        return None
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    handle.write(pem_inline)
    handle.flush()
    handle.close()
    path = Path(handle.name)
    os.chmod(path, 0o600)
    env["KALSHI_PRIVATE_KEY_PATH"] = str(path)
    return path
