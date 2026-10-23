#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-source-latency-profiler}"
APP_DIR="${APP_DIR:-/opt/source-latency-profiler}"
ORCHESTRATOR_USER="${ORCHESTRATOR_USER:-ec2-user}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.small}"
VOLUME_SIZE_GB="${VOLUME_SIZE_GB:-30}"
GIT_REF="${GIT_REF:-main}"
INSTALL_SCHEDULERS="${INSTALL_SCHEDULERS:-0}"
CREATE_RESULTS_BUCKET="${CREATE_RESULTS_BUCKET:-1}"
WAIT_FOR_SSM="${WAIT_FOR_SSM:-1}"
ENABLE_PREFLIGHT="${ENABLE_PREFLIGHT:-1}"
DRY_RUN="${DRY_RUN:-0}"
DEPLOY_SOURCE_MODE="${DEPLOY_SOURCE_MODE:-artifact}"
ARTIFACT_PREFIX="${ARTIFACT_PREFIX:-bootstrap/deployments}"
NULLONE_INSTALL_MODE="${NULLONE_INSTALL_MODE:-auto}"
NULLONE_BIN_PATH="${NULLONE_BIN_PATH:-/usr/local/bin/nullone}"
NULLONE_ROOT="${NULLONE_ROOT:-/opt/nullone}"
NULLONE_S3_URI="${NULLONE_S3_URI:-}"
NULLONE_DOWNLOAD_URL="${NULLONE_DOWNLOAD_URL:-}"
NULLONE_FORCE_INSTALL="${NULLONE_FORCE_INSTALL:-0}"
NULLONE_BIN_LOCAL_PATH="${NULLONE_BIN_LOCAL_PATH:-}"
NULLONE_RELEASE_PREFIX="${NULLONE_RELEASE_PREFIX:-nullone/releases}"

ROLE_NAME="${ROLE_NAME:-${PROJECT_NAME}-orchestrator}"
INSTANCE_PROFILE_NAME="${INSTANCE_PROFILE_NAME:-${ROLE_NAME}}"
SECURITY_GROUP_NAME="${SECURITY_GROUP_NAME:-${PROJECT_NAME}-orchestrator}"
INSTANCE_NAME="${INSTANCE_NAME:-${PROJECT_NAME}-orchestrator}"
SERVICE_NAME="${SERVICE_NAME:-${PROJECT_NAME}-orchestrator.service}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

json_quote() {
  python3 - "$1" <<'PY'
import json
import sys

print(json.dumps(sys.argv[1]))
PY
}

require_command aws
require_command python3

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${AWS_REGION}")"
RESULTS_BUCKET="${RESULTS_BUCKET:-${PROJECT_NAME}-results-${ACCOUNT_ID}}"
DEPLOY_ARTIFACT_S3_URI="${DEPLOY_ARTIFACT_S3_URI:-}"
EFFECTIVE_NULLONE_INSTALL_MODE="${NULLONE_INSTALL_MODE}"
NULLONE_S3_BUCKET=""
NULLONE_S3_KEY=""

if [[ "${DEPLOY_SOURCE_MODE}" == "git" ]]; then
  if [[ -z "${REPO_URL:-}" ]]; then
    if command -v git >/dev/null 2>&1 && git config --get remote.origin.url >/dev/null 2>&1; then
      REPO_URL="$(git config --get remote.origin.url)"
    else
      echo "Set REPO_URL to the repository URL the EC2 host should clone." >&2
      exit 1
    fi
  fi
elif [[ "${DEPLOY_SOURCE_MODE}" != "artifact" ]]; then
  echo "Unsupported DEPLOY_SOURCE_MODE=${DEPLOY_SOURCE_MODE} (expected artifact or git)." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

ASSUME_ROLE_POLICY="${TMP_DIR}/assume-role-policy.json"
INLINE_POLICY="${TMP_DIR}/orchestrator-policy.json"
USER_DATA="${TMP_DIR}/user-data.sh"

cat >"${ASSUME_ROLE_POLICY}" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "ec2.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
JSON

cat >"${INLINE_POLICY}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "StrategyRuntimeEc2",
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:CreateSecurityGroup",
        "ec2:DeleteSecurityGroup",
        "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupEgress",
        "ec2:CreateTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "StrategyRuntimeIam",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PassRole",
        "iam:CreateInstanceProfile",
        "iam:DeleteInstanceProfile",
        "iam:GetInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile",
        "iam:ListInstanceProfilesForRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy"
      ],
      "Resource": [
        "arn:aws:iam::${ACCOUNT_ID}:role/${PROJECT_NAME}-strategy-*",
        "arn:aws:iam::${ACCOUNT_ID}:instance-profile/${PROJECT_NAME}-strategy-*"
      ]
    },
    {
      "Sid": "RuntimeArtifactsAndDiagnostics",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:AbortMultipartUpload"
      ],
      "Resource": [
        "arn:aws:s3:::${RESULTS_BUCKET}/profiler/*",
        "arn:aws:s3:::${RESULTS_BUCKET}/runtime/*",
        "arn:aws:s3:::${RESULTS_BUCKET}/diagnostics/*",
        "arn:aws:s3:::${RESULTS_BUCKET}/${ARTIFACT_PREFIX}/*",
        "arn:aws:s3:::${RESULTS_BUCKET}/${NULLONE_RELEASE_PREFIX}/*"
      ]
    },
    {
      "Sid": "RuntimeBucketList",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucket*"
      ],
      "Resource": "arn:aws:s3:::${RESULTS_BUCKET}"
    },
    {
      "Sid": "EcrRead",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:DescribeImages",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RuntimeSecretsRead",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:ListSecrets"
      ],
      "Resource": "arn:aws:secretsmanager:${AWS_REGION}:${ACCOUNT_ID}:secret:nullone/*"
    },
    {
      "Sid": "RuntimeSsmControl",
      "Effect": "Allow",
      "Action": [
        "ssm:SendCommand",
        "ssm:GetCommandInvocation",
        "ssm:ListCommandInvocations",
        "ssm:DescribeInstanceInformation",
        "ssm:CancelCommand"
      ],
      "Resource": "*"
    }
  ]
}
JSON

ensure_results_bucket() {
  if [[ "${CREATE_RESULTS_BUCKET}" != "1" ]]; then
    return
  fi

  if aws s3api head-bucket --bucket "${RESULTS_BUCKET}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    echo "Using existing results bucket: ${RESULTS_BUCKET}"
    return
  fi

  echo "Creating results bucket: ${RESULTS_BUCKET}"
  if [[ "${AWS_REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${RESULTS_BUCKET}" --region "${AWS_REGION}" >/dev/null
  else
    aws s3api create-bucket \
      --bucket "${RESULTS_BUCKET}" \
      --region "${AWS_REGION}" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
  fi
  aws s3api put-public-access-block \
    --bucket "${RESULTS_BUCKET}" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true \
    --region "${AWS_REGION}"
  aws s3api put-bucket-encryption \
    --bucket "${RESULTS_BUCKET}" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
    --region "${AWS_REGION}"
  aws s3api put-bucket-versioning \
    --bucket "${RESULTS_BUCKET}" \
    --versioning-configuration Status=Enabled \
    --region "${AWS_REGION}"
}

ensure_iam() {
  if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
    echo "Using existing IAM role: ${ROLE_NAME}"
  else
    echo "Creating IAM role: ${ROLE_NAME}"
    aws iam create-role \
      --role-name "${ROLE_NAME}" \
      --assume-role-policy-document "file://${ASSUME_ROLE_POLICY}" >/dev/null
  fi

  aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null
  aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "${PROJECT_NAME}-orchestrator-access" \
    --policy-document "file://${INLINE_POLICY}" >/dev/null

  if aws iam get-instance-profile --instance-profile-name "${INSTANCE_PROFILE_NAME}" >/dev/null 2>&1; then
    echo "Using existing instance profile: ${INSTANCE_PROFILE_NAME}"
  else
    echo "Creating instance profile: ${INSTANCE_PROFILE_NAME}"
    aws iam create-instance-profile --instance-profile-name "${INSTANCE_PROFILE_NAME}" >/dev/null
  fi

  if ! aws iam get-instance-profile \
    --instance-profile-name "${INSTANCE_PROFILE_NAME}" \
    --query "InstanceProfile.Roles[?RoleName=='${ROLE_NAME}'].RoleName" \
    --output text | grep -q "${ROLE_NAME}"; then
    aws iam add-role-to-instance-profile \
      --instance-profile-name "${INSTANCE_PROFILE_NAME}" \
      --role-name "${ROLE_NAME}" >/dev/null
  fi
}

parse_s3_uri() {
  local uri="$1"
  local without_scheme
  without_scheme="${uri#s3://}"
  if [[ "${without_scheme}" == "${uri}" || "${without_scheme}" != */* ]]; then
    return 1
  fi
  NULLONE_S3_BUCKET="${without_scheme%%/*}"
  NULLONE_S3_KEY="${without_scheme#*/}"
  [[ -n "${NULLONE_S3_BUCKET}" && -n "${NULLONE_S3_KEY}" ]]
}

validate_nullone_s3_uri_contract() {
  if [[ -z "${NULLONE_S3_URI}" ]]; then
    echo "NULLONE_S3_URI is required when NULLONE_INSTALL_MODE=s3." >&2
    exit 1
  fi
  if ! parse_s3_uri "${NULLONE_S3_URI}"; then
    echo "Invalid NULLONE_S3_URI (expected s3://bucket/key): ${NULLONE_S3_URI}" >&2
    exit 1
  fi
  if [[ "${NULLONE_S3_BUCKET}" != "${RESULTS_BUCKET}" ]]; then
    echo "NULLONE_S3_URI bucket must match RESULTS_BUCKET for managed IAM policy." >&2
    echo "RESULTS_BUCKET=${RESULTS_BUCKET}, NULLONE_S3_URI bucket=${NULLONE_S3_BUCKET}" >&2
    exit 1
  fi
  if [[ "${NULLONE_S3_KEY}" != ${NULLONE_RELEASE_PREFIX}/* ]]; then
    echo "NULLONE_S3_URI must live under s3://${RESULTS_BUCKET}/${NULLONE_RELEASE_PREFIX}/" >&2
    exit 1
  fi
  if [[ "${NULLONE_S3_KEY}" == */latest/* ]]; then
    echo "NULLONE_S3_URI cannot use mutable 'latest' path; use an immutable versioned release key." >&2
    exit 1
  fi
  if [[ "${NULLONE_S3_KEY}" != */nullone ]]; then
    echo "NULLONE_S3_URI must point to the executable object ending with '/nullone'." >&2
    exit 1
  fi
}

preflight_permissions() {
  if [[ "${ENABLE_PREFLIGHT}" != "1" ]]; then
    echo "Skipping preflight permission check (ENABLE_PREFLIGHT=${ENABLE_PREFLIGHT})."
    return
  fi

  local caller_arn role_arn profile_arn bucket_arn
  local deny_count=0
  caller_arn="$(aws sts get-caller-identity --query Arn --output text --region "${AWS_REGION}")"
  role_arn="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
  profile_arn="arn:aws:iam::${ACCOUNT_ID}:instance-profile/${INSTANCE_PROFILE_NAME}"
  bucket_arn="arn:aws:s3:::${RESULTS_BUCKET}"

  echo "Running IAM preflight for ${caller_arn}..."

  _simulate_allowed() {
    local action="$1"
    local resource="$2"
    local decision
    decision="$(aws iam simulate-principal-policy \
      --policy-source-arn "${caller_arn}" \
      --action-names "${action}" \
      --resource-arns "${resource}" \
      --query "EvaluationResults[0].EvalDecision" \
      --output text 2>/dev/null || true)"
    [[ "${decision}" == "allowed" ]]
  }

  _simulate_available() {
    aws iam simulate-principal-policy \
      --policy-source-arn "${caller_arn}" \
      --action-names "ec2:DescribeImages" \
      --resource-arns "*" \
      --query "EvaluationResults[0].EvalDecision" \
      --output text >/dev/null 2>&1
  }

  # If simulation API itself is blocked, fall back to runtime checks only.
  if ! _simulate_available; then
    echo "Warning: iam:SimulatePrincipalPolicy unavailable; running best-effort API checks instead." >&2
    if ! aws ec2 describe-vpcs --max-items 1 --region "${AWS_REGION}" >/dev/null 2>&1; then
      echo "Missing permission: ec2:DescribeVpcs" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! aws ec2 describe-subnets --max-items 1 --region "${AWS_REGION}" >/dev/null 2>&1; then
      echo "Missing permission: ec2:DescribeSubnets" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! aws ec2 describe-images --owners amazon --max-items 1 --region "${AWS_REGION}" >/dev/null 2>&1; then
      echo "Missing permission: ec2:DescribeImages" >&2
      deny_count=$((deny_count + 1))
    fi
    if [[ "${WAIT_FOR_SSM}" == "1" ]] && ! aws ssm describe-instance-information --max-results 1 --region "${AWS_REGION}" >/dev/null 2>&1; then
      echo "Missing permission: ssm:DescribeInstanceInformation" >&2
      deny_count=$((deny_count + 1))
    fi
    if [[ "${DEPLOY_SOURCE_MODE}" == "artifact" ]] && ! aws s3api put-object --bucket "${RESULTS_BUCKET}" --key "${ARTIFACT_PREFIX}/preflight-check" --body /dev/null --region "${AWS_REGION}" >/dev/null 2>&1; then
      echo "Missing permission: s3:PutObject on s3://${RESULTS_BUCKET}/${ARTIFACT_PREFIX}/*" >&2
      deny_count=$((deny_count + 1))
    else
      if [[ "${DEPLOY_SOURCE_MODE}" == "artifact" ]]; then
        aws s3api delete-object --bucket "${RESULTS_BUCKET}" --key "${ARTIFACT_PREFIX}/preflight-check" --region "${AWS_REGION}" >/dev/null 2>&1 || true
      fi
    fi
    if [[ "${EFFECTIVE_NULLONE_INSTALL_MODE}" == "s3" ]] && ! aws s3api head-object --bucket "${NULLONE_S3_BUCKET}" --key "${NULLONE_S3_KEY}" --region "${AWS_REGION}" >/dev/null 2>&1; then
      echo "Missing permission (or missing object): s3:GetObject on ${NULLONE_S3_URI}" >&2
      deny_count=$((deny_count + 1))
    fi
  else
    # Core calls this deploy script performs directly.
    if ! _simulate_allowed "ec2:DescribeVpcs" "*"; then
      echo "Missing permission: ec2:DescribeVpcs" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "ec2:DescribeSubnets" "*"; then
      echo "Missing permission: ec2:DescribeSubnets" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "ec2:DescribeSecurityGroups" "*"; then
      echo "Missing permission: ec2:DescribeSecurityGroups" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "ec2:CreateSecurityGroup" "*"; then
      echo "Missing permission: ec2:CreateSecurityGroup" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "ec2:CreateTags" "*"; then
      echo "Missing permission: ec2:CreateTags" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "ec2:RunInstances" "*"; then
      echo "Missing permission: ec2:RunInstances" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "iam:GetRole" "${role_arn}"; then
      echo "Missing permission: iam:GetRole on ${role_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "iam:CreateRole" "${role_arn}"; then
      echo "Missing permission: iam:CreateRole on ${role_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "iam:PutRolePolicy" "${role_arn}"; then
      echo "Missing permission: iam:PutRolePolicy on ${role_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "iam:AttachRolePolicy" "${role_arn}"; then
      echo "Missing permission: iam:AttachRolePolicy on ${role_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "iam:GetInstanceProfile" "${profile_arn}"; then
      echo "Missing permission: iam:GetInstanceProfile on ${profile_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "iam:CreateInstanceProfile" "${profile_arn}"; then
      echo "Missing permission: iam:CreateInstanceProfile on ${profile_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if ! _simulate_allowed "iam:AddRoleToInstanceProfile" "${profile_arn}"; then
      echo "Missing permission: iam:AddRoleToInstanceProfile on ${profile_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if [[ "${CREATE_RESULTS_BUCKET}" == "1" ]] && ! _simulate_allowed "s3:CreateBucket" "${bucket_arn}"; then
      echo "Missing permission: s3:CreateBucket on ${bucket_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if [[ "${CREATE_RESULTS_BUCKET}" == "1" ]] && ! _simulate_allowed "s3:PutBucketVersioning" "${bucket_arn}"; then
      echo "Missing permission: s3:PutBucketVersioning on ${bucket_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if [[ "${CREATE_RESULTS_BUCKET}" == "1" ]] && ! _simulate_allowed "s3:PutBucketEncryption" "${bucket_arn}"; then
      echo "Missing permission: s3:PutBucketEncryption on ${bucket_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if [[ "${CREATE_RESULTS_BUCKET}" == "1" ]] && ! _simulate_allowed "s3:PutBucketPublicAccessBlock" "${bucket_arn}"; then
      echo "Missing permission: s3:PutBucketPublicAccessBlock on ${bucket_arn}" >&2
      deny_count=$((deny_count + 1))
    fi
    if [[ "${DEPLOY_SOURCE_MODE}" == "artifact" ]] && ! _simulate_allowed "s3:PutObject" "arn:aws:s3:::${RESULTS_BUCKET}/${ARTIFACT_PREFIX}/*"; then
      echo "Missing permission: s3:PutObject on arn:aws:s3:::${RESULTS_BUCKET}/${ARTIFACT_PREFIX}/*" >&2
      deny_count=$((deny_count + 1))
    fi
    if [[ "${EFFECTIVE_NULLONE_INSTALL_MODE}" == "s3" ]] && ! _simulate_allowed "s3:GetObject" "arn:aws:s3:::${NULLONE_S3_BUCKET}/${NULLONE_S3_KEY}"; then
      echo "Missing permission: s3:GetObject on arn:aws:s3:::${NULLONE_S3_BUCKET}/${NULLONE_S3_KEY}" >&2
      deny_count=$((deny_count + 1))
    fi
    if [[ "${WAIT_FOR_SSM}" == "1" ]] && ! _simulate_allowed "ssm:DescribeInstanceInformation" "*"; then
      echo "Missing permission: ssm:DescribeInstanceInformation" >&2
      deny_count=$((deny_count + 1))
    fi

    # AMI resolution supports either SSM parameter or EC2 image listing.
    if ! _simulate_allowed "ssm:GetParameter" "*" && ! _simulate_allowed "ec2:DescribeImages" "*"; then
      echo "Missing permission: need one of ssm:GetParameter or ec2:DescribeImages for AL2023 AMI resolution" >&2
      deny_count=$((deny_count + 1))
    fi
  fi

  if [[ "${deny_count}" -gt 0 ]]; then
    echo "" >&2
    echo "Preflight failed with ${deny_count} missing permission(s)." >&2
    echo "Fix IAM or run with ENABLE_PREFLIGHT=0 to bypass checks." >&2
    exit 1
  fi

  echo "Preflight permission check passed."
}

create_deploy_artifact() {
  if [[ "${DEPLOY_SOURCE_MODE}" != "artifact" ]]; then
    return
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    if [[ -z "${DEPLOY_ARTIFACT_S3_URI}" ]]; then
      DEPLOY_ARTIFACT_S3_URI="s3://${RESULTS_BUCKET}/${ARTIFACT_PREFIX}/${PROJECT_NAME}-dry-run.tar.gz"
    fi
    echo "DRY_RUN=1: skipping deploy artifact upload; using ${DEPLOY_ARTIFACT_S3_URI}"
    return
  fi

  require_command tar
  if [[ ! -f "scripts/deploy_orchestrator_host.sh" ]]; then
    echo "DEPLOY_SOURCE_MODE=artifact must be run from the repository root." >&2
    exit 1
  fi
  local stamp artifact_name artifact_path key
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  artifact_name="${PROJECT_NAME}-${stamp}.tar.gz"
  artifact_path="${TMP_DIR}/${artifact_name}"
  key="${ARTIFACT_PREFIX}/${artifact_name}"

  echo "Packaging local source artifact..."
  tar \
    --exclude=".git" \
    --exclude=".venv" \
    --exclude=".pytest_cache" \
    --exclude="__pycache__" \
    --exclude=".generated" \
    --exclude="results" \
    --exclude="runtime" \
    --exclude="recommendations" \
    -czf "${artifact_path}" .

  DEPLOY_ARTIFACT_S3_URI="s3://${RESULTS_BUCKET}/${key}"
  echo "Uploading deploy artifact to ${DEPLOY_ARTIFACT_S3_URI}"
  aws s3 cp "${artifact_path}" "${DEPLOY_ARTIFACT_S3_URI}" --region "${AWS_REGION}" >/dev/null
}

prepare_nullone_bootstrap() {
  local local_bin=""

  case "${NULLONE_INSTALL_MODE}" in
    auto)
      if [[ -n "${NULLONE_S3_URI}" ]]; then
        EFFECTIVE_NULLONE_INSTALL_MODE="s3"
      elif [[ -n "${NULLONE_DOWNLOAD_URL}" ]]; then
        EFFECTIVE_NULLONE_INSTALL_MODE="url"
      else
        if [[ -n "${NULLONE_BIN_LOCAL_PATH}" ]]; then
          local_bin="${NULLONE_BIN_LOCAL_PATH}"
        elif command -v nullone >/dev/null 2>&1; then
          local_bin="$(command -v nullone)"
        fi

        if [[ -n "${local_bin}" ]]; then
          local stamp key
          stamp="$(date -u +%Y%m%dT%H%M%SZ)"
          key="${ARTIFACT_PREFIX}/nullone/nullone-${stamp}"
          NULLONE_S3_URI="s3://${RESULTS_BUCKET}/${key}"
          if [[ "${DRY_RUN}" == "1" ]]; then
            echo "DRY_RUN=1: would upload local nullone binary from ${local_bin} to ${NULLONE_S3_URI}"
          else
            echo "Uploading local nullone binary from ${local_bin} to ${NULLONE_S3_URI}"
            aws s3 cp "${local_bin}" "${NULLONE_S3_URI}" --region "${AWS_REGION}" >/dev/null
          fi
          EFFECTIVE_NULLONE_INSTALL_MODE="s3"
        else
          EFFECTIVE_NULLONE_INSTALL_MODE="skip"
        fi
      fi
      ;;
    skip|s3|url)
      EFFECTIVE_NULLONE_INSTALL_MODE="${NULLONE_INSTALL_MODE}"
      ;;
    *)
      echo "Unsupported NULLONE_INSTALL_MODE=${NULLONE_INSTALL_MODE} (expected auto, skip, s3, or url)." >&2
      exit 1
      ;;
  esac

  if [[ "${INSTALL_SCHEDULERS}" == "1" && "${EFFECTIVE_NULLONE_INSTALL_MODE}" != "s3" ]]; then
    echo "INSTALL_SCHEDULERS=1 requires explicit S3 NullOne release usage." >&2
    echo "Set NULLONE_INSTALL_MODE=s3 and NULLONE_S3_URI=s3://${RESULTS_BUCKET}/${NULLONE_RELEASE_PREFIX}/<version>/linux-amd64/nullone" >&2
    exit 1
  fi

  if [[ "${EFFECTIVE_NULLONE_INSTALL_MODE}" == "s3" ]]; then
    validate_nullone_s3_uri_contract
  fi
}

print_dry_run_summary() {
  echo ""
  echo "DRY RUN: validated deployment inputs; no AWS resources were created or modified."
  echo "  Region:   ${AWS_REGION}"
  echo "  App dir:  ${APP_DIR}"
  echo "  Results:  s3://${RESULTS_BUCKET}"
  echo "  Source:   ${DEPLOY_SOURCE_MODE}"
  if [[ "${DEPLOY_SOURCE_MODE}" == "artifact" ]]; then
    echo "  Artifact: ${DEPLOY_ARTIFACT_S3_URI}"
  fi
  echo "  NullOne:  ${EFFECTIVE_NULLONE_INSTALL_MODE}"
  if [[ "${EFFECTIVE_NULLONE_INSTALL_MODE}" == "s3" ]]; then
    echo "  NullOne artifact: ${NULLONE_S3_URI}"
  fi
  echo ""
  echo "Next step: rerun with DRY_RUN=0 (default) to apply."
}

default_vpc_id() {
  aws ec2 describe-vpcs \
    --filters Name=is-default,Values=true \
    --query "Vpcs[0].VpcId" \
    --output text \
    --region "${AWS_REGION}"
}

default_subnet_id() {
  local vpc_id="$1"
  aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${vpc_id}" Name=default-for-az,Values=true \
    --query "Subnets[0].SubnetId" \
    --output text \
    --region "${AWS_REGION}"
}

ensure_security_group() {
  local vpc_id="$1"
  local sg_id
  sg_id="$(aws ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=${vpc_id}" "Name=group-name,Values=${SECURITY_GROUP_NAME}" \
    --query "SecurityGroups[0].GroupId" \
    --output text \
    --region "${AWS_REGION}")"

  if [[ "${sg_id}" == "None" || -z "${sg_id}" ]]; then
    echo "Creating security group: ${SECURITY_GROUP_NAME}" >&2
    sg_id="$(aws ec2 create-security-group \
      --group-name "${SECURITY_GROUP_NAME}" \
      --description "Source Latency Profiler orchestrator egress" \
      --vpc-id "${vpc_id}" \
      --query GroupId \
      --output text \
      --region "${AWS_REGION}")"
    aws ec2 create-tags \
      --resources "${sg_id}" \
      --tags "Key=Name,Value=${SECURITY_GROUP_NAME}" "Key=Project,Value=${PROJECT_NAME}" "Key=Component,Value=orchestrator" \
      --region "${AWS_REGION}"
  else
    echo "Using existing security group: ${sg_id}" >&2
  fi

  echo "${sg_id}"
}

latest_al2023_ami() {
  local ami_id
  if ami_id="$(aws ssm get-parameter \
    --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
    --query Parameter.Value \
    --output text \
    --region "${AWS_REGION}" 2>/dev/null)"; then
    echo "${ami_id}"
    return
  fi

  # Fallback when caller lacks ssm:GetParameter but has ec2:DescribeImages.
  ami_id="$(aws ec2 describe-images \
    --owners amazon \
    --filters \
      "Name=name,Values=al2023-ami-*-x86_64" \
      "Name=state,Values=available" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" \
    --output text \
    --region "${AWS_REGION}")"

  if [[ "${ami_id}" == "None" || -z "${ami_id}" ]]; then
    echo "Could not resolve an Amazon Linux 2023 AMI. Set AMI_ID explicitly." >&2
    exit 1
  fi

  echo "${ami_id}"
}

write_user_data() {
  local repo_url_json
  local deploy_source_mode_json
  local deploy_artifact_s3_uri_json
  local git_ref_json
  local app_dir_json
  local user_json
  local results_bucket_json
  local aws_region_json
  local install_schedulers_json
  local service_name_json
  local nullone_install_mode_json
  local nullone_s3_uri_json
  local nullone_download_url_json
  local nullone_bin_path_json
  local nullone_root_json
  local nullone_force_install_json

  repo_url_json="$(json_quote "${REPO_URL:-}")"
  deploy_source_mode_json="$(json_quote "${DEPLOY_SOURCE_MODE}")"
  deploy_artifact_s3_uri_json="$(json_quote "${DEPLOY_ARTIFACT_S3_URI}")"
  git_ref_json="$(json_quote "${GIT_REF}")"
  app_dir_json="$(json_quote "${APP_DIR}")"
  user_json="$(json_quote "${ORCHESTRATOR_USER}")"
  results_bucket_json="$(json_quote "${RESULTS_BUCKET}")"
  aws_region_json="$(json_quote "${AWS_REGION}")"
  install_schedulers_json="$(json_quote "${INSTALL_SCHEDULERS}")"
  service_name_json="$(json_quote "${SERVICE_NAME}")"
  nullone_install_mode_json="$(json_quote "${EFFECTIVE_NULLONE_INSTALL_MODE}")"
  nullone_s3_uri_json="$(json_quote "${NULLONE_S3_URI}")"
  nullone_download_url_json="$(json_quote "${NULLONE_DOWNLOAD_URL}")"
  nullone_bin_path_json="$(json_quote "${NULLONE_BIN_PATH}")"
  nullone_root_json="$(json_quote "${NULLONE_ROOT}")"
  nullone_force_install_json="$(json_quote "${NULLONE_FORCE_INSTALL}")"

  cat >"${USER_DATA}" <<EOF
#!/bin/bash
set -euxo pipefail

exec > >(tee -a /var/log/source-latency-profiler-bootstrap.log | logger -t source-latency-profiler-bootstrap -s 2>/dev/console) 2>&1

REPO_URL=${repo_url_json}
DEPLOY_SOURCE_MODE=${deploy_source_mode_json}
DEPLOY_ARTIFACT_S3_URI=${deploy_artifact_s3_uri_json}
GIT_REF=${git_ref_json}
APP_DIR=${app_dir_json}
ORCHESTRATOR_USER=${user_json}
RESULTS_BUCKET=${results_bucket_json}
AWS_REGION=${aws_region_json}
INSTALL_SCHEDULERS=${install_schedulers_json}
SERVICE_NAME=${service_name_json}
NULLONE_INSTALL_MODE=${nullone_install_mode_json}
NULLONE_S3_URI=${nullone_s3_uri_json}
NULLONE_DOWNLOAD_URL=${nullone_download_url_json}
NULLONE_BIN=${nullone_bin_path_json}
NULLONE_ROOT=${nullone_root_json}
NULLONE_FORCE_INSTALL=${nullone_force_install_json}

if command -v dnf >/dev/null 2>&1; then
  dnf install -y git awscli tar gzip
elif command -v yum >/dev/null 2>&1; then
  yum install -y git awscli tar gzip
fi

mkdir -p "\${APP_DIR}"
find "\${APP_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

if [[ "\${DEPLOY_SOURCE_MODE}" == "artifact" ]]; then
  if [[ -z "\${DEPLOY_ARTIFACT_S3_URI}" ]]; then
    echo "DEPLOY_ARTIFACT_S3_URI is required when DEPLOY_SOURCE_MODE=artifact" >&2
    exit 1
  fi
  aws s3 cp "\${DEPLOY_ARTIFACT_S3_URI}" /tmp/source-latency-profiler.tar.gz --region "\${AWS_REGION}"
  tar -xzf /tmp/source-latency-profiler.tar.gz -C "\${APP_DIR}"
else
  if [[ -z "\${REPO_URL}" ]]; then
    echo "REPO_URL is required when DEPLOY_SOURCE_MODE=git" >&2
    exit 1
  fi
  git clone --branch "\${GIT_REF}" "\${REPO_URL}" "\${APP_DIR}"
fi

cd "\${APP_DIR}"
APP_DIR="\${APP_DIR}" \\
ORCHESTRATOR_USER="\${ORCHESTRATOR_USER}" \\
RESULTS_BUCKET="\${RESULTS_BUCKET}" \\
AWS_REGION="\${AWS_REGION}" \\
INSTALL_SCHEDULERS="\${INSTALL_SCHEDULERS}" \\
SERVICE_NAME="\${SERVICE_NAME}" \\
NULLONE_INSTALL_MODE="\${NULLONE_INSTALL_MODE}" \\
NULLONE_S3_URI="\${NULLONE_S3_URI}" \\
NULLONE_DOWNLOAD_URL="\${NULLONE_DOWNLOAD_URL}" \\
NULLONE_BIN="\${NULLONE_BIN}" \\
NULLONE_ROOT="\${NULLONE_ROOT}" \\
NULLONE_FORCE_INSTALL="\${NULLONE_FORCE_INSTALL}" \\
REPO_URL="\${REPO_URL}" \\
DEPLOY_SOURCE_MODE="\${DEPLOY_SOURCE_MODE}" \\
GIT_REF="\${GIT_REF}" \\
bash scripts/deploy_orchestrator_host.sh
EOF
}

launch_instance() {
  local vpc_id="${VPC_ID:-}"
  local subnet_id="${SUBNET_ID:-}"
  local security_group_id="${SECURITY_GROUP_ID:-}"
  local ami_id="${AMI_ID:-}"

  if [[ -z "${vpc_id}" ]]; then
    vpc_id="$(default_vpc_id)"
  fi
  if [[ -z "${subnet_id}" ]]; then
    subnet_id="$(default_subnet_id "${vpc_id}")"
  fi
  if [[ -z "${security_group_id}" ]]; then
    security_group_id="$(ensure_security_group "${vpc_id}")"
  fi
  if [[ -z "${ami_id}" ]]; then
    ami_id="$(latest_al2023_ami)"
  fi

  write_user_data

  echo "Launching orchestrator EC2 instance..."
  local instance_id
  instance_id="$(aws ec2 run-instances \
    --image-id "${ami_id}" \
    --instance-type "${INSTANCE_TYPE}" \
    --subnet-id "${subnet_id}" \
    --security-group-ids "${security_group_id}" \
    --iam-instance-profile "Name=${INSTANCE_PROFILE_NAME}" \
    --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=${VOLUME_SIZE_GB},VolumeType=gp3,DeleteOnTermination=true,Encrypted=true}" \
    --tag-specifications \
      "ResourceType=instance,Tags=[{Key=Name,Value=${INSTANCE_NAME}},{Key=Project,Value=${PROJECT_NAME}},{Key=Component,Value=orchestrator}]" \
      "ResourceType=volume,Tags=[{Key=Name,Value=${INSTANCE_NAME}},{Key=Project,Value=${PROJECT_NAME}},{Key=Component,Value=orchestrator}]" \
    --user-data "file://${USER_DATA}" \
    --query "Instances[0].InstanceId" \
    --output text \
    --region "${AWS_REGION}")"

  echo "Instance ID: ${instance_id}"
  aws ec2 wait instance-running --instance-ids "${instance_id}" --region "${AWS_REGION}"

  if [[ "${WAIT_FOR_SSM}" == "1" ]]; then
    echo "Waiting for SSM registration..."
    for _ in $(seq 1 60); do
      if aws ssm describe-instance-information \
        --filters "Key=InstanceIds,Values=${instance_id}" \
        --query "InstanceInformationList[0].PingStatus" \
        --output text \
        --region "${AWS_REGION}" | grep -q Online; then
        break
      fi
      sleep 10
    done
  fi

  echo ""
  echo "Orchestrator instance launched."
  echo "  Instance: ${instance_id}"
  echo "  Region:   ${AWS_REGION}"
  echo "  App dir:  ${APP_DIR}"
  echo "  Results:  s3://${RESULTS_BUCKET}"
  echo "  Source:   ${DEPLOY_SOURCE_MODE}"
  echo "  NullOne:  ${EFFECTIVE_NULLONE_INSTALL_MODE}"
  if [[ "${DEPLOY_SOURCE_MODE}" == "artifact" ]]; then
    echo "  Artifact: ${DEPLOY_ARTIFACT_S3_URI}"
  fi
  if [[ "${EFFECTIVE_NULLONE_INSTALL_MODE}" == "s3" && -n "${NULLONE_S3_URI}" ]]; then
    echo "  NullOne artifact: ${NULLONE_S3_URI}"
  fi
  echo ""
  echo "Check bootstrap logs with:"
  echo "  aws ssm start-session --target ${instance_id} --region ${AWS_REGION}"
  echo "  sudo tail -f /var/log/source-latency-profiler-bootstrap.log"
}

ensure_results_bucket
prepare_nullone_bootstrap
preflight_permissions
create_deploy_artifact
if [[ "${DRY_RUN}" == "1" ]]; then
  print_dry_run_summary
  exit 0
fi
ensure_iam

# Give IAM instance profile propagation a short head start before EC2 launch.
sleep 10
launch_instance
