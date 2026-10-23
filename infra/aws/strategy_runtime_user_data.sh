#!/bin/bash
set -euxo pipefail

mkdir -p /opt/nullone/bootstrap /var/log/nullone
exec > >(tee -a /var/log/nullone/bootstrap.log | logger -t nullone-bootstrap -s 2>/dev/console) 2>&1

bootstrap_event() {
  local stage="$1"
  local status="$2"
  python3 - "$stage" "$status" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path("/opt/nullone/bootstrap/state.json")
path.parent.mkdir(parents=True, exist_ok=True)
event = {
    "stage": sys.argv[1],
    "status": sys.argv[2],
    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
events = []
if path.exists():
    try:
        events = json.loads(path.read_text(encoding="utf-8")).get("events", [])
    except json.JSONDecodeError:
        events = []
events.append(event)
path.write_text(json.dumps({"latest": event, "events": events}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

bootstrap_event "bootstrap" "start"
trap 'bootstrap_event "bootstrap" "failed"' ERR

bootstrap_event "packages" "start"
dnf update -y
dnf install -y awscli docker amazon-ssm-agent
systemctl enable --now amazon-ssm-agent
systemctl enable --now docker
bootstrap_event "packages" "done"

bootstrap_event "kernel_tuning" "start"
cat >/etc/sysctl.d/99-nullone-low-latency.conf <<'EOF'
# Conservative low-latency defaults for short-lived strategy runtimes.
# Validate on target AMI before production; some keys vary by kernel version.
vm.swappiness = 1
net.core.netdev_max_backlog = 250000
net.core.somaxconn = 4096
net.ipv4.tcp_fastopen = 3
net.core.busy_poll = 50
net.core.busy_read = 50
EOF
sysctl --system || true
bootstrap_event "kernel_tuning" "done"

mkdir -p /etc/nullone /opt/nullone /var/log/nullone /run/secrets
chmod 700 /run/secrets
touch /run/secrets/nullone.env
chmod 600 /run/secrets/nullone.env
bootstrap_event "runtime_config_download" "start"
aws s3 cp "s3://${runtime_config_bucket}/${runtime_config_key}" /etc/nullone/runtime.json
bootstrap_event "runtime_config_download" "done"

export NULLONE_RUNTIME_ENV_SECRET="${runtime_env_secret_id}"
export NULLONE_PRIVATE_KEY_SECRET="${private_key_secret_id}"

bootstrap_event "secrets_materialization" "start"
python3 <<'PY'
import json
import os
import re
import subprocess

runtime_env_secret = os.environ.get("NULLONE_RUNTIME_ENV_SECRET", "")
private_key_secret = os.environ.get("NULLONE_PRIVATE_KEY_SECRET", "")

def get_secret(secret_id: str) -> str:
    if not secret_id:
        return ""
    return subprocess.check_output(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", secret_id, "--query", "SecretString", "--output", "text"],
        text=True,
    )

runtime_env = get_secret(runtime_env_secret)
if runtime_env:
    values = json.loads(runtime_env)
    with open("/run/secrets/nullone.env", "w", encoding="utf-8") as handle:
        for key, value in sorted(values.items()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"Invalid environment variable name: {key}")
            normalized = "" if value is None else str(value).replace("\r", "").replace("\n", "\\n")
            handle.write(f"{key}={normalized}\n")
    os.chmod("/run/secrets/nullone.env", 0o600)

private_key = get_secret(private_key_secret)
if private_key:
    with open("/run/secrets/kalshi-private-key.pem", "w", encoding="utf-8") as handle:
        handle.write(private_key)
        handle.write("\n")
    os.chmod("/run/secrets/kalshi-private-key.pem", 0o600)
PY
bootstrap_event "secrets_materialization" "done"

bootstrap_event "runtime_env_write" "start"
cat >/opt/nullone/runtime.env <<'EOF'
AWS_REGION='${aws_region}'
NULLONE_RUNTIME_CONFIG='/etc/nullone/runtime.json'
NULLONE_ARTIFACT_TYPE='${artifact_type}'
NULLONE_ARTIFACT_IMAGE='${artifact_image}'
NULLONE_STARTUP_COMMAND='${startup_command}'
NULLONE_EXECUTION_COMMAND='${execution_command}'
EOF
bootstrap_event "runtime_env_write" "done"

if [ "${artifact_type}" = "container" ] && [ -n "${artifact_image}" ]; then
  bootstrap_event "ecr_login" "start"
  aws ecr get-login-password --region "${aws_region}" | docker login --username AWS --password-stdin "$(echo "${artifact_image}" | cut -d/ -f1)" || true
  bootstrap_event "ecr_login" "done"
  bootstrap_event "docker_pull" "start"
  docker pull "${artifact_image}"
  bootstrap_event "docker_pull" "done"
  bootstrap_event "container_create" "start"
  docker rm -f nullone-runtime >/dev/null 2>&1 || true
  docker create \
    --name nullone-runtime \
    --network host \
    -v /etc/nullone/runtime.json:/etc/nullone/runtime.json:ro \
    -e AWS_REGION="${aws_region}" \
    --env-file /run/secrets/nullone.env \
    -v /run/secrets:/run/secrets:ro \
    --entrypoint sleep \
    "${artifact_image}" \
    infinity
  docker start nullone-runtime
  bootstrap_event "container_create" "done"
  bootstrap_event "wrappers" "start"
  cat >/usr/local/bin/nullone-health <<'EOF'
#!/bin/bash
set -euo pipefail
docker exec nullone-runtime nullone health --config /etc/nullone/runtime.json
EOF
  cat >/usr/local/bin/nullone-ready <<'EOF'
#!/bin/bash
set -euo pipefail
source /opt/nullone/runtime.env
docker exec nullone-runtime sh -lc "$NULLONE_STARTUP_COMMAND"
EOF
  cat >/usr/local/bin/nullone-execute <<'EOF'
#!/bin/bash
set -euo pipefail
source /opt/nullone/runtime.env
docker exec nullone-runtime sh -lc "$NULLONE_EXECUTION_COMMAND"
EOF
  cat >/usr/local/bin/nullone-shutdown <<'EOF'
#!/bin/bash
set -euo pipefail
docker exec nullone-runtime nullone shutdown --config /etc/nullone/runtime.json || true
docker stop nullone-runtime
EOF
else
  bootstrap_event "wrappers" "start"
  cat >/usr/local/bin/nullone-health <<'EOF'
#!/bin/bash
set -euo pipefail
source /opt/nullone/runtime.env
set -a
[ -f /run/secrets/nullone.env ] && source /run/secrets/nullone.env
set +a
nullone health --config /etc/nullone/runtime.json
EOF
  cat >/usr/local/bin/nullone-ready <<'EOF'
#!/bin/bash
set -euo pipefail
source /opt/nullone/runtime.env
set -a
[ -f /run/secrets/nullone.env ] && source /run/secrets/nullone.env
set +a
sh -lc "$NULLONE_STARTUP_COMMAND"
EOF
  cat >/usr/local/bin/nullone-execute <<'EOF'
#!/bin/bash
set -euo pipefail
source /opt/nullone/runtime.env
set -a
[ -f /run/secrets/nullone.env ] && source /run/secrets/nullone.env
set +a
sh -lc "$NULLONE_EXECUTION_COMMAND"
EOF
  cat >/usr/local/bin/nullone-shutdown <<'EOF'
#!/bin/bash
set -euo pipefail
source /opt/nullone/runtime.env
set -a
[ -f /run/secrets/nullone.env ] && source /run/secrets/nullone.env
set +a
nullone shutdown --config /etc/nullone/runtime.json
EOF
fi

chmod +x /usr/local/bin/nullone-health /usr/local/bin/nullone-ready /usr/local/bin/nullone-execute /usr/local/bin/nullone-shutdown
bootstrap_event "wrappers" "done"

bootstrap_event "execute_scheduled_install" "start"
echo "${execute_scheduled_script_b64}" | base64 -d >/usr/local/bin/nullone-execute-scheduled
chmod +x /usr/local/bin/nullone-execute-scheduled
bootstrap_event "execute_scheduled_install" "done"

bootstrap_event "ready_service" "start"
cat >/etc/systemd/system/nullone-ready.service <<'EOF'
[Unit]
Description=NullOne strategy warmup
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/opt/nullone/runtime.env
ExecStart=/usr/local/bin/nullone-ready
StandardOutput=append:/var/log/nullone/ready.log
StandardError=append:/var/log/nullone/ready.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable nullone-ready.service
systemctl start nullone-ready.service
bootstrap_event "ready_service" "done"

cat >/etc/motd <<EOF
NullOne strategy runtime
Runtime config: s3://${runtime_config_bucket}/${runtime_config_key}

Warmup logs: /var/log/nullone/ready.log
Health command: /usr/local/bin/nullone-health
Manual execution command: /usr/local/bin/nullone-execute
Shutdown command: /usr/local/bin/nullone-shutdown
EOF

trap - ERR
bootstrap_event "bootstrap" "done"
