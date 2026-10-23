from profiler.tfvars import build_strategy_runtime_tfvars


def test_build_strategy_runtime_tfvars() -> None:
    tfvars = build_strategy_runtime_tfvars(
        {
            "strategy": "nullone_wngsr_kalshi",
            "run_id": "run-123",
            "recommended_infrastructure": {
                "provider": "aws",
                "region": "us-east-1",
                "runtime": "ec2",
                "instance_type": "c7i.large",
                "network": {"public_ip": False},
                "artifact": {
                    "type": "container",
                    "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:run-123",
                },
                "startup_command": "nullone ready --config /etc/nullone/runtime.json",
                "execution_command": "nullone execute --config /etc/nullone/runtime.json",
            },
        },
        runtime_config_s3_uri="s3://bucket/runtime/run-123.json",
        secret_arns=["arn:aws:secretsmanager:us-east-1:123456789012:secret:nullone/prod/runtime/env-abcd"],
        secret_names=["nullone/prod/runtime/env", "nullone/prod/kalshi/private_key_pem"],
    )

    assert tfvars["aws_region"] == "us-east-1"
    assert tfvars["create_results_bucket"] is False
    assert tfvars["results_bucket_name"] == "bucket"
    assert tfvars["runner_count"] == 0
    assert tfvars["strategy_runtime_enabled"] is True
    assert tfvars["strategy_name"] == "nullone_wngsr_kalshi"
    assert tfvars["strategy_runtime_config_bucket"] == "bucket"
    assert tfvars["strategy_runtime_config_key"] == "runtime/run-123.json"
    assert tfvars["strategy_artifact_type"] == "container"
    assert tfvars["strategy_artifact_image"] == "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:run-123"
    assert tfvars["strategy_secret_arns"] == [
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:nullone/prod/runtime/env-abcd"
    ]
    assert tfvars["strategy_runtime_env_secret_id"] == "nullone/prod/runtime/env"
    assert tfvars["strategy_private_key_secret_id"] == "nullone/prod/kalshi/private_key_pem"
    assert tfvars["strategy_runtime_expires_at"] == ""
    assert tfvars["strategy_create_private_endpoints"] is True
