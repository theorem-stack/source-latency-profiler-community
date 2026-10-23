#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-source-latency-profiler}"
APP_DIR="${APP_DIR:-/opt/source-latency-profiler}"
ORCHESTRATOR_USER="${ORCHESTRATOR_USER:-ec2-user}"
SECRETS_PREFIX="${SECRETS_PREFIX:-nullone/prod}"
RESULTS_BUCKET="${RESULTS_BUCKET:-}"

NULLONE_INSTALL_MODE="${NULLONE_INSTALL_MODE:-s3}"
NULLONE_S3_URI="${NULLONE_S3_URI:-}"
DEPLOY_SOURCE_MODE="${DEPLOY_SOURCE_MODE:-artifact}"
INSTALL_SCHEDULERS="${INSTALL_SCHEDULERS:-0}"
WAIT_FOR_SSM="${WAIT_FOR_SSM:-1}"
ENABLE_PREFLIGHT="${ENABLE_PREFLIGHT:-1}"

INSTANCE_ID="${INSTANCE_ID:-}"
KALSHI_MARKET_TICKER="${KALSHI_MARKET_TICKER:-}"
EVENT_OFFSET_MINUTES="${EVENT_OFFSET_MINUTES:--2}"
RUN_ID="${RUN_ID:-live-kalshi-e2e-$(date -u +%Y%m%dT%H%M%SZ)}"
KALSHI_DISCOVERY_INSECURE="${KALSHI_DISCOVERY_INSECURE:-0}"
TMP_FILES=()

cleanup_tmp_files() {
  local path
  for path in "${TMP_FILES[@]:-}"; do
    [[ -n "${path}" ]] && rm -f "${path}" || true
  done
}

trap cleanup_tmp_files EXIT

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

require_command aws
require_command python3

if [[ "${NULLONE_INSTALL_MODE}" == "s3" && -z "${NULLONE_S3_URI}" ]]; then
  echo "NULLONE_S3_URI is required when NULLONE_INSTALL_MODE=s3." >&2
  echo "Example: s3://<results-bucket>/nullone/releases/<version>/linux-amd64/nullone" >&2
  exit 1
fi
if [[ "${NULLONE_INSTALL_MODE}" == "s3" && "${NULLONE_S3_URI}" == *"<"* ]]; then
  echo "NULLONE_S3_URI contains placeholder text: ${NULLONE_S3_URI}" >&2
  echo "Set a concrete immutable object key, for example:" >&2
  echo "  s3://source-latency-profiler-results-<acct>/nullone/releases/v0.0.0/linux-amd64/nullone" >&2
  exit 1
fi

if [[ -z "${RESULTS_BUCKET}" ]]; then
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${AWS_REGION}")"
  RESULTS_BUCKET="${PROJECT_NAME}-results-${ACCOUNT_ID}"
fi

pick_live_kalshi_market() {
  if [[ -n "${KALSHI_MARKET_TICKER}" ]]; then
    echo "Using provided KALSHI_MARKET_TICKER=${KALSHI_MARKET_TICKER}"
    return
  fi

  echo "Selecting a live Kalshi market ticker..."
  local urls=(
    "https://api.elections.kalshi.com/trade-api/v2/markets?limit=200&status=open"
    "https://external-api.kalshi.com/trade-api/v2/markets?limit=200&status=open"
  )
  local payload=""

  # Prefer curl so we use the OS trust store on developer machines.
  if command -v curl >/dev/null 2>&1; then
    local curl_flags=(--silent --show-error --fail --location --max-time 20)
    if [[ "${KALSHI_DISCOVERY_INSECURE}" == "1" ]]; then
      echo "Warning: KALSHI_DISCOVERY_INSECURE=1; disabling TLS verification for market discovery." >&2
      curl_flags+=(--insecure)
    fi
    local url
    for url in "${urls[@]}"; do
      if payload="$(curl "${curl_flags[@]}" "${url}" 2>/dev/null)"; then
        break
      fi
      payload=""
    done
  fi

  # Fallback to Python HTTP client if curl is unavailable.
  if [[ -z "${payload}" ]]; then
    payload="$(
      KALSHI_DISCOVERY_INSECURE="${KALSHI_DISCOVERY_INSECURE}" python3 - <<'PY'
import json
import os
import ssl
import urllib.request

urls = [
    "https://api.elections.kalshi.com/trade-api/v2/markets?limit=200&status=open",
    "https://external-api.kalshi.com/trade-api/v2/markets?limit=200&status=open",
]
insecure = os.getenv("KALSHI_DISCOVERY_INSECURE") == "1"
context = ssl._create_unverified_context() if insecure else None

last_err = None
for url in urls:
    try:
        with urllib.request.urlopen(url, timeout=20, context=context) as resp:
            print(resp.read().decode("utf-8"))
            raise SystemExit(0)
    except Exception as exc:  # pragma: no cover - shell fallback path.
        last_err = exc

raise SystemExit(f"Could not fetch an open Kalshi market payload: {last_err}")
PY
    )"
  fi

  KALSHI_MARKET_TICKER="$(
    python3 - <<'PY' "${payload}"
import json
import sys

payload = json.loads(sys.argv[1])
for market in payload.get("markets") or []:
    ticker = market.get("ticker")
    if ticker and isinstance(ticker, str):
        print(ticker)
        raise SystemExit(0)

raise SystemExit("Kalshi response had no open market tickers")
PY
  )"
  echo "Selected market ticker: ${KALSHI_MARKET_TICKER}"
}

launch_orchestrator_if_needed() {
  if [[ -n "${INSTANCE_ID}" ]]; then
    echo "Using provided INSTANCE_ID=${INSTANCE_ID}"
    return
  fi

  echo "Launching orchestrator host via deploy_orchestrator_ec2.sh..."
  local deploy_log
  deploy_log="$(mktemp)"
  TMP_FILES+=("${deploy_log}")

  (
    cd "${REPO_ROOT}"
    AWS_REGION="${AWS_REGION}" \
    PROJECT_NAME="${PROJECT_NAME}" \
    APP_DIR="${APP_DIR}" \
    RESULTS_BUCKET="${RESULTS_BUCKET}" \
    DEPLOY_SOURCE_MODE="${DEPLOY_SOURCE_MODE}" \
    INSTALL_SCHEDULERS="${INSTALL_SCHEDULERS}" \
    WAIT_FOR_SSM="${WAIT_FOR_SSM}" \
    ENABLE_PREFLIGHT="${ENABLE_PREFLIGHT}" \
    NULLONE_INSTALL_MODE="${NULLONE_INSTALL_MODE}" \
    NULLONE_S3_URI="${NULLONE_S3_URI}" \
    bash scripts/deploy_orchestrator_ec2.sh
  ) | tee "${deploy_log}"

  INSTANCE_ID="$(awk '/Instance ID:/ {print $3}' "${deploy_log}" | tail -n 1)"
  if [[ -z "${INSTANCE_ID}" ]]; then
    echo "Could not parse Instance ID from deploy output." >&2
    exit 1
  fi
  echo "Orchestrator instance: ${INSTANCE_ID}"
}

wait_for_ssm_online() {
  echo "Waiting for SSM online state on ${INSTANCE_ID}..."
  for _ in $(seq 1 90); do
    status="$(
      aws ssm describe-instance-information \
        --filters "Key=InstanceIds,Values=${INSTANCE_ID}" \
        --query "InstanceInformationList[0].PingStatus" \
        --output text \
        --region "${AWS_REGION}" 2>/dev/null || true
    )"
    if [[ "${status}" == "Online" ]]; then
      echo "SSM is online."
      return
    fi
    sleep 10
  done
  echo "Timed out waiting for SSM online status on ${INSTANCE_ID}" >&2
  exit 1
}

send_ssm_script() {
  local label="$1"
  local script_body="$2"
  local timeout_seconds="${3:-3600}"

  local safe_label
  safe_label="$(echo "${label}" | tr -c 'A-Za-z0-9._-' '_')"

  local script_b64
  script_b64="$(
    python3 - "$script_body" <<'PY'
import base64
import sys

print(base64.b64encode(sys.argv[1].encode("utf-8")).decode("ascii"))
PY
  )"

  local script_json
  script_json="$(
    python3 - \
      "$script_b64" \
      "$safe_label" \
      "$APP_DIR" \
      "$ORCHESTRATOR_USER" \
      "$SECRETS_PREFIX" \
      "$RESULTS_BUCKET" \
      "$RUN_ID" \
      "$KALSHI_MARKET_TICKER" \
      "$EVENT_OFFSET_MINUTES" \
      "$AWS_REGION" \
      "$NULLONE_INSTALL_MODE" \
      "$NULLONE_S3_URI" <<'PY'
import json
import shlex
import sys

script_b64 = sys.argv[1]
safe_label = sys.argv[2]
app_dir = sys.argv[3]
orchestrator_user = sys.argv[4]
secrets_prefix = sys.argv[5]
results_bucket = sys.argv[6]
run_id = sys.argv[7]
market_ticker = sys.argv[8]
event_offset_minutes = sys.argv[9]
aws_region = sys.argv[10]
nullone_install_mode = sys.argv[11]
nullone_s3_uri = sys.argv[12]
env_prefix = " ".join(
    [
        f"APP_DIR={shlex.quote(app_dir)}",
        f"ORCHESTRATOR_USER={shlex.quote(orchestrator_user)}",
        f"SECRETS_PREFIX={shlex.quote(secrets_prefix)}",
        f"RESULTS_BUCKET={shlex.quote(results_bucket)}",
        f"RUN_ID={shlex.quote(run_id)}",
        f"KALSHI_MARKET_TICKER={shlex.quote(market_ticker)}",
        f"EVENT_OFFSET_MINUTES={shlex.quote(event_offset_minutes)}",
        f"AWS_REGION={shlex.quote(aws_region)}",
        f"NULLONE_INSTALL_MODE={shlex.quote(nullone_install_mode)}",
        f"NULLONE_S3_URI={shlex.quote(nullone_s3_uri)}",
    ]
)
runner = (
    f"echo '{script_b64}' | base64 -d > /tmp/slp-{safe_label}.sh && "
    f"chmod +x /tmp/slp-{safe_label}.sh && "
    f"{env_prefix} /usr/bin/env bash /tmp/slp-{safe_label}.sh"
)
print(json.dumps([runner]))
PY
  )"

  local command_id
  local send_err
  send_err="$(mktemp)"
  TMP_FILES+=("${send_err}")
  if ! command_id="$(
    aws ssm send-command \
      --instance-ids "${INSTANCE_ID}" \
      --document-name "AWS-RunShellScript" \
      --comment "source-latency-profiler ${label}" \
      --parameters "commands=${script_json}" \
      --timeout-seconds "${timeout_seconds}" \
      --query "Command.CommandId" \
      --output text \
      --region "${AWS_REGION}" 2>"${send_err}"
  )"; then
    local err_msg
    err_msg="$(<"${send_err}")"
    echo "${err_msg}" >&2
    if [[ "${err_msg}" == *"AccessDeniedException"* ]]; then
      echo >&2
      echo "Local caller is missing SSM command permissions." >&2
      echo "Grant at least: ssm:SendCommand, ssm:GetCommandInvocation, ssm:ListCommandInvocations, ssm:CancelCommand on *." >&2
    fi
    exit 1
  fi

  local waiter_failed=0
  if ! aws ssm wait command-executed \
    --command-id "${command_id}" \
    --instance-id "${INSTANCE_ID}" \
    --region "${AWS_REGION}"; then
    waiter_failed=1
  fi

  local status
  status="$(
    aws ssm get-command-invocation \
      --command-id "${command_id}" \
      --instance-id "${INSTANCE_ID}" \
      --query "Status" \
      --output text \
      --region "${AWS_REGION}"
  )"

  echo "----- ${label}: stdout -----"
  aws ssm get-command-invocation \
    --command-id "${command_id}" \
    --instance-id "${INSTANCE_ID}" \
    --query "StandardOutputContent" \
    --output text \
    --region "${AWS_REGION}" || true
  echo
  echo "----- ${label}: stderr -----"
  aws ssm get-command-invocation \
    --command-id "${command_id}" \
    --instance-id "${INSTANCE_ID}" \
    --query "StandardErrorContent" \
    --output text \
    --region "${AWS_REGION}" || true
  echo

  if [[ "${status}" != "Success" ]]; then
    if [[ "${waiter_failed}" == "1" ]]; then
      echo "SSM waiter reported terminal failure for step '${label}'." >&2
    fi
    echo "SSM step '${label}' failed with status=${status}" >&2
    exit 1
  fi
}

run_remote_e2e() {
  local remote_repair
  remote_repair="$(cat <<'EOF'
set -euo pipefail

# Fresh instances can register with SSM before user-data unpack/bootstrap completes.
for _ in $(seq 1 30); do
  if [[ -d "${APP_DIR}" && -f "${APP_DIR}/scripts/deploy_orchestrator_host.sh" ]]; then
    break
  fi
  sleep 10
done

if [[ ! -d "${APP_DIR}" ]]; then
  echo "App dir missing after bootstrap wait: ${APP_DIR}" >&2
  sudo tail -n 120 /var/log/source-latency-profiler-bootstrap.log >&2 || true
  exit 1
fi

if [[ ! -f "${APP_DIR}/scripts/deploy_orchestrator_host.sh" ]]; then
  echo "App files not fully unpacked at ${APP_DIR}." >&2
  sudo tail -n 120 /var/log/source-latency-profiler-bootstrap.log >&2 || true
  exit 1
fi

cd "${APP_DIR}"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Host bootstrap appears incomplete; re-running deploy_orchestrator_host.sh..."
  APP_DIR="${APP_DIR}" \
  ORCHESTRATOR_USER="${ORCHESTRATOR_USER}" \
  RESULTS_BUCKET="${RESULTS_BUCKET}" \
  AWS_REGION="${AWS_REGION}" \
  INSTALL_SCHEDULERS=0 \
  NULLONE_INSTALL_MODE="${NULLONE_INSTALL_MODE}" \
  NULLONE_S3_URI="${NULLONE_S3_URI}" \
  bash scripts/deploy_orchestrator_host.sh
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing ${APP_DIR}/.venv/bin/python after bootstrap repair." >&2
  exit 1
fi
EOF
)"
  send_ssm_script "repair-host-bootstrap" "${remote_repair}" 1800

  local remote_bootstrap
  remote_bootstrap="$(cat <<'EOF'
set -euo pipefail
cd "${APP_DIR}"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing ${APP_DIR}/.venv/bin/python (host bootstrap incomplete)." >&2
  exit 1
fi

mkdir -p .generated/e2e
python3 - <<PY
from datetime import datetime, timedelta, timezone
import pathlib
import yaml

template_path = pathlib.Path("strategies/nullone-wpsr-kalshi-sandbox.yaml")
out_strategy = pathlib.Path(".generated/e2e/live-kalshi-e2e.yaml")
out_calendar = pathlib.Path(".generated/e2e/live-kalshi-e2e-calendar.json")

data = yaml.safe_load(template_path.read_text())
event_time = (datetime.now(timezone.utc) + timedelta(minutes=${EVENT_OFFSET_MINUTES})).replace(microsecond=0).isoformat()

data["strategy"]["name"] = "nullone_live_kalshi_e2e"
data["strategy"]["description"] = "Generated by run_aws_live_kalshi_e2e.sh"
data["strategy"]["event_id"] = "live_kalshi_e2e"
data["strategy"]["event_time"] = event_time
data["strategy"]["release_at_et"] = event_time
data["strategy"]["mode"] = "paper"
data["event"]["source"] = "kalshi_live_probe"

brackets = data["markets"].get("brackets") or []
if not brackets:
    raise SystemExit("Template strategy has no pinned brackets.")
brackets[0]["kalshi"]["market_ticker"] = "${KALSHI_MARKET_TICKER}"
data["markets"]["brackets"] = [brackets[0]]
data["markets"]["source"] = "pinned"
data["markets"]["refresh"]["offsets"] = ["T-15m", "T-2m"]

data["automation"]["warmup_starts_at"] = "T-5m"
data["automation"]["execution_starts_at"] = "T+0"
data["automation"]["latest_deploy_cutoff"] = "T-2m"
data["automation"]["profiler_stops_at"] = "T-1m"

out_strategy.write_text(yaml.safe_dump(data, sort_keys=False))
out_calendar.write_text("[]\n")
print(f"strategy_path={out_strategy}")
print(f"event_time={event_time}")
print(f"market_ticker=${KALSHI_MARKET_TICKER}")
PY
EOF
)"

  send_ssm_script "prepare-live-strategy" "${remote_bootstrap}" 600

  local remote_dry_run
  remote_dry_run="$(cat <<'EOF'
set -euo pipefail
cd "${APP_DIR}"
.venv/bin/python -m profiler.cli scheduler run \
  --strategy .generated/e2e/live-kalshi-e2e.yaml \
  --secrets-prefix "${SECRETS_PREFIX}" \
  --state-path .generated/e2e/live-kalshi-e2e-state.json \
  --events-path .generated/e2e/live-kalshi-e2e-events.jsonl \
  --once \
  --dry-run
EOF
)"
  send_ssm_script "scheduler-dry-run" "${remote_dry_run}" 1200

  local remote_execute
  remote_execute="$(cat <<'EOF'
set -euo pipefail
cd "${APP_DIR}"
.venv/bin/python -m profiler.cli scheduler run \
  --strategy .generated/e2e/live-kalshi-e2e.yaml \
  --secrets-prefix "${SECRETS_PREFIX}" \
  --state-path .generated/e2e/live-kalshi-e2e-state.json \
  --events-path .generated/e2e/live-kalshi-e2e-events.jsonl \
  --results-s3-prefix "s3://${RESULTS_BUCKET}/profiler" \
  --runtime-s3-prefix "s3://${RESULTS_BUCKET}/runtime" \
  --run-id "${RUN_ID}" \
  --teardown-mode after-execution \
  --execution-handoff ssm-immediate \
  --execution-lead-seconds 30 \
  --diagnostics hot-path \
  --once

echo
echo "Recent scheduler events:"
tail -n 20 .generated/e2e/live-kalshi-e2e-events.jsonl || true
EOF
)"
  send_ssm_script "scheduler-execute-once" "${remote_execute}" 7200
}

echo "=== source-latency-profiler live Kalshi AWS E2E ==="
echo "Region: ${AWS_REGION}"
echo "Results bucket: ${RESULTS_BUCKET}"
echo "Run ID: ${RUN_ID}"

pick_live_kalshi_market
launch_orchestrator_if_needed
wait_for_ssm_online
run_remote_e2e

echo
echo "E2E complete."
echo "Instance: ${INSTANCE_ID}"
echo "Run ID: ${RUN_ID}"
echo "Market: ${KALSHI_MARKET_TICKER}"
echo
echo "Next checks:"
echo "  aws s3 ls s3://${RESULTS_BUCKET}/profiler/${RUN_ID}/ --recursive --region ${AWS_REGION}"
echo "  aws s3 ls s3://${RESULTS_BUCKET}/diagnostics/ --recursive --region ${AWS_REGION}"
