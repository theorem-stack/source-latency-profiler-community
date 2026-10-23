from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ORCHESTRATOR_REQUIRED_ACTIONS = [
    "ec2:DescribeInstances",
    "ec2:DescribeImages",
    "ec2:DescribeSecurityGroups",
    "ec2:DescribeSubnets",
    "ec2:DescribeVpcs",
    "ec2:DescribeVpcAttribute",
    "ec2:DescribeRouteTables",
    "ec2:RunInstances",
    "ec2:TerminateInstances",
    "ec2:CreateSecurityGroup",
    "ec2:DeleteSecurityGroup",
    "ec2:AuthorizeSecurityGroupEgress",
    "ec2:RevokeSecurityGroupEgress",
    "ec2:CreateTags",
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
    "iam:DetachRolePolicy",
    "s3:GetObject",
    "s3:PutObject",
    "s3:GetBucketAcl",
    "s3:GetBucketPolicy",
    "s3:GetBucketLocation",
    "s3:GetBucketVersioning",
    "s3:GetEncryptionConfiguration",
    "s3:GetBucketPublicAccessBlock",
    "s3:ListBucket",
    "ecr:GetAuthorizationToken",
    "ecr:DescribeImages",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ecr:BatchCheckLayerAvailability",
    "secretsmanager:GetSecretValue",
    "secretsmanager:ListSecrets",
    "ssm:SendCommand",
    "ssm:GetCommandInvocation",
    "ssm:ListCommandInvocations",
    "ssm:DescribeInstanceInformation",
    "ssm:CancelCommand",
]


@dataclass(frozen=True)
class PreflightRequest:
    account_id: str
    region: str
    results_bucket: str
    deploy_bucket: str | None = None
    project_name: str = "source-latency-profiler"
    sample_run_id: str = "preflight"


def build_orchestrator_policy(request: PreflightRequest) -> dict[str, Any]:
    runtime_name = f"{request.project_name}-strategy-*"
    bucket_resources = [
        f"arn:aws:s3:::{request.results_bucket}/profiler/*",
        f"arn:aws:s3:::{request.results_bucket}/runtime/*",
        f"arn:aws:s3:::{request.results_bucket}/diagnostics/*",
    ]
    if request.deploy_bucket:
        bucket_resources.append(f"arn:aws:s3:::{request.deploy_bucket}/{request.project_name}/deployments/*")

    return {
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
                    "ec2:CreateTags",
                ],
                "Resource": "*",
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
                    "iam:DetachRolePolicy",
                ],
                "Resource": [
                    f"arn:aws:iam::{request.account_id}:role/{runtime_name}",
                    f"arn:aws:iam::{request.account_id}:instance-profile/{runtime_name}",
                    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
                ],
            },
            {
                "Sid": "RuntimeArtifactsAndDiagnostics",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": bucket_resources,
            },
            {
                "Sid": "RuntimeBucketRead",
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    "s3:GetBucket*",
                ],
                "Resource": f"arn:aws:s3:::{request.results_bucket}",
            },
            {
                "Sid": "EcrRead",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:DescribeImages",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": "*",
            },
            {
                "Sid": "RuntimeSecretsRead",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue", "secretsmanager:ListSecrets"],
                "Resource": f"arn:aws:secretsmanager:{request.region}:{request.account_id}:secret:nullone/*",
            },
            {
                "Sid": "RuntimeSsmControl",
                "Effect": "Allow",
                "Action": [
                    "ssm:SendCommand",
                    "ssm:GetCommandInvocation",
                    "ssm:ListCommandInvocations",
                    "ssm:DescribeInstanceInformation",
                    "ssm:CancelCommand",
                ],
                "Resource": "*",
            },
        ],
    }


def run_preflight(request: PreflightRequest) -> dict[str, Any]:
    sts = _boto3_client("sts", request.region)
    identity = sts.get_caller_identity()
    policy = build_orchestrator_policy(request)
    result = {
        "identity": identity,
        "required_actions": ORCHESTRATOR_REQUIRED_ACTIONS,
        "policy": policy,
        "simulation": None,
        "missing_actions": [],
    }

    principal_arn = _simulation_principal_arn(identity["Arn"])
    if principal_arn is None:
        result["simulation"] = {
            "status": "skipped",
            "reason": "caller is not an IAM role/user ARN that can be simulated",
        }
        return result

    try:
        iam = _boto3_client("iam", request.region)
        response = iam.simulate_principal_policy(
            PolicySourceArn=principal_arn,
            ActionNames=ORCHESTRATOR_REQUIRED_ACTIONS,
            ResourceArns=["*"],
        )
    except Exception as exc:  # noqa: BLE001 - report preflight failures as data.
        result["simulation"] = {"status": "failed", "error": str(exc)}
        return result

    missing = [
        evaluation["EvalActionName"]
        for evaluation in response.get("EvaluationResults", [])
        if evaluation.get("EvalDecision") != "allowed"
    ]
    result["simulation"] = {"status": "ok", "principal_arn": principal_arn}
    result["missing_actions"] = missing
    return result


def _simulation_principal_arn(caller_arn: str) -> str | None:
    if ":assumed-role/" in caller_arn:
        prefix, suffix = caller_arn.split(":assumed-role/", 1)
        role_name = suffix.split("/", 1)[0]
        return f"{prefix}:role/{role_name}"
    if ":role/" in caller_arn or ":user/" in caller_arn:
        return caller_arn
    return None


def _boto3_client(service: str, region: str):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to enable AWS preflight checks") from exc
    return boto3.client(service, region_name=region)
