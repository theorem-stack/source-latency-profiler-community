#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/source-latency-profiler}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-source-latency-profiler}"
RUN_ID="${RUN_ID:-}"
AUTO_APPROVE=false

usage() {
  cat <<'EOF'
Usage:
  scripts/teardown_strategy_runtime.sh --run-id <run-id> [--yes]

Environment overrides:
  APP_DIR       Default: /opt/source-latency-profiler
  AWS_REGION    Default: us-east-1
  PROJECT_NAME  Default: source-latency-profiler

Behavior:
  1. If the generated tfvars file exists, run OpenTofu destroy.
  2. Otherwise, clean up the strategy runtime EC2, security group,
     instance profile, IAM role policy, SSM policy attachment, and IAM role
     by the standard resource name: <project>-strategy-<run-id>.

Use --yes to skip prompts.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      RUN_ID="${2:-}"
      shift 2
      ;;
    --yes|-y)
      AUTO_APPROVE=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$RUN_ID" ]]; then
  echo "Missing --run-id" >&2
  usage >&2
  exit 2
fi

RESOURCE_NAME="${PROJECT_NAME}-strategy-${RUN_ID}"
TFVARS_PATH="${APP_DIR}/.generated/${RUN_ID}/strategy-runtime.tfvars.json"

confirm() {
  if [[ "$AUTO_APPROVE" == "true" ]]; then
    return 0
  fi

  read -r -p "Tear down strategy runtime ${RESOURCE_NAME}? Type 'yes' to continue: " answer
  [[ "$answer" == "yes" ]]
}

run_tofu_destroy_if_possible() {
  if [[ ! -f "$TFVARS_PATH" ]]; then
    return 1
  fi

  if [[ ! -d "${APP_DIR}/infra/aws" ]]; then
    return 1
  fi

  echo "Found tfvars: ${TFVARS_PATH}"
  echo "Attempting OpenTofu destroy first."
  if [[ "$AUTO_APPROVE" == "true" ]]; then
    tofu -chdir="${APP_DIR}/infra/aws" destroy -auto-approve -var-file="$TFVARS_PATH"
  else
    tofu -chdir="${APP_DIR}/infra/aws" destroy -var-file="$TFVARS_PATH"
  fi
}

aws_text() {
  aws "$@" --output text 2>/dev/null || true
}

cleanup_instances() {
  local instance_ids
  instance_ids="$(aws_text ec2 describe-instances \
    --region "$AWS_REGION" \
    --filters "Name=tag:RunId,Values=${RUN_ID}" "Name=tag:Component,Values=strategy-runtime" \
    --query "Reservations[].Instances[?State.Name!='terminated'].InstanceId")"

  if [[ -z "$instance_ids" || "$instance_ids" == "None" ]]; then
    echo "No live strategy runtime EC2 instances found for ${RUN_ID}."
    return
  fi

  echo "Terminating EC2 instances: ${instance_ids}"
  aws ec2 terminate-instances --region "$AWS_REGION" --instance-ids $instance_ids >/dev/null
  echo "Waiting for EC2 termination..."
  aws ec2 wait instance-terminated --region "$AWS_REGION" --instance-ids $instance_ids
}

cleanup_iam() {
  echo "Cleaning IAM resources for ${RESOURCE_NAME}..."

  aws iam detach-role-policy \
    --role-name "$RESOURCE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null 2>&1 || true

  aws iam delete-role-policy \
    --role-name "$RESOURCE_NAME" \
    --policy-name strategy-runtime-access >/dev/null 2>&1 || true

  aws iam remove-role-from-instance-profile \
    --instance-profile-name "$RESOURCE_NAME" \
    --role-name "$RESOURCE_NAME" >/dev/null 2>&1 || true

  aws iam delete-instance-profile \
    --instance-profile-name "$RESOURCE_NAME" >/dev/null 2>&1 || true

  aws iam delete-role \
    --role-name "$RESOURCE_NAME" >/dev/null 2>&1 || true
}

cleanup_security_group() {
  local group_ids
  group_ids="$(aws_text ec2 describe-security-groups \
    --region "$AWS_REGION" \
    --filters "Name=group-name,Values=${RESOURCE_NAME}" \
    --query "SecurityGroups[].GroupId")"

  if [[ -z "$group_ids" || "$group_ids" == "None" ]]; then
    echo "No strategy runtime security groups found for ${RESOURCE_NAME}."
    return
  fi

  for group_id in $group_ids; do
    echo "Deleting security group: ${group_id}"
    aws ec2 delete-security-group --region "$AWS_REGION" --group-id "$group_id" >/dev/null 2>&1 || {
      echo "Could not delete security group ${group_id}; it may still be attached. Retry after EC2 termination completes." >&2
      return 1
    }
  done
}

verify_cleanup() {
  echo "Verifying cleanup for ${RUN_ID}..."
  aws ec2 describe-instances \
    --region "$AWS_REGION" \
    --filters "Name=tag:RunId,Values=${RUN_ID}" "Name=tag:Component,Values=strategy-runtime" \
    --query "Reservations[].Instances[].{InstanceId:InstanceId,State:State.Name}" \
    --output table || true

  aws ec2 describe-security-groups \
    --region "$AWS_REGION" \
    --filters "Name=group-name,Values=${RESOURCE_NAME}" \
    --query "SecurityGroups[].{GroupId:GroupId,GroupName:GroupName}" \
    --output table || true

  aws iam get-role --role-name "$RESOURCE_NAME" >/dev/null 2>&1 && \
    echo "IAM role still exists: ${RESOURCE_NAME}" || \
    echo "IAM role removed or absent: ${RESOURCE_NAME}"
}

if ! confirm; then
  echo "Cancelled."
  exit 1
fi

if run_tofu_destroy_if_possible; then
  echo "OpenTofu destroy completed."
  exit 0
fi

echo "OpenTofu destroy was not available. Falling back to AWS CLI cleanup."
cleanup_instances
cleanup_iam
cleanup_security_group
verify_cleanup
echo "Teardown complete for ${RUN_ID}."
