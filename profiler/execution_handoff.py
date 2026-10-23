from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXECUTION_HANDOFF_MODES = frozenset({"ssm-immediate", "ssm-lead", "host-timer"})


def normalize_execution_handoff(value: str) -> str:
    mode = (value or "ssm-immediate").strip().lower()
    if mode not in EXECUTION_HANDOFF_MODES:
        raise ValueError(f"unsupported execution handoff mode: {value}")
    return mode


def execute_at_from_runtime_config(runtime_config: dict[str, Any]) -> datetime:
    raw = runtime_config.get("runtime", {}).get("execute_at")
    if not raw:
        raise ValueError("runtime.execute_at is required for scheduled execution handoff")
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def wait_until_lead_window(execute_at: datetime, *, lead_seconds: int) -> float:
    if lead_seconds < 0:
        raise ValueError("execution lead seconds must be non-negative")
    now = datetime.now(UTC)
    comparable_execute_at = execute_at if execute_at.tzinfo else execute_at.replace(tzinfo=UTC)
    lead_at = comparable_execute_at.timestamp() - lead_seconds
    sleep_for = max(0.0, lead_at - now.timestamp())
    if sleep_for:
        time.sleep(sleep_for)
    return sleep_for


def start_host_scheduled_execution(
    instance_id: str,
    *,
    region: str,
    lead_seconds: int,
    timeout_seconds: int,
    diagnostic: bool | str = False,
) -> dict[str, Any]:
    diagnostic_mode = _diagnostic_mode(diagnostic)
    client = _boto3_client("ssm", region)
    command = client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": [
                "rm -f /opt/nullone/execute-result.json",
                (
                    "nohup /usr/local/bin/nullone-execute-scheduled "
                    f"--config /etc/nullone/runtime.json "
                    f"--lead-seconds {lead_seconds} "
                    f"--timeout-seconds {timeout_seconds} "
                    f"--diagnostic {diagnostic_mode} "
                    "--result /opt/nullone/execute-result.json "
                    ">> /var/log/nullone/scheduled-execute.log 2>&1 &"
                ),
                "echo __HOST_EXECUTE_STARTED__",
            ]
        },
    )
    command_id = command["Command"]["CommandId"]
    invocation = _wait_for_command(client, command_id, instance_id, timeout_seconds=120)
    output = invocation.get("StandardOutputContent", "")
    status = "ok" if invocation.get("Status") == "Success" and "__HOST_EXECUTE_STARTED__" in output else "failed"
    return {
        "status": status,
        "command_id": command_id,
        "output": output,
        "error": invocation.get("StandardErrorContent", ""),
        "handoff": "host-timer",
    }


def poll_host_execution_result(
    instance_id: str,
    *,
    region: str,
    timeout_seconds: int,
    poll_seconds: int = 15,
) -> dict[str, Any]:
    client = _boto3_client("ssm", region)
    deadline = time.time() + timeout_seconds + 60
    last_payload: dict[str, Any] | None = None
    while time.time() < deadline:
        command = client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [
                    "if [ -f /opt/nullone/execute-result.json ]; then cat /opt/nullone/execute-result.json; else echo __HOST_EXECUTE_PENDING__; fi",
                    "tail -50 /var/log/nullone/scheduled-execute.log 2>/dev/null || true",
                ]
            },
        )
        command_id = command["Command"]["CommandId"]
        invocation = _wait_for_command(client, command_id, instance_id, timeout_seconds=min(120, poll_seconds + 30))
        output = invocation.get("StandardOutputContent", "")
        if "__HOST_EXECUTE_PENDING__" in output and "finished_at" not in output:
            time.sleep(max(1, poll_seconds))
            continue
        payload = _parse_host_execution_payload(output)
        if payload:
            last_payload = payload
            if payload.get("status") in {"ok", "failed"}:
                exit_code = int(payload.get("exit_code") or 0)
                return {
                    "status": payload["status"] if payload["status"] == "ok" and exit_code == 0 else "failed",
                    "exit_code": exit_code,
                    "output": payload.get("output", ""),
                    "error": payload.get("error", ""),
                    "handoff": "host-timer",
                    "finished_at": payload.get("finished_at"),
                }
        time.sleep(max(1, poll_seconds))
    if last_payload:
        return {
            "status": "failed",
            "exit_code": int(last_payload.get("exit_code") or 1),
            "output": last_payload.get("output", ""),
            "error": last_payload.get("error", ""),
            "handoff": "host-timer",
            "finished_at": last_payload.get("finished_at"),
        }
    return {
        "status": "failed",
        "exit_code": 124,
        "output": "",
        "error": f"Timed out waiting for host execution result on {instance_id}",
        "handoff": "host-timer",
    }


def _parse_host_execution_payload(output: str) -> dict[str, Any] | None:
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "status" in parsed:
            return parsed
    return None


def _diagnostic_mode(diagnostic: bool | str) -> str:
    if diagnostic is True:
        return "hot-path"
    if diagnostic is False or diagnostic in {None, "", "none"}:
        return "none"
    return str(diagnostic)


def _wait_for_command(client, command_id: str, instance_id: str, *, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        status = response.get("Status")
        if status in {"Success", "Cancelled", "TimedOut", "Failed"}:
            return response
        time.sleep(2)
    return {"Status": "TimedOut", "StandardOutputContent": "", "StandardErrorContent": "poll timeout"}


def _boto3_client(service: str, region: str):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to enable execution handoff") from exc
    return boto3.client(service, region_name=region)
