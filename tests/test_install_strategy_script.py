from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_install_strategy_dry_run_includes_runtime_user() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "install-strategy.sh"
    run_as_user = os.environ.get("USER", "root")

    result = subprocess.run(
        [
            str(script_path),
            "--strategy",
            "strategies/nullone-wngsr-kalshi.yaml",
            "--calendar",
            "calendars/kalshi-wngsr-2026.json",
            "--dry-run",
        ],
        cwd=repo_root,
        env={
            **os.environ,
            "RESULTS_BUCKET": "unit-test-bucket",
            "RUN_AS_USER": run_as_user,
        },
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"User={run_as_user}" in result.stdout
    assert f"Group={run_as_user}" in result.stdout
