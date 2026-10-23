#!/usr/bin/env bash
# install-strategy.sh — generate and install systemd service + timer units for
# a profiler strategy YAML.
#
# Usage:
#   scripts/install-strategy.sh \
#     --strategy  strategies/nullone-wngsr-kalshi.yaml \
#     [--calendar  calendars/kalshi-wngsr-2026.json] \
#     [--unit-name wngsr]                    # defaults to strategy.name with _ -> -
#     [--secrets-prefix nullone/prod]        # default: nullone/prod
#     [--timer-interval 5min]                # default: 5min
#     [--extra-args "--samples 1 --diagnostics hot-path"]
#     [--enable]                             # enable + start the timer after install
#     [--dry-run]                            # print generated units, do not write
#
# Environment:
#   APP_DIR        — profiler root on this host (default: /opt/source-latency-profiler)
#   RESULTS_BUCKET — S3 bucket name for results and runtime artefacts (required)
#
# The generated unit names are:
#   source-latency-profiler-scheduler-<slug>.service
#   source-latency-profiler-scheduler-<slug>.timer
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/source-latency-profiler}"
RESULTS_BUCKET="${RESULTS_BUCKET:?Set RESULTS_BUCKET to your profiler results bucket name}"
AWS_REGION="${AWS_REGION:-us-east-1}"
SCHEDULER_STATE_DIR="${SCHEDULER_STATE_DIR:-${APP_DIR}/.generated/scheduler}"
SCHEDULER_EVENTS_DIR="${SCHEDULER_EVENTS_DIR:-${APP_DIR}/.generated/scheduler}"
NULLONE_ENV_FILE="${NULLONE_ENV_FILE:-/etc/source-latency-profiler/nullone.env}"
RUN_AS_USER="${RUN_AS_USER:-ec2-user}"

STRATEGY=""
CALENDAR=""
UNIT_NAME=""
SECRETS_PREFIX="nullone/prod"
TIMER_INTERVAL="5min"
EXTRA_ARGS=""
ENABLE=false
DRY_RUN=false

# ── Argument parsing ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --strategy)        STRATEGY="$2";        shift 2 ;;
    --calendar)        CALENDAR="$2";        shift 2 ;;
    --unit-name)       UNIT_NAME="$2";       shift 2 ;;
    --secrets-prefix)  SECRETS_PREFIX="$2";  shift 2 ;;
    --timer-interval)  TIMER_INTERVAL="$2";  shift 2 ;;
    --extra-args)      EXTRA_ARGS="$2";      shift 2 ;;
    --enable)          ENABLE=true;          shift ;;
    --dry-run)         DRY_RUN=true;         shift ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: install-strategy.sh --strategy <path> [options]" >&2
      exit 1
      ;;
  esac
done

if [[ -z "${STRATEGY}" ]]; then
  echo "Error: --strategy is required." >&2
  echo "Usage: install-strategy.sh --strategy <path> [options]" >&2
  exit 1
fi

if ! id "${RUN_AS_USER}" >/dev/null 2>&1; then
  echo "Run user does not exist: ${RUN_AS_USER}" >&2
  echo "Set RUN_AS_USER to a valid account before installing units." >&2
  exit 1
fi

# ── Resolve strategy YAML path ─────────────────────────────────────────────────
# Accept paths relative to APP_DIR, relative to cwd, or absolute.
STRATEGY_YAML="${APP_DIR}/${STRATEGY}"
if [[ ! -f "${STRATEGY_YAML}" ]]; then
  STRATEGY_YAML="${STRATEGY}"
fi
if [[ ! -f "${STRATEGY_YAML}" ]]; then
  echo "Strategy file not found: ${STRATEGY}" >&2
  exit 1
fi

# ── Derive slug from strategy.name ────────────────────────────────────────────
# Uses stdlib-only Python to avoid a PyYAML dependency at install time.
# Works for any strategy YAML that follows the standard schema (strategy.name
# is an unquoted scalar in the first-level `strategy:` mapping block).
STRATEGY_NAME=$(python3 - <<PYEOF
import pathlib, sys

text = pathlib.Path("${STRATEGY_YAML}").read_text()
in_strategy = False
for line in text.splitlines():
    if line.startswith("strategy:"):
        in_strategy = True
        continue
    if in_strategy:
        stripped = line.lstrip()
        if stripped.startswith("name:"):
            name = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            print(name.replace("_", "-"))
            sys.exit(0)
        # A non-indented line means we've left the strategy block
        if line and not line[0].isspace():
            break

sys.exit("strategy.name not found in " + "${STRATEGY_YAML}")
PYEOF
) || { echo "Cannot read strategy name from ${STRATEGY_YAML}" >&2; exit 1; }

SLUG="${UNIT_NAME:-${STRATEGY_NAME}}"
SERVICE_UNIT="source-latency-profiler-scheduler-${SLUG}.service"
TIMER_UNIT="source-latency-profiler-scheduler-${SLUG}.timer"

# ── Build ExecStart line ───────────────────────────────────────────────────────
EXEC_START="${APP_DIR}/.venv/bin/python -m profiler.cli scheduler run"
EXEC_START+=" --strategy ${STRATEGY}"
[[ -n "${CALENDAR}" ]]   && EXEC_START+=" --event-calendar ${CALENDAR}"
EXEC_START+=" --secrets-prefix ${SECRETS_PREFIX}"
EXEC_START+=" --state-path ${SCHEDULER_STATE_DIR}/${SLUG}-state.json"
EXEC_START+=" --events-path ${SCHEDULER_EVENTS_DIR}/${SLUG}-events.jsonl"
EXEC_START+=" --once"
EXEC_START+=" --results-s3-prefix s3://${RESULTS_BUCKET}/profiler"
EXEC_START+=" --runtime-s3-prefix s3://${RESULTS_BUCKET}/runtime"
EXEC_START+=" --teardown-mode after-execution"
EXEC_START+=" --execution-handoff host-timer"
EXEC_START+=" --execution-lead-seconds 60"
[[ -n "${EXTRA_ARGS}" ]] && EXEC_START+=" ${EXTRA_ARGS}"

# ── Generate unit content ──────────────────────────────────────────────────────
SERVICE_CONTENT=$(cat <<EOF
[Unit]
Description=Source Latency Profiler scheduler: ${SLUG}
After=network-online.target

[Service]
Type=oneshot
TimeoutStartSec=0
WorkingDirectory=${APP_DIR}
User=${RUN_AS_USER}
Group=${RUN_AS_USER}
EnvironmentFile=${NULLONE_ENV_FILE}
Environment=AWS_REGION=${AWS_REGION}
Environment=RESULTS_BUCKET=${RESULTS_BUCKET}
ExecStart=${EXEC_START}

[Install]
WantedBy=multi-user.target
EOF
)

TIMER_CONTENT=$(cat <<EOF
[Unit]
Description=Run ${SLUG} strategy scheduler every ${TIMER_INTERVAL}

[Timer]
OnBootSec=2min
OnUnitActiveSec=${TIMER_INTERVAL}
Unit=${SERVICE_UNIT}
Persistent=true

[Install]
WantedBy=timers.target
EOF
)

# ── Dry run: print and exit without writing ────────────────────────────────────
if [[ "${DRY_RUN}" == true ]]; then
  echo "=== /etc/systemd/system/${SERVICE_UNIT} ==="
  echo "${SERVICE_CONTENT}"
  echo ""
  echo "=== /etc/systemd/system/${TIMER_UNIT} ==="
  echo "${TIMER_CONTENT}"
  exit 0
fi

# ── Root required to write unit files ─────────────────────────────────────────
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo) to install systemd units." >&2
  exit 1
fi

mkdir -p "${SCHEDULER_STATE_DIR}" "${SCHEDULER_EVENTS_DIR}"
chown -R "${RUN_AS_USER}:${RUN_AS_USER}" "${SCHEDULER_STATE_DIR}" "${SCHEDULER_EVENTS_DIR}"
chmod 755 "${SCHEDULER_STATE_DIR}" "${SCHEDULER_EVENTS_DIR}"

# ── Write units and reload ─────────────────────────────────────────────────────
echo "${SERVICE_CONTENT}" > "/etc/systemd/system/${SERVICE_UNIT}"
echo "${TIMER_CONTENT}"   > "/etc/systemd/system/${TIMER_UNIT}"

systemctl daemon-reload

echo "Installed ${SERVICE_UNIT} and ${TIMER_UNIT}."

if [[ "${ENABLE}" == true ]]; then
  systemctl enable --now "${TIMER_UNIT}"
  echo "Enabled and started ${TIMER_UNIT}."
else
  echo "Timer installed but not enabled. To enable:"
  echo "  sudo systemctl enable --now ${TIMER_UNIT}"
fi
