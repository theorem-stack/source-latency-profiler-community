#!/usr/bin/env bash
# Runs NullOne execute at runtime.execute_at with an optional lead window.
# Started once after warmup (nohup); avoids SSM agent work at T+0.
set -euo pipefail

CONFIG="/etc/nullone/runtime.json"
LEAD_SECONDS=60
RESULT_PATH="/opt/nullone/execute-result.json"
DIAGNOSTIC="none"
TIMEOUT_SECONDS=900

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --lead-seconds) LEAD_SECONDS="$2"; shift 2 ;;
    --result) RESULT_PATH="$2"; shift 2 ;;
    --diagnostic) DIAGNOSTIC="$2"; shift 2 ;;
    --timeout-seconds) TIMEOUT_SECONDS="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

STDOUT_PATH="/tmp/nullone-scheduled-stdout.txt"
STDERR_PATH="/tmp/nullone-scheduled-stderr.txt"

write_result() {
  local status="$1"
  local exit_code="$2"
  python3 - "$status" "$exit_code" "$RESULT_PATH" "$STDOUT_PATH" "$STDERR_PATH" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

status, exit_code, result_path, stdout_path, stderr_path = sys.argv[1:6]
stdout = Path(stdout_path).read_text(encoding="utf-8") if Path(stdout_path).exists() else ""
stderr = Path(stderr_path).read_text(encoding="utf-8") if Path(stderr_path).exists() else ""
payload = {
    "status": status,
    "exit_code": int(exit_code),
    "output": stdout,
    "error": stderr,
    "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
Path(result_path).parent.mkdir(parents=True, exist_ok=True)
Path(result_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

trap 'write_result failed 1' ERR

python3 - "$CONFIG" "$LEAD_SECONDS" <<'PY'
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

config_path, lead_seconds = sys.argv[1], int(sys.argv[2])
config = json.loads(Path(config_path).read_text(encoding="utf-8"))
execute_at_raw = config.get("runtime", {}).get("execute_at")
if not execute_at_raw:
    raise SystemExit("runtime.execute_at missing from config")
execute_at = datetime.fromisoformat(str(execute_at_raw).replace("Z", "+00:00"))
lead_at = execute_at.timestamp() - lead_seconds
now = datetime.now(timezone.utc).timestamp()
sleep_for = max(0.0, lead_at - now)
if sleep_for:
    time.sleep(sleep_for)
PY

if [[ "$DIAGNOSTIC" == "live-paper" ]]; then
  EXEC_COMMAND=(
    "nullone execute --config ${CONFIG}"
    "--paper-live-release --timeout-seconds ${TIMEOUT_SECONDS} --trace-jsonl"
  )
elif [[ "$DIAGNOSTIC" == "hot-path" ]]; then
  EXEC_COMMAND=(
    "nullone execute --config ${CONFIG}"
    "--diagnostic --simulate packaged --timeout-seconds ${TIMEOUT_SECONDS} --trace-jsonl"
  )
else
  EXEC_COMMAND=("/usr/local/bin/nullone-execute")
fi

source /opt/nullone/runtime.env
set -a
[[ -f /run/secrets/nullone.env ]] && source /run/secrets/nullone.env
set +a

: >"$STDOUT_PATH"
: >"$STDERR_PATH"

if [[ "${NULLONE_ARTIFACT_TYPE:-}" == "container" && "$DIAGNOSTIC" != "none" ]]; then
  INNER="${EXEC_COMMAND[*]}"
  set +e
  timeout "$((TIMEOUT_SECONDS + 15))s" \
    docker exec nullone-runtime sh -lc "$INNER" \
    >"$STDOUT_PATH" 2>"$STDERR_PATH"
  EXIT_CODE=$?
  set -e
elif [[ "${NULLONE_ARTIFACT_TYPE:-}" == "container" ]]; then
  set +e
  timeout "$((TIMEOUT_SECONDS + 15))s" /usr/local/bin/nullone-execute \
    >"$STDOUT_PATH" 2>"$STDERR_PATH"
  EXIT_CODE=$?
  set -e
else
  INNER="${EXEC_COMMAND[*]}"
  set +e
  timeout "$((TIMEOUT_SECONDS + 15))s" bash -lc "$INNER" \
    >"$STDOUT_PATH" 2>"$STDERR_PATH"
  EXIT_CODE=$?
  set -e
fi

if [[ "$EXIT_CODE" -eq 124 ]]; then
  write_result failed 124
  exit 124
fi

if [[ "$EXIT_CODE" -eq 0 ]]; then
  write_result ok 0
  exit 0
fi

write_result failed "$EXIT_CODE"
exit "$EXIT_CODE"
