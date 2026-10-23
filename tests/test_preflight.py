from profiler.preflight import PreflightRequest, build_orchestrator_policy


def test_build_orchestrator_policy_includes_discovered_permissions() -> None:
    policy = build_orchestrator_policy(
        PreflightRequest(
            account_id="123456789012",
            region="us-east-1",
            results_bucket="latency-results",
            deploy_bucket="deploy-artifacts",
        )
    )

    actions = {action for statement in policy["Statement"] for action in statement["Action"]}

    assert "iam:ListInstanceProfilesForRole" in actions
    assert "ssm:CancelCommand" in actions
    assert "ssm:ListCommandInvocations" in actions
    assert "s3:PutObject" in actions
    assert "ec2:TerminateInstances" in actions
