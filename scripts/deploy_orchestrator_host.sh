#!/usr/bin/env bash
set -euxo pipefail

APP_DIR="${APP_DIR:-/opt/source-latency-profiler}"
ORCHESTRATOR_USER="${ORCHESTRATOR_USER:-ec2-user}"
SERVICE_NAME="${SERVICE_NAME:-source-latency-profiler-orchestrator.service}"
AWS_REGION="${AWS_REGION:-us-east-1}"
REPO_URL="${REPO_URL:-}"
GIT_REF="${GIT_REF:-main}"
CONFIG_DIR="${CONFIG_DIR:-/etc/source-latency-profiler}"
DATA_DIR="${DATA_DIR:-/var/lib/source-latency-profiler}"
LOG_DIR="${LOG_DIR:-/var/log/source-latency-profiler}"
NULLONE_ENV_FILE="${NULLONE_ENV_FILE:-${CONFIG_DIR}/nullone.env}"
NULLONE_BIN="${NULLONE_BIN:-/usr/local/bin/nullone}"
NULLONE_ROOT="${NULLONE_ROOT:-/opt/nullone}"
NULLONE_INSTALL_MODE="${NULLONE_INSTALL_MODE:-skip}"
NULLONE_S3_URI="${NULLONE_S3_URI:-}"
NULLONE_DOWNLOAD_URL="${NULLONE_DOWNLOAD_URL:-}"
NULLONE_FORCE_INSTALL="${NULLONE_FORCE_INSTALL:-0}"
SCHEDULER_SECRETS_PREFIX="${SCHEDULER_SECRETS_PREFIX:-nullone/prod}"

install_packages_dnf() {
  local -a base_packages=(
    awscli
    git
    tar
    gzip
    ca-certificates
    amazon-ssm-agent
  )
  local -a python_packages=(python3.12 python3.12-pip)
  local -a fallback_python_packages=(python3.11 python3.11-pip)

  # AL2023 usually ships curl-minimal; prefer it to avoid package conflicts.
  if sudo dnf install -y "${base_packages[@]}" curl-minimal "${python_packages[@]}"; then
    return
  fi
  if sudo dnf install -y "${base_packages[@]}" curl-minimal "${fallback_python_packages[@]}"; then
    return
  fi

  # Fallback path for hosts that only provide full curl (or already have it).
  if sudo dnf install -y --allowerasing "${base_packages[@]}" curl "${python_packages[@]}"; then
    return
  fi
  sudo dnf install -y --allowerasing "${base_packages[@]}" curl "${fallback_python_packages[@]}"
}

install_packages_yum() {
  local -a base_packages=(
    awscli
    git
    tar
    gzip
    ca-certificates
    amazon-ssm-agent
    curl
  )
  if sudo yum install -y "${base_packages[@]}" python3.12 python3.12-pip; then
    return
  fi
  sudo yum install -y "${base_packages[@]}" python3.11 python3.11-pip
}

install_packages() {
  if command -v dnf >/dev/null 2>&1; then
    install_packages_dnf
    return
  fi

  if command -v yum >/dev/null 2>&1; then
    install_packages_yum
    return
  fi

  echo "Unsupported host: dnf or yum is required" >&2
  exit 1
}

install_opentofu() {
  if command -v tofu >/dev/null 2>&1; then
    tofu version
    return
  fi

  write_opentofu_repo 1
  sudo rpm --import https://packages.opentofu.org/opentofu/tofu/gpgkey || true

  if ! install_tofu_package; then
    echo "Signed OpenTofu RPM install failed; retrying with repo GPG checks disabled for bootstrap" >&2
    write_opentofu_repo 0
    install_tofu_package
  fi

  tofu version
}

write_opentofu_repo() {
  local gpgcheck="$1"

  cat <<'EOF' | sudo tee /etc/yum.repos.d/opentofu.repo >/dev/null
[opentofu]
name=opentofu
baseurl=https://packages.opentofu.org/opentofu/tofu/rpm_any/rpm_any/$basearch
repo_gpgcheck=0
gpgcheck=GPGCHECK_VALUE
enabled=1
gpgkey=https://packages.opentofu.org/opentofu/tofu/gpgkey
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
metadata_expire=300

[opentofu-source]
name=opentofu-source
baseurl=https://packages.opentofu.org/opentofu/tofu/rpm_any/rpm_any/SRPMS
repo_gpgcheck=0
gpgcheck=GPGCHECK_VALUE
enabled=1
gpgkey=https://packages.opentofu.org/opentofu/tofu/gpgkey
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
metadata_expire=300
EOF
  sudo sed -i "s/GPGCHECK_VALUE/${gpgcheck}/g" /etc/yum.repos.d/opentofu.repo
}

install_tofu_package() {
  if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y tofu
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y tofu
  else
    echo "Unsupported host: cannot install OpenTofu without dnf or yum" >&2
    exit 1
  fi
}

select_python() {
  if command -v python3.12 >/dev/null 2>&1; then
    command -v python3.12
    return
  fi

  if command -v python3.11 >/dev/null 2>&1; then
    command -v python3.11
    return
  fi

  echo "Python 3.11+ is required" >&2
  exit 1
}

finalize_ownership() {
  if id "$ORCHESTRATOR_USER" >/dev/null 2>&1; then
    sudo chown -R "$ORCHESTRATOR_USER:$ORCHESTRATOR_USER" "$APP_DIR" "$DATA_DIR" "$LOG_DIR"
  else
    echo "Orchestrator user ${ORCHESTRATOR_USER} does not exist; leaving ${APP_DIR} owned by deploy user"
  fi
}

prepare_directories() {
  sudo mkdir -p "$APP_DIR" "$CONFIG_DIR" "$DATA_DIR/scheduler" "$LOG_DIR"
  sudo chmod 755 "$APP_DIR" "$CONFIG_DIR" "$DATA_DIR" "$DATA_DIR/scheduler" "$LOG_DIR"
}

install_nullone_if_needed() {
  local needs_install=0

  if [[ "${NULLONE_FORCE_INSTALL}" == "1" ]]; then
    needs_install=1
  elif [[ -x "${NULLONE_BIN}" ]]; then
    needs_install=0
  else
    needs_install=1
  fi

  if [[ "${needs_install}" == "0" ]]; then
    return
  fi

  case "${NULLONE_INSTALL_MODE}" in
    skip)
      ;;
    s3)
      if [[ -z "${NULLONE_S3_URI}" ]]; then
        echo "NULLONE_INSTALL_MODE=s3 requires NULLONE_S3_URI" >&2
        exit 1
      fi
      aws s3 cp "${NULLONE_S3_URI}" /tmp/nullone --region "${AWS_REGION}"
      if aws s3 cp "${NULLONE_S3_URI}.sha256" /tmp/nullone.sha256 --region "${AWS_REGION}" >/dev/null 2>&1; then
        (
          cd /tmp
          sha256sum -c nullone.sha256
        )
        rm -f /tmp/nullone.sha256
      fi
      sudo install -m 0755 /tmp/nullone "${NULLONE_BIN}"
      rm -f /tmp/nullone
      ;;
    url)
      if [[ -z "${NULLONE_DOWNLOAD_URL}" ]]; then
        echo "NULLONE_INSTALL_MODE=url requires NULLONE_DOWNLOAD_URL" >&2
        exit 1
      fi
      curl -fsSL "${NULLONE_DOWNLOAD_URL}" -o /tmp/nullone
      sudo install -m 0755 /tmp/nullone "${NULLONE_BIN}"
      rm -f /tmp/nullone
      ;;
    *)
      echo "Unsupported NULLONE_INSTALL_MODE=${NULLONE_INSTALL_MODE} (expected skip, s3, or url)." >&2
      exit 1
      ;;
  esac
}

write_nullone_env_file() {
  sudo mkdir -p "$(dirname "${NULLONE_ENV_FILE}")"
  cat <<EOF | sudo tee "${NULLONE_ENV_FILE}" >/dev/null
NULLONE_BIN=${NULLONE_BIN}
NULLONE_ROOT=${NULLONE_ROOT}
EOF
  sudo chmod 0644 "${NULLONE_ENV_FILE}"
}

validate_nullone_ready() {
  if [[ ! -x "${NULLONE_BIN}" ]]; then
    echo "NullOne binary not found at ${NULLONE_BIN}." >&2
    echo "Set NULLONE_BIN to an existing path, or configure NULLONE_INSTALL_MODE=s3/url." >&2
    exit 1
  fi
  if ! "${NULLONE_BIN}" --help >/dev/null 2>&1; then
    echo "NullOne binary exists but failed '--help': ${NULLONE_BIN}" >&2
    exit 1
  fi
}

probe_scheduler_secret_access() {
  local prefix="$1"
  local runtime_secret="${prefix%/}/runtime/env"
  if ! aws secretsmanager describe-secret --secret-id "${runtime_secret}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    echo "Unable to describe scheduler runtime secret: ${runtime_secret}" >&2
    echo "Verify instance role permissions for secretsmanager:DescribeSecret/GetSecretValue." >&2
    exit 1
  fi
}

ensure_scheduler_paths_writable() {
  local owner="$1"
  local state_path="${DATA_DIR}/scheduler/.write-check"
  local events_path="${LOG_DIR}/.write-check"
  if ! sudo -u "${owner}" sh -c "touch '${state_path}' && rm -f '${state_path}'"; then
    echo "Scheduler state path is not writable by ${owner}: ${DATA_DIR}/scheduler" >&2
    echo "Fix: sudo chown -R ${owner}:${owner} ${DATA_DIR}" >&2
    exit 1
  fi
  if ! sudo -u "${owner}" sh -c "touch '${events_path}' && rm -f '${events_path}'"; then
    echo "Scheduler events path is not writable by ${owner}: ${LOG_DIR}" >&2
    echo "Fix: sudo chown -R ${owner}:${owner} ${LOG_DIR}" >&2
    exit 1
  fi
}

ensure_nullone_specs_writable() {
  local owner="$1"
  local specs_dir="${NULLONE_ROOT}/specs"
  local probe_path="${specs_dir}/.write-check"
  sudo mkdir -p "${specs_dir}"
  sudo chown -R "${owner}:${owner}" "${NULLONE_ROOT}"
  sudo chmod -R u+rwX "${NULLONE_ROOT}"
  if ! sudo -u "${owner}" sh -c "touch '${probe_path}' && rm -f '${probe_path}'"; then
    echo "NullOne specs path is not writable by ${owner}: ${specs_dir}" >&2
    echo "Fix: set NULLONE_ROOT to a writable location or run sudo chown -R ${owner}:${owner} ${NULLONE_ROOT}" >&2
    exit 1
  fi
}

sync_repo_if_configured() {
  if [[ -z "$REPO_URL" ]]; then
    return
  fi

  if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" fetch --prune origin
    git -C "$APP_DIR" checkout "$GIT_REF"
    git -C "$APP_DIR" pull --ff-only origin "$GIT_REF" || true
    return
  fi

  if [[ -z "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
    git clone --branch "$GIT_REF" "$REPO_URL" "$APP_DIR"
    return
  fi

  echo "${APP_DIR} is not empty and is not a git checkout; skipping clone"
}

install_packages
install_opentofu
sudo systemctl enable --now amazon-ssm-agent || true
prepare_directories
sync_repo_if_configured
install_nullone_if_needed
write_nullone_env_file

cd "$APP_DIR"

PYTHON_BIN="$(select_python)"
"$PYTHON_BIN" -c 'import sys; print(sys.version); assert sys.version_info >= (3, 11)'

rm -rf .venv
"$PYTHON_BIN" -m venv .venv
. .venv/bin/activate

python -c 'import sys; print(sys.version); assert sys.version_info >= (3, 11)'
python -m pip install --upgrade pip
python -m pip install -e '.[dev,ws,aws]'

sudo systemctl daemon-reload
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}"; then
  sudo systemctl restart "$SERVICE_NAME"
else
  echo "${SERVICE_NAME} not installed; deploy updated code only"
fi

# Opt-in: regenerate and install all strategy systemd units.
# Use: INSTALL_SCHEDULERS=1 RESULTS_BUCKET=my-bucket ./deploy_orchestrator_host.sh
if [[ "${INSTALL_SCHEDULERS:-0}" == "1" ]]; then
  if ! id "${ORCHESTRATOR_USER}" >/dev/null 2>&1; then
    echo "Orchestrator user ${ORCHESTRATOR_USER} does not exist; cannot validate scheduler write permissions." >&2
    exit 1
  fi
  # Ensure durable paths are writable before preflight probes and timer install.
  finalize_ownership
  validate_nullone_ready
  ensure_scheduler_paths_writable "${ORCHESTRATOR_USER}"
  ensure_nullone_specs_writable "${ORCHESTRATOR_USER}"
  probe_scheduler_secret_access "${SCHEDULER_SECRETS_PREFIX}"
  echo "Running scheduler unit installer (requires RESULTS_BUCKET)..."
  sudo APP_DIR="${APP_DIR}" \
    RESULTS_BUCKET="${RESULTS_BUCKET:?}" \
    AWS_REGION="${AWS_REGION}" \
    SCHEDULER_STATE_DIR="${DATA_DIR}/scheduler" \
    SCHEDULER_EVENTS_DIR="${LOG_DIR}" \
    RUN_AS_USER="${ORCHESTRATOR_USER}" \
    NULLONE_ENV_FILE="${NULLONE_ENV_FILE}" \
    "${APP_DIR}/scripts/install-systemd-schedulers.sh"
fi

finalize_ownership
