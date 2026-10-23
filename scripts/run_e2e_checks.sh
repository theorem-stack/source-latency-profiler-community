#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SLP_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NULLONE_ROOT="${NULLONE_ROOT:-${SLP_ROOT}/../NullOne}"

run_profiler_suite() {
  echo "==> source-latency-profiler: scheduler/strategy regression tests"
  (
    cd "${SLP_ROOT}"
    pytest tests/test_scheduler.py tests/test_strategy.py
  )
}

setup_nullone_venv_if_needed() {
  if [[ ! -x "${NULLONE_ROOT}/.venv/bin/python" ]]; then
    echo "==> NullOne: creating .venv"
    (
      cd "${NULLONE_ROOT}"
      python3 -m venv .venv
    )
  fi

  if [[ ! -x "${NULLONE_ROOT}/.venv/bin/pytest" ]]; then
    echo "==> NullOne: installing dev dependencies"
    (
      cd "${NULLONE_ROOT}"
      .venv/bin/python -m pip install --upgrade pip
      .venv/bin/python -m pip install -e ".[dev]"
    )
  fi
}

run_nullone_suite() {
  if [[ ! -d "${NULLONE_ROOT}" ]]; then
    echo "warning: NullOne repo not found at ${NULLONE_ROOT}; skipping NullOne checks"
    return
  fi

  setup_nullone_venv_if_needed

  echo "==> NullOne: discover/cli regression tests"
  (
    cd "${NULLONE_ROOT}"
    .venv/bin/pytest tests/test_kalshi_discover.py tests/test_cli.py
  )
}

echo "Running cross-repo E2E regression checks..."
run_profiler_suite
run_nullone_suite
echo "E2E checks completed successfully."
