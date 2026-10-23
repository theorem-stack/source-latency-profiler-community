import json
import zipfile
from pathlib import Path

import pytest

from profiler import orchestrator
from profiler.orchestrator import ExecuteOptions, execute_strategy
from profiler.result import ProbeResult
from profiler.strategy import load_strategy_profile

PINNED_STRATEGY = "strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml"


def test_execute_plan_only_runs_expected_command_order_and_overrides_public_ip(monkeypatch, tmp_path: Path) -> None:
    tofu_calls: list[list[str]] = []
    _patch_common_orchestration(monkeypatch, tmp_path, tofu_calls=tofu_calls)

    result = execute_strategy(
        ExecuteOptions(
            strategy_path=PINNED_STRATEGY,
            run_id="run-123",
            results_s3_prefix="s3://bucket/profiler",
            runtime_s3_prefix="s3://bucket/runtime",
            secrets_prefix="nullone/prod",
            work_dir=str(tmp_path / "runs" / "run-123"),
            tofu_dir=str(tmp_path / "infra"),
            plan_only=True,
            require_public_ip=True,
            samples=1,
            interval_ms=1,
        )
    )

    assert tofu_calls == [["init"], ["plan", f"-var-file={result.tfvars_path.resolve()}"]]
    tfvars = json.loads(result.tfvars_path.read_text(encoding="utf-8"))
    assert tfvars["strategy_require_public_ip"] is True
    assert tfvars["strategy_create_private_endpoints"] is False
    assert result.runtime_config_s3_uri == "s3://bucket/runtime/runs/run-123/combined/run-123.json"


def test_execute_apply_waits_for_runtime_health(monkeypatch, tmp_path: Path) -> None:
    tofu_calls: list[list[str]] = []
    _patch_common_orchestration(monkeypatch, tmp_path, tofu_calls=tofu_calls)
    monkeypatch.setattr(orchestrator, "run_tofu_capture", lambda args, *, cwd: "i-1234567890abcdef0\n")
    monkeypatch.setattr(
        orchestrator,
        "wait_for_runtime_ready",
        lambda instance_id, *, region, timeout_seconds: {"status": "ok", "output": '"ready": true'},
    )

    result = execute_strategy(
        ExecuteOptions(
            strategy_path=PINNED_STRATEGY,
            run_id="run-apply",
            results_s3_prefix="s3://bucket/profiler",
            runtime_s3_prefix="s3://bucket/runtime",
            work_dir=str(tmp_path / "runs" / "run-apply"),
            tofu_dir=str(tmp_path / "infra"),
            apply=True,
            samples=1,
            interval_ms=1,
        )
    )

    assert tofu_calls == [
        ["init"],
        ["apply", "-auto-approve", f"-var-file={result.tfvars_path.resolve()}"],
    ]
    assert result.instance_id == "i-1234567890abcdef0"
    assert result.teardown_performed is False


def test_execute_apply_tears_down_after_ready(monkeypatch, tmp_path: Path) -> None:
    teardown_calls: list[str] = []
    _patch_apply_orchestration(monkeypatch, tmp_path)
    monkeypatch.setattr(
        orchestrator,
        "teardown_strategy_runtime",
        lambda run_id, *, app_dir, yes: teardown_calls.append(run_id),
    )

    result = execute_strategy(
        ExecuteOptions(
            strategy_path=PINNED_STRATEGY,
            run_id="run-after-ready",
            results_s3_prefix="s3://bucket/profiler",
            runtime_s3_prefix="s3://bucket/runtime",
            work_dir=str(tmp_path / "runs" / "run-after-ready"),
            tofu_dir=str(tmp_path / "infra"),
            apply=True,
            teardown_mode="after-ready",
            samples=1,
            interval_ms=1,
        )
    )

    assert teardown_calls == ["run-after-ready"]
    assert result.teardown_performed is True


def test_execute_apply_tears_down_after_execution_signal(monkeypatch, tmp_path: Path) -> None:
    teardown_calls: list[str] = []
    execution_calls: list[str] = []
    _patch_apply_orchestration(monkeypatch, tmp_path)
    monkeypatch.setattr(
        orchestrator,
        "run_runtime_execution",
        lambda instance_id, *, region, timeout_seconds, diagnostic=False: execution_calls.append(instance_id)
        or {"status": "ok", "output": "execution complete"},
    )
    monkeypatch.setattr(
        orchestrator,
        "teardown_strategy_runtime",
        lambda run_id, *, app_dir, yes: teardown_calls.append(run_id),
    )

    result = execute_strategy(
        ExecuteOptions(
            strategy_path=PINNED_STRATEGY,
            run_id="run-after-execution",
            results_s3_prefix="s3://bucket/profiler",
            runtime_s3_prefix="s3://bucket/runtime",
            work_dir=str(tmp_path / "runs" / "run-after-execution"),
            tofu_dir=str(tmp_path / "infra"),
            apply=True,
            teardown_mode="after-execution",
            samples=1,
            interval_ms=1,
        )
    )

    assert execution_calls == ["i-1234567890abcdef0"]
    assert teardown_calls == ["run-after-execution"]
    assert result.execution_status == {
        "status": "ok",
        "output": "execution complete",
        "handoff": "ssm-immediate",
    }
    assert result.teardown_performed is True


def test_execute_apply_collects_hot_path_diagnostics(monkeypatch, tmp_path: Path) -> None:
    uploaded: list[tuple[Path, str]] = []
    _patch_apply_orchestration(monkeypatch, tmp_path)
    monkeypatch.setattr(
        orchestrator,
        "run_runtime_execution",
        lambda instance_id, *, region, timeout_seconds, diagnostic=False: {
            "status": "ok",
            "command_id": "exec-1",
            "output": '{"event":"source_fetch_done","duration_ms":12.4}\nplain log\n',
            "error": "",
            "diagnostic": diagnostic,
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "run_runtime_diagnostics",
        lambda instance_id, *, region: {"status": "ok", "command_id": "diag-1", "output": "docker ps", "error": ""},
    )
    monkeypatch.setattr(orchestrator, "upload_file_to_s3", lambda local_path, destination: uploaded.append((Path(local_path), destination)))

    result = execute_strategy(
        ExecuteOptions(
            strategy_path=PINNED_STRATEGY,
            run_id="run-diag",
            results_s3_prefix="s3://bucket/profiler",
            runtime_s3_prefix="s3://bucket/runtime",
            work_dir=str(tmp_path / "runs" / "run-diag"),
            tofu_dir=str(tmp_path / "infra"),
            apply=True,
            diagnostics_mode="hot-path",
            diagnostics_s3_prefix="s3://bucket/diagnostics",
            samples=1,
            interval_ms=1,
        )
    )

    assert result.execution_status is not None
    assert result.execution_status["diagnostic"] == "hot-path"
    assert result.diagnostics_path is not None
    assert result.diagnostics_path.exists()
    assert result.diagnostics_s3_uri == "s3://bucket/diagnostics/runs/run-diag/combined/run-diag-hot-path-diagnostics.zip"
    assert uploaded[-1][1] == result.diagnostics_s3_uri
    trace = result.diagnostics_path.parent / "execution-trace.jsonl"
    assert json.loads(trace.read_text(encoding="utf-8")) == {"duration_ms": 12.4, "event": "source_fetch_done"}
    with zipfile.ZipFile(result.diagnostics_path) as bundle:
        assert sorted(bundle.namelist()) == [
            "execution-stderr.txt",
            "execution-stdout.txt",
            "execution-trace.jsonl",
            "latency-summary.json",
            "runtime-diagnostics.json",
        ]
    summary = json.loads((result.diagnostics_path.parent / "latency-summary.json").read_text(encoding="utf-8"))
    assert summary["event_count"] == 1
    assert summary["status"] == "passed"


def test_execute_apply_tears_down_after_ttl(monkeypatch, tmp_path: Path) -> None:
    sleep_calls: list[int] = []
    teardown_calls: list[str] = []
    _patch_apply_orchestration(monkeypatch, tmp_path)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(
        orchestrator,
        "teardown_strategy_runtime",
        lambda run_id, *, app_dir, yes: teardown_calls.append(run_id),
    )

    result = execute_strategy(
        ExecuteOptions(
            strategy_path=PINNED_STRATEGY,
            run_id="run-ttl",
            results_s3_prefix="s3://bucket/profiler",
            runtime_s3_prefix="s3://bucket/runtime",
            work_dir=str(tmp_path / "runs" / "run-ttl"),
            tofu_dir=str(tmp_path / "infra"),
            apply=True,
            teardown_mode="ttl",
            teardown_after_seconds=30,
            samples=1,
            interval_ms=1,
        )
    )

    assert sleep_calls == [30]
    assert teardown_calls == ["run-ttl"]
    assert result.teardown_performed is True


def test_execute_apply_refuses_non_deployable_policy(monkeypatch, tmp_path: Path) -> None:
    _patch_common_orchestration(monkeypatch, tmp_path, policy_decision="hold")

    with pytest.raises(RuntimeError, match="refusing to apply"):
        execute_strategy(
            ExecuteOptions(
                strategy_path=PINNED_STRATEGY,
                run_id="run-hold",
                results_s3_prefix="s3://bucket/profiler",
                runtime_s3_prefix="s3://bucket/runtime",
                work_dir=str(tmp_path / "runs" / "run-hold"),
                tofu_dir=str(tmp_path / "infra"),
                apply=True,
                samples=1,
                interval_ms=1,
            )
        )


def test_ttl_teardown_requires_delay(monkeypatch, tmp_path: Path) -> None:
    _patch_apply_orchestration(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="teardown-after-seconds"):
        execute_strategy(
            ExecuteOptions(
                strategy_path=PINNED_STRATEGY,
                run_id="run-bad-ttl",
                results_s3_prefix="s3://bucket/profiler",
                runtime_s3_prefix="s3://bucket/runtime",
                work_dir=str(tmp_path / "runs" / "run-bad-ttl"),
                tofu_dir=str(tmp_path / "infra"),
                apply=True,
                teardown_mode="ttl",
                samples=1,
                interval_ms=1,
            )
        )


def test_wait_for_command_retries_before_invocation_exists(monkeypatch) -> None:
    sleep_calls: list[int] = []
    client = _EventuallyConsistentCommandClient()
    monkeypatch.setattr(orchestrator.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    invocation = orchestrator._wait_for_command(client, "command-123", "i-123", timeout_seconds=30)

    assert invocation["Status"] == "Success"
    assert sleep_calls == [2]
    assert client.calls == 2


def test_wait_for_runtime_ready_retries_while_bootstrap_finishes(monkeypatch) -> None:
    sleep_calls: list[int] = []
    invocations = [
        {
            "Status": "Success",
            "StandardOutputContent": "inactive\n__NULLONE_HEALTH_FAILED__\n",
            "StandardErrorContent": "/usr/local/bin/nullone-health: No such file or directory\n",
        },
        {
            "Status": "Success",
            "StandardOutputContent": "active\n__NULLONE_READY_OK__\n",
            "StandardErrorContent": "",
        },
    ]
    client = _RuntimeReadyClient()
    monkeypatch.setattr(orchestrator, "_boto3_client", lambda service, region: client)
    monkeypatch.setattr(orchestrator, "_wait_for_command", lambda client, command_id, instance_id, *, timeout_seconds: invocations.pop(0))
    monkeypatch.setattr(orchestrator.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    status = orchestrator.wait_for_runtime_ready("i-123", region="us-east-1", timeout_seconds=60)

    assert status["status"] == "ok"
    assert client.command_count == 2
    assert sleep_calls == [10]


def test_run_runtime_execution_returns_timeout_artifacts(monkeypatch) -> None:
    client = _RuntimeExecutionClient()
    monkeypatch.setattr(orchestrator, "_boto3_client", lambda service, region: client)
    monkeypatch.setattr(
        orchestrator,
        "_wait_for_command",
        lambda client, command_id, instance_id, *, timeout_seconds: {
            "Status": "Success",
            "StandardOutputContent": "\n".join(
                [
                    "__NULLONE_EXECUTION_EXIT_CODE__=124",
                    "__NULLONE_EXECUTION_START__=2026-05-28T06:39:55Z",
                    "__NULLONE_EXECUTION_END__=2026-05-28T06:41:55Z",
                    "__NULLONE_EXECUTION_STDOUT_BEGIN__",
                    '{"event":"execute_start"}',
                    "__NULLONE_EXECUTION_STDOUT_END__",
                    "__NULLONE_EXECUTION_STDERR_BEGIN__",
                    "timed out",
                    "__NULLONE_EXECUTION_STDERR_END__",
                ]
            ),
            "StandardErrorContent": "",
        },
    )

    status = orchestrator.run_runtime_execution("i-123", region="us-east-1", timeout_seconds=120)

    assert client.commands[2].startswith("timeout 135s /usr/local/bin/nullone-execute")
    assert status["status"] == "failed"
    assert status["exit_code"] == 124
    assert status["output"] == '{"event":"execute_start"}\n'
    assert status["error"] == "timed out\n"


def test_run_runtime_execution_uses_nullone_diagnostic_command(monkeypatch) -> None:
    client = _RuntimeExecutionClient()
    monkeypatch.setattr(orchestrator, "_boto3_client", lambda service, region: client)
    monkeypatch.setattr(
        orchestrator,
        "_wait_for_command",
        lambda client, command_id, instance_id, *, timeout_seconds: {
            "Status": "Success",
            "StandardOutputContent": "\n".join(
                [
                    "__NULLONE_EXECUTION_EXIT_CODE__=0",
                    "__NULLONE_EXECUTION_STDOUT_BEGIN__",
                    '{"event":"execute_done","duration_ms":88.993}',
                    "__NULLONE_EXECUTION_STDOUT_END__",
                    "__NULLONE_EXECUTION_STDERR_BEGIN__",
                    "__NULLONE_EXECUTION_STDERR_END__",
                ]
            ),
            "StandardErrorContent": "",
        },
    )

    status = orchestrator.run_runtime_execution("i-123", region="us-east-1", timeout_seconds=120, diagnostic=True)

    command = client.commands[2]
    assert "docker exec nullone-runtime nullone execute --config /etc/nullone/runtime.json" in command
    assert "--diagnostic --simulate packaged --timeout-seconds 120 --trace-jsonl" in command
    assert status["status"] == "ok"
    assert status["exit_code"] == 0
    assert status["output"] == '{"event":"execute_done","duration_ms":88.993}\n'


def test_run_runtime_execution_uses_live_paper_command(monkeypatch) -> None:
    client = _RuntimeExecutionClient()
    monkeypatch.setattr(orchestrator, "_boto3_client", lambda service, region: client)
    monkeypatch.setattr(
        orchestrator,
        "_wait_for_command",
        lambda client, command_id, instance_id, *, timeout_seconds: {
            "Status": "Success",
            "StandardOutputContent": "\n".join(
                [
                    "__NULLONE_EXECUTION_EXIT_CODE__=0",
                    "__NULLONE_EXECUTION_STDOUT_BEGIN__",
                    '{"event":"release_wait_done"}',
                    "__NULLONE_EXECUTION_STDOUT_END__",
                    "__NULLONE_EXECUTION_STDERR_BEGIN__",
                    "__NULLONE_EXECUTION_STDERR_END__",
                ]
            ),
            "StandardErrorContent": "",
        },
    )

    status = orchestrator.run_runtime_execution("i-123", region="us-east-1", timeout_seconds=600, diagnostic="live-paper")

    command = client.commands[2]
    assert "--paper-live-release --timeout-seconds 600 --trace-jsonl" in command
    assert "--diagnostic --simulate packaged" not in command
    assert status["status"] == "ok"


def _patch_apply_orchestration(monkeypatch, tmp_path: Path) -> None:
    tofu_calls: list[list[str]] = []
    _patch_common_orchestration(monkeypatch, tmp_path, tofu_calls=tofu_calls)
    monkeypatch.setattr(orchestrator, "run_tofu_capture", lambda args, *, cwd: "i-1234567890abcdef0\n")
    monkeypatch.setattr(
        orchestrator,
        "wait_for_runtime_ready",
        lambda instance_id, *, region, timeout_seconds: {"status": "ok", "output": '"ready": true'},
    )


def _patch_common_orchestration(
    monkeypatch,
    tmp_path: Path,
    *,
    tofu_calls: list[list[str]] | None = None,
    policy_decision: str = "deploy",
) -> None:
    (tmp_path / "infra").mkdir()
    monkeypatch.setattr(
        orchestrator,
        "run_probes",
        lambda targets, runners, *, samples, interval_ms: [
            ProbeResult.empty(
                target="eia_wngsr_json",
                target_type="http",
                provider="aws",
                region="us-east-1",
                runner_id="aws-us-east-1",
            )
        ],
    )
    monkeypatch.setattr(
        orchestrator,
        "build_recommendation",
        lambda results, profile: {
            "recommended_location": {"provider": "aws", "region": "us-east-1"},
            "confidence": 0.99,
            "sample_window": {"minimum_samples_met": True},
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "artifact_with_resolved_image",
        lambda artifact: {
            "type": "container",
            "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:nullone-runtime-2026-05-25",
        },
    )
    monkeypatch.setattr(orchestrator, "build_infrastructure_recommendation", _infra_factory(policy_decision))
    monkeypatch.setattr(orchestrator, "upload_file_to_s3", lambda local_path, destination: None)
    monkeypatch.setattr(orchestrator, "secret_names_for_prefix", lambda prefix: ["nullone/prod/runtime/env"])
    monkeypatch.setattr(
        orchestrator,
        "secret_arns_for_names",
        lambda names, *, region: ["arn:aws:secretsmanager:us-east-1:123456789012:secret:nullone/prod/runtime/env-abcd"],
    )

    if tofu_calls is not None:
        monkeypatch.setattr(orchestrator, "run_tofu", lambda args, *, cwd: tofu_calls.append(args))

    monkeypatch.setattr(
        orchestrator,
        "resolve_strategy_profile",
        lambda path, **kwargs: load_strategy_profile(PINNED_STRATEGY),
    )
    monkeypatch.setattr(orchestrator, "_kalshi_env_for_execute", lambda options: {})


def _infra_factory(policy_decision: str):
    def factory(strategy, recommendation, *, run_id, now=None, artifact_override=None):
        return {
            "schema_version": 1,
            "strategy": strategy.name,
            "run_id": run_id,
            "event_time": strategy.event_time,
            "recommended_infrastructure": {
                "provider": "aws",
                "region": "us-east-1",
                "runtime": "ec2",
                "instance_type": "c7i.large",
                "network": {"public_ip": False, "placement": "single_az_low_latency"},
                "artifact": artifact_override,
                "startup_command": "nullone ready --config /etc/nullone/runtime.json",
                "execution_command": "nullone execute --config /etc/nullone/runtime.json",
            },
            "confidence": 0.99,
            "policy_decision": {"decision": policy_decision, "reasons": []},
            "fallback": {},
            "generated_at": "2026-05-25T00:00:00Z",
        }

    return factory


class _EventuallyConsistentCommandClient:
    calls = 0

    def get_command_invocation(self, *, CommandId, InstanceId):
        self.calls += 1
        if self.calls == 1:
            raise _InvocationDoesNotExistError()
        return {"Status": "Success", "StandardOutputContent": "__NULLONE_READY_OK__", "StandardErrorContent": ""}


class _InvocationDoesNotExistError(Exception):
    response = {"Error": {"Code": "InvocationDoesNotExist"}}


class _RuntimeReadyClient:
    command_count = 0

    def describe_instance_information(self, *, Filters):
        return {"InstanceInformationList": [{"InstanceId": "i-123"}]}

    def send_command(self, *, InstanceIds, DocumentName, Parameters):
        self.command_count += 1
        return {"Command": {"CommandId": f"command-{self.command_count}"}}


class _RuntimeExecutionClient:
    def __init__(self):
        self.commands = []

    def send_command(self, *, InstanceIds, DocumentName, Parameters):
        self.commands = Parameters["commands"]
        return {"Command": {"CommandId": "exec-command"}}
