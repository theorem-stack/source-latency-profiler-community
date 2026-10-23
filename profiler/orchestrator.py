from __future__ import annotations

import json
import os
import subprocess
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from profiler.artifacts import artifact_with_resolved_image
from profiler.config import Runner
from profiler.http_probe import probe_http
from profiler.infra_profile import build_infrastructure_recommendation, write_infrastructure_recommendation
from profiler.latency import write_latency_summary
from profiler.live_guardrails import build_live_policy_report, enforce_live_policy, write_reconciliation_report
from profiler.recommend import build_recommendation, write_recommendation
from profiler.result import ProbeResult, write_jsonl
from profiler.runtime_config import build_nullone_runtime_config, write_runtime_config
from profiler.secrets import load_kalshi_env_from_prefix, secret_arns_for_names, secret_names_for_prefix
from profiler.storage import build_s3_run_uri, upload_file_to_s3
from profiler.execution_handoff import (
    execute_at_from_runtime_config,
    normalize_execution_handoff,
    poll_host_execution_result,
    start_host_scheduled_execution,
    wait_until_lead_window,
)
from profiler.markets.resolve import resolve_strategy_profile
from profiler.strategy import StrategyProfile, write_strategy_artifacts
from profiler.tfvars import build_strategy_runtime_tfvars, write_tfvars
from profiler.ws_probe import probe_websocket


DEPLOYABLE_DECISIONS = {"deploy", "deploy_fallback", "deploy_standbys"}
TEARDOWN_MODES = {"never", "after-ready", "after-execution", "ttl"}
DIAGNOSTICS_MODES = {"none", "hot-path", "live-paper"}


@dataclass(frozen=True)
class ExecuteOptions:
    strategy_path: str
    run_id: str
    results_s3_prefix: str
    runtime_s3_prefix: str
    secrets_prefix: str | None = None
    work_dir: str | None = None
    tofu_dir: str = "infra/aws"
    samples: int = 3
    interval_ms: int = 1000
    apply: bool = False
    plan_only: bool = False
    auto_teardown: bool = False
    teardown_mode: str = "never"
    teardown_after_seconds: int = 0
    execution_timeout_seconds: int = 900
    execution_handoff: str = "ssm-immediate"
    execution_lead_seconds: int = 60
    diagnostics_mode: str = "none"
    diagnostics_s3_prefix: str | None = None
    require_public_ip: bool = False
    ready_timeout_seconds: int = 600
    runtime_ttl_seconds: int = 7200
    allow_live: bool = False
    live_capital_limit_usd: float = 0.0
    kill_switch_file: str | None = None


@dataclass(frozen=True)
class ExecuteResult:
    run_id: str
    work_dir: Path
    infra_recommendation_path: Path
    runtime_config_path: Path
    tfvars_path: Path
    runtime_config_s3_uri: str
    instance_id: str | None = None
    ready_status: dict[str, Any] | None = None
    execution_status: dict[str, Any] | None = None
    diagnostics_path: Path | None = None
    diagnostics_s3_uri: str | None = None
    teardown_performed: bool = False


def execute_strategy(options: ExecuteOptions) -> ExecuteResult:
    app_dir = Path.cwd()
    teardown_mode = _resolve_teardown_mode(options)
    diagnostics_mode = _resolve_diagnostics_mode(options)
    work_dir = Path(options.work_dir) if options.work_dir else Path(".generated") / options.run_id
    paths = _run_paths(work_dir, options.run_id)
    paths["root"].mkdir(parents=True, exist_ok=True)
    event_log = paths["events"]

    def event(stage: str, status: str, **details: Any) -> None:
        _write_event(event_log, stage=stage, status=status, run_id=options.run_id, details=details)

    kalshi_env = _kalshi_env_for_execute(options)
    strategy = resolve_strategy_profile(
        options.strategy_path,
        env=kalshi_env,
        audit_dir=paths["markets"],
    )
    event("strategy_loaded", "ok", strategy=strategy.name)
    event(
        "markets_discovered",
        "ok",
        source=strategy.markets.get("source"),
        kalshi_event_ticker=strategy.markets.get("kalshi_event_ticker"),
        bracket_count=len(strategy.markets.get("brackets", [])),
        spec_path=strategy.markets.get("spec_path"),
    )
    _upload_markets_artifacts(strategy, paths, options)

    artifacts = write_strategy_artifacts(strategy, paths["artifacts"])
    event("strategy_artifacts", "ok", artifacts={key: str(value) for key, value in artifacts.items()})

    runners = strategy.to_runners() or [Runner(provider="local", region="local", runtime="local", runner_id="local")]
    results = run_probes(strategy.targets, runners, samples=options.samples, interval_ms=options.interval_ms)
    write_jsonl(paths["results"], results)
    results_s3_uri = build_s3_run_uri(options.results_s3_prefix, options.run_id, paths["results"].name)
    upload_file_to_s3(paths["results"], results_s3_uri)
    event("profile", "ok", results=str(paths["results"]), results_s3_uri=results_s3_uri, count=len(results))

    recommendation = build_recommendation([result.to_dict() for result in results], strategy.to_placement_profile())
    write_recommendation(paths["placement"], recommendation)
    event("recommend", "ok", recommendation=str(paths["placement"]))

    artifact = artifact_with_resolved_image(strategy.deployment.get("artifact", {}))
    live_policy_report = build_live_policy_report(
        strategy,
        allow_live=options.allow_live,
        max_capital_usd=options.live_capital_limit_usd,
        artifact_image=artifact.get("image"),
        kill_switch_file=options.kill_switch_file,
    )
    paths["live_policy"].write_text(json.dumps(live_policy_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    event("live_policy", live_policy_report["decision"], report=str(paths["live_policy"]), failures=live_policy_report["failures"])
    if options.apply:
        enforce_live_policy(live_policy_report)

    infra_recommendation = build_infrastructure_recommendation(
        strategy,
        recommendation,
        run_id=options.run_id,
        artifact_override=artifact,
    )
    write_infrastructure_recommendation(paths["infra"], infra_recommendation)
    event(
        "infra_plan",
        "ok",
        recommendation=str(paths["infra"]),
        policy_decision=infra_recommendation.get("policy_decision", {}).get("decision"),
        artifact=infra_recommendation["recommended_infrastructure"].get("artifact", {}).get("image"),
    )

    decision = infra_recommendation.get("policy_decision", {}).get("decision")
    if options.apply and decision not in DEPLOYABLE_DECISIONS:
        event("policy", "failed", decision=decision)
        raise RuntimeError(f"Policy decision is {decision}; refusing to apply")

    runtime_config = build_nullone_runtime_config(strategy, infra_recommendation)
    write_runtime_config(paths["runtime_config"], runtime_config)
    runtime_config_s3_uri = build_s3_run_uri(options.runtime_s3_prefix, options.run_id, paths["runtime_config"].name)
    upload_file_to_s3(paths["runtime_config"], runtime_config_s3_uri)
    event("runtime_config", "ok", path=str(paths["runtime_config"]), s3_uri=runtime_config_s3_uri)

    secret_names = secret_names_for_prefix(options.secrets_prefix) if options.secrets_prefix else []
    secret_arns = (
        secret_arns_for_names(secret_names, region=infra_recommendation["recommended_infrastructure"]["region"])
        if secret_names
        else []
    )
    tfvars = build_strategy_runtime_tfvars(
        infra_recommendation,
        runtime_config_s3_uri=runtime_config_s3_uri,
        secret_arns=secret_arns,
        secret_names=secret_names,
        expires_at=_runtime_expires_at(options.runtime_ttl_seconds),
    )
    if options.require_public_ip:
        tfvars["strategy_require_public_ip"] = True
        tfvars["strategy_create_private_endpoints"] = False
    write_tfvars(paths["tfvars"], tfvars)
    event("tfvars", "ok", path=str(paths["tfvars"]), require_public_ip=tfvars.get("strategy_require_public_ip"))

    run_tofu(["init"], cwd=Path(options.tofu_dir))
    if options.plan_only or not options.apply:
        run_tofu(["plan", f"-var-file={paths['tfvars'].resolve()}"], cwd=Path(options.tofu_dir))
        event("tofu_plan", "ok")
        return ExecuteResult(
            run_id=options.run_id,
            work_dir=work_dir,
            infra_recommendation_path=paths["infra"],
            runtime_config_path=paths["runtime_config"],
            tfvars_path=paths["tfvars"],
            runtime_config_s3_uri=runtime_config_s3_uri,
        )

    run_tofu(["apply", "-auto-approve", f"-var-file={paths['tfvars'].resolve()}"], cwd=Path(options.tofu_dir))
    event("tofu_apply", "ok")

    instance_id = run_tofu_capture(["output", "-raw", "strategy_runtime_instance_id"], cwd=Path(options.tofu_dir)).strip()
    ready_status = wait_for_runtime_ready(instance_id, region=infra_recommendation["recommended_infrastructure"]["region"], timeout_seconds=options.ready_timeout_seconds)
    event("runtime_ready", ready_status["status"], instance_id=instance_id, output=ready_status.get("output"), error=ready_status.get("error"))

    if ready_status["status"] != "ok":
        raise RuntimeError(f"Runtime readiness failed for {instance_id}; see {event_log}")

    execution_status = None
    diagnostics_path = None
    diagnostics_s3_uri = None
    teardown_performed = False
    if teardown_mode == "after-execution" or diagnostics_mode in {"hot-path", "live-paper"}:
        handoff_mode = normalize_execution_handoff(options.execution_handoff)
        region = infra_recommendation["recommended_infrastructure"]["region"]
        diagnostic = diagnostics_mode if diagnostics_mode != "none" else False
        event(
            "execution_handoff",
            "ok",
            mode=handoff_mode,
            lead_seconds=options.execution_lead_seconds,
        )
        if handoff_mode == "host-timer":
            start_status = start_host_scheduled_execution(
                instance_id,
                region=region,
                lead_seconds=options.execution_lead_seconds,
                timeout_seconds=options.execution_timeout_seconds,
                diagnostic=diagnostic,
            )
            event("host_execute_start", start_status["status"], command_id=start_status.get("command_id"))
            if start_status["status"] != "ok":
                raise RuntimeError(f"Failed to start host-scheduled execution for {instance_id}")
            execution_status = poll_host_execution_result(
                instance_id,
                region=region,
                timeout_seconds=options.execution_timeout_seconds,
            )
        else:
            if handoff_mode == "ssm-lead":
                slept = wait_until_lead_window(
                    execute_at_from_runtime_config(runtime_config),
                    lead_seconds=options.execution_lead_seconds,
                )
                event("execution_lead_wait", "ok", slept_seconds=slept)
            execution_status = run_runtime_execution(
                instance_id,
                region=region,
                timeout_seconds=options.execution_timeout_seconds,
                diagnostic=diagnostic,
            )
            execution_status = {**execution_status, "handoff": handoff_mode}
        event(
            "runtime_execution",
            execution_status["status"],
            instance_id=instance_id,
            handoff=execution_status.get("handoff", handoff_mode),
            output=execution_status.get("output"),
            error=execution_status.get("error"),
        )
        if diagnostics_mode in {"hot-path", "live-paper"}:
            diagnostics_path, diagnostics_s3_uri = collect_hot_path_diagnostics(
                instance_id,
                region=infra_recommendation["recommended_infrastructure"]["region"],
                run_id=options.run_id,
                output_dir=paths["diagnostics"],
                execution_status=execution_status,
                upload_s3_prefix=options.diagnostics_s3_prefix,
            )
            event(
                "execution_diagnostics",
                "ok",
                mode=diagnostics_mode,
                path=str(diagnostics_path),
                s3_uri=diagnostics_s3_uri,
            )
        if execution_status["status"] != "ok":
            raise RuntimeError(f"Runtime execution failed for {instance_id}; see {event_log}")
        write_reconciliation_report(paths["reconciliation"], run_id=options.run_id, execution_status=execution_status)
        event("reconciliation", "ok", path=str(paths["reconciliation"]))

    if teardown_mode == "ttl":
        event("teardown_wait", "ok", seconds=options.teardown_after_seconds)
        time.sleep(options.teardown_after_seconds)

    if teardown_mode in {"after-ready", "after-execution", "ttl"}:
        teardown_strategy_runtime(options.run_id, app_dir=app_dir, yes=True)
        event("teardown", "ok")
        teardown_performed = True

    return ExecuteResult(
        run_id=options.run_id,
        work_dir=work_dir,
        infra_recommendation_path=paths["infra"],
        runtime_config_path=paths["runtime_config"],
        tfvars_path=paths["tfvars"],
        runtime_config_s3_uri=runtime_config_s3_uri,
        instance_id=instance_id,
        ready_status=ready_status,
        execution_status=execution_status,
        diagnostics_path=diagnostics_path,
        diagnostics_s3_uri=diagnostics_s3_uri,
        teardown_performed=teardown_performed,
    )


def run_probes(
    targets: list,
    runners: list[Runner],
    *,
    samples: int,
    interval_ms: int,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for sample_index in range(samples):
        for runner in runners:
            for target in targets:
                if target.type == "http":
                    results.append(probe_http(target, runner))
                elif target.type == "websocket":
                    results.append(probe_websocket(target, runner))
                else:
                    result = ProbeResult.empty(
                        target=target.name,
                        target_type=target.type,
                        provider=runner.provider,
                        region=runner.region,
                        runner_id=runner.runner_id or f"{runner.provider}-{runner.region}",
                    )
                    result.error = f"Unsupported target type: {target.type}"
                    results.append(result)
        if sample_index < samples - 1:
            time.sleep(interval_ms / 1000)
    return results


def wait_for_runtime_ready(instance_id: str, *, region: str, timeout_seconds: int) -> dict[str, Any]:
    client = _boto3_client("ssm", region)
    deadline = time.time() + timeout_seconds
    last_status: dict[str, Any] | None = None
    while time.time() < deadline:
        response = client.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
        if response.get("InstanceInformationList"):
            command = client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={
                    "commands": [
                        "systemctl is-active nullone-ready.service || true",
                        "systemctl is-active --quiet nullone-ready.service && echo __NULLONE_READY_OK__ || true",
                        "echo __NULLONE_BOOTSTRAP_STATE_BEGIN__",
                        "cat /opt/nullone/bootstrap/state.json || true",
                        "echo __NULLONE_BOOTSTRAP_STATE_END__",
                        "echo __NULLONE_BOOTSTRAP_LOG_BEGIN__",
                        "tail -200 /var/log/nullone/bootstrap.log || true",
                        "echo __NULLONE_BOOTSTRAP_LOG_END__",
                        "cat /var/log/nullone/ready.log || true",
                        "docker ps -a || true",
                        "/usr/local/bin/nullone-health && echo __NULLONE_HEALTH_OK__ || echo __NULLONE_HEALTH_FAILED__",
                    ]
                },
            )
            command_id = command["Command"]["CommandId"]
            invocation = _wait_for_command(client, command_id, instance_id, timeout_seconds=min(timeout_seconds, 300))
            output = invocation.get("StandardOutputContent", "")
            error = invocation.get("StandardErrorContent", "")
            if invocation.get("Status") == "Success" and _ready_output_ok(output):
                return {"status": "ok", "command_id": command_id, "output": output, "error": error}
            last_status = {"status": "pending", "command_id": command_id, "output": output, "error": error}
        time.sleep(10)
    if last_status:
        return {
            "status": "failed",
            "command_id": last_status.get("command_id"),
            "output": last_status.get("output", ""),
            "error": last_status.get("error", ""),
        }
    return {"status": "failed", "error": f"Timed out waiting for SSM registration for {instance_id}"}


def run_runtime_execution(
    instance_id: str,
    *,
    region: str,
    timeout_seconds: int,
    diagnostic: bool | str = False,
) -> dict[str, Any]:
    client = _boto3_client("ssm", region)
    command = client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": _runtime_execution_commands(timeout_seconds, diagnostic=diagnostic)},
    )
    command_id = command["Command"]["CommandId"]
    invocation = _wait_for_command(client, command_id, instance_id, timeout_seconds=timeout_seconds + 60)
    raw_output = invocation.get("StandardOutputContent", "")
    raw_error = invocation.get("StandardErrorContent", "")
    output = _extract_marker_section(raw_output, "STDOUT")
    error = _extract_marker_section(raw_output, "STDERR") or raw_error
    exit_code = _extract_execution_exit_code(raw_output)
    status = "ok" if invocation.get("Status") == "Success" and exit_code == 0 else "failed"
    return {
        "status": status,
        "command_id": command_id,
        "exit_code": exit_code,
        "output": output,
        "error": error,
        "raw_output": raw_output,
        "raw_error": raw_error,
    }


def collect_hot_path_diagnostics(
    instance_id: str,
    *,
    region: str,
    run_id: str,
    output_dir: Path,
    execution_status: dict[str, Any],
    upload_s3_prefix: str | None = None,
) -> tuple[Path, str | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "execution-stdout.txt").write_text(execution_status.get("output", ""), encoding="utf-8")
    (output_dir / "execution-stderr.txt").write_text(execution_status.get("error", ""), encoding="utf-8")
    trace_path = output_dir / "execution-trace.jsonl"
    trace_path.write_text(_json_lines_from_text(execution_status.get("output", "")), encoding="utf-8")
    latency_summary_path = output_dir / "latency-summary.json"
    latency_summary = write_latency_summary(trace_path, latency_summary_path)

    runtime_status = run_runtime_diagnostics(instance_id, region=region)
    diagnostics = {
        "schema_version": 1,
        "run_id": run_id,
        "instance_id": instance_id,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "execution": execution_status,
        "latency_summary": latency_summary,
        "runtime": runtime_status,
    }
    (output_dir / "runtime-diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    archive = output_dir / f"{run_id}-hot-path-diagnostics.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(output_dir.iterdir()):
            if path != archive and path.is_file():
                bundle.write(path, arcname=path.name)

    uploaded_to = None
    if upload_s3_prefix:
        uploaded_to = build_s3_run_uri(upload_s3_prefix, run_id, archive.name)
        upload_file_to_s3(trace_path, build_s3_run_uri(upload_s3_prefix, run_id, trace_path.name))
        upload_file_to_s3(latency_summary_path, build_s3_run_uri(upload_s3_prefix, run_id, latency_summary_path.name))
        upload_file_to_s3(archive, uploaded_to)
    return archive, uploaded_to


def run_runtime_diagnostics(instance_id: str, *, region: str) -> dict[str, Any]:
    client = _boto3_client("ssm", region)
    command = client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": [
                "systemctl status nullone-ready.service --no-pager || true",
                "echo __NULLONE_BOOTSTRAP_STATE_BEGIN__",
                "cat /opt/nullone/bootstrap/state.json || true",
                "echo __NULLONE_BOOTSTRAP_STATE_END__",
                "echo __NULLONE_BOOTSTRAP_LOG_BEGIN__",
                "tail -300 /var/log/nullone/bootstrap.log || true",
                "echo __NULLONE_BOOTSTRAP_LOG_END__",
                "cat /var/log/nullone/ready.log || true",
                "docker ps -a || true",
                "docker logs nullone-runtime --tail 300 || true",
                "cat /etc/nullone/runtime.json || true",
                "/usr/local/bin/nullone-health || true",
            ]
        },
    )
    command_id = command["Command"]["CommandId"]
    invocation = _wait_for_command(client, command_id, instance_id, timeout_seconds=300)
    return {
        "status": "ok" if invocation.get("Status") == "Success" else "failed",
        "command_id": command_id,
        "output": invocation.get("StandardOutputContent", ""),
        "error": invocation.get("StandardErrorContent", ""),
    }


def teardown_strategy_runtime(run_id: str, *, app_dir: Path, yes: bool = False) -> None:
    script = app_dir / "scripts" / "teardown_strategy_runtime.sh"
    args = ["bash", str(script), "--run-id", run_id]
    if yes:
        args.append("--yes")
    env = os.environ.copy()
    env["APP_DIR"] = str(app_dir)
    subprocess.run(args, check=True, env=env)


def run_tofu(args: list[str], *, cwd: Path) -> None:
    subprocess.run(["tofu", *args], cwd=cwd, check=True)


def run_tofu_capture(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(["tofu", *args], cwd=cwd, check=True, text=True, capture_output=True)
    return result.stdout


def _wait_for_command(client: Any, command_id: str, instance_id: str, *, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            invocation = client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except Exception as exc:
            if _is_retryable_command_poll_error(exc):
                time.sleep(2)
                continue
            raise
        if invocation["Status"] in {"Success", "Failed", "TimedOut", "Cancelled"}:
            return invocation
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for SSM command {command_id}")


def _is_retryable_command_poll_error(exc: Exception) -> bool:
    error = getattr(exc, "response", {}).get("Error", {})
    return error.get("Code") in {"InvocationDoesNotExist", "InvocationDoesNotExistException"}


def _ready_output_ok(output: str) -> bool:
    return (
        "__NULLONE_READY_OK__" in output
        or "__NULLONE_HEALTH_OK__" in output
        or '"ready": true' in output
        or '"healthy": true' in output
        or output.startswith("active\n")
        or "\nactive\n" in output
    )


def _resolve_teardown_mode(options: ExecuteOptions) -> str:
    mode = "after-ready" if options.auto_teardown and options.teardown_mode == "never" else options.teardown_mode
    if mode not in TEARDOWN_MODES:
        raise ValueError(f"Invalid teardown mode: {mode}")
    if mode == "ttl" and options.teardown_after_seconds <= 0:
        raise ValueError("--teardown-after-seconds must be greater than zero when --teardown-mode=ttl")
    return mode


def _resolve_diagnostics_mode(options: ExecuteOptions) -> str:
    if options.diagnostics_mode not in DIAGNOSTICS_MODES:
        raise ValueError(f"Invalid diagnostics mode: {options.diagnostics_mode}")
    return options.diagnostics_mode


def _runtime_expires_at(ttl_seconds: int) -> str:
    if ttl_seconds <= 0:
        return ""
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    return expires_at.isoformat(timespec="seconds").replace("+00:00", "Z")


def _runtime_execution_commands(timeout_seconds: int, *, diagnostic: bool | str = False) -> list[str]:
    command = _nullone_diagnostic_command(timeout_seconds, mode=diagnostic) if diagnostic else "/usr/local/bin/nullone-execute"
    return [
        "mkdir -p /tmp/nullone-hotpath",
        "date -u +%Y-%m-%dT%H:%M:%SZ > /tmp/nullone-hotpath/start.txt",
        (
            f"timeout {timeout_seconds + 15}s {command} "
            "> /tmp/nullone-hotpath/stdout.txt 2> /tmp/nullone-hotpath/stderr.txt; "
            "echo $? > /tmp/nullone-hotpath/exit-code.txt"
        ),
        "date -u +%Y-%m-%dT%H:%M:%SZ > /tmp/nullone-hotpath/end.txt",
        "echo __NULLONE_EXECUTION_EXIT_CODE__=$(cat /tmp/nullone-hotpath/exit-code.txt)",
        "echo __NULLONE_EXECUTION_START__=$(cat /tmp/nullone-hotpath/start.txt)",
        "echo __NULLONE_EXECUTION_END__=$(cat /tmp/nullone-hotpath/end.txt)",
        "echo __NULLONE_EXECUTION_STDOUT_BEGIN__",
        "cat /tmp/nullone-hotpath/stdout.txt || true",
        "echo __NULLONE_EXECUTION_STDOUT_END__",
        "echo __NULLONE_EXECUTION_STDERR_BEGIN__",
        "cat /tmp/nullone-hotpath/stderr.txt || true",
        "echo __NULLONE_EXECUTION_STDERR_END__",
    ]


def _nullone_diagnostic_command(timeout_seconds: int, *, mode: bool | str) -> str:
    if mode == "live-paper":
        command = (
            "nullone execute --config /etc/nullone/runtime.json "
            f"--paper-live-release --timeout-seconds {timeout_seconds} --trace-jsonl"
        )
    else:
        command = (
            "nullone execute --config /etc/nullone/runtime.json "
            f"--diagnostic --simulate packaged --timeout-seconds {timeout_seconds} --trace-jsonl"
        )
    return (
        "bash -lc 'source /opt/nullone/runtime.env; "
        'if [ "${NULLONE_ARTIFACT_TYPE:-}" = "container" ]; then '
        f"docker exec nullone-runtime {command}; "
        "else "
        f"{command}; "
        "fi'"
    )


def _extract_execution_exit_code(output: str) -> int | None:
    for line in output.splitlines():
        if line.startswith("__NULLONE_EXECUTION_EXIT_CODE__="):
            value = line.split("=", 1)[1].strip()
            return int(value) if value.isdigit() else None
    return None


def _extract_marker_section(output: str, name: str) -> str:
    begin = f"__NULLONE_EXECUTION_{name}_BEGIN__"
    end = f"__NULLONE_EXECUTION_{name}_END__"
    lines = output.splitlines()
    try:
        start_index = lines.index(begin) + 1
        end_index = lines.index(end)
    except ValueError:
        return ""
    return "\n".join(lines[start_index:end_index]) + ("\n" if end_index > start_index else "")


def _json_lines_from_text(value: str) -> str:
    lines: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        lines.append(json.dumps(parsed, sort_keys=True))
    return "\n".join(lines) + ("\n" if lines else "")


def _run_paths(work_dir: Path, run_id: str) -> dict[str, Path]:
    return {
        "root": work_dir,
        "artifacts": work_dir / "artifacts",
        "events": work_dir / "orchestration-events.jsonl",
        "results": work_dir / "results" / f"{run_id}.jsonl",
        "placement": work_dir / "recommendations" / f"{run_id}-placement.json",
        "infra": work_dir / "recommendations" / f"{run_id}-infra.json",
        "runtime_config": work_dir / "runtime" / f"{run_id}.json",
        "tfvars": work_dir / "strategy-runtime.tfvars.json",
        "diagnostics": work_dir / "diagnostics",
        "markets": work_dir / "markets",
        "live_policy": work_dir / "live-policy-report.json",
        "reconciliation": work_dir / "reconciliation-report.json",
    }


def _kalshi_env_for_execute(options: ExecuteOptions) -> dict[str, str] | None:
    if not options.secrets_prefix:
        return None
    return load_kalshi_env_from_prefix(options.secrets_prefix)


def _upload_markets_artifacts(
    strategy: StrategyProfile,
    paths: dict[str, Path],
    options: ExecuteOptions,
) -> None:
    spec_path = strategy.markets.get("spec_path")
    if not spec_path:
        return
    source = Path(spec_path)
    if not source.exists():
        return
    paths["markets"].mkdir(parents=True, exist_ok=True)
    destination = paths["markets"] / "markets-spec.yaml"
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    markets_s3_uri = build_s3_run_uri(options.runtime_s3_prefix, options.run_id, "markets-spec.yaml")
    upload_file_to_s3(destination, markets_s3_uri)


def _write_event(path: Path, *, stage: str, status: str, run_id: str, details: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_version": 1,
        "run_id": run_id,
        "stage": stage,
        "status": status,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "details": details,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True))
        handle.write("\n")


def _boto3_client(service: str, region: str):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to enable runtime verification") from exc
    return boto3.client(service, region_name=region)
