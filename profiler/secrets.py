from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RUNTIME_ENV_SECRET = "runtime/env"
KALSHI_PRIVATE_KEY_SECRET = "kalshi/private_key_pem"
RUNTIME_PRIVATE_KEY_PATH = "/run/secrets/kalshi-private-key.pem"


@dataclass(frozen=True)
class SecretSpec:
    name: str
    description: str
    value: str
    content_type: str

    def redacted(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "content_type": self.content_type,
            "bytes": len(self.value.encode("utf-8")),
            "value": "<redacted>",
        }


@dataclass(frozen=True)
class SecretPlan:
    prefix: str
    specs: list[SecretSpec]
    env_keys: list[str]

    @property
    def names(self) -> list[str]:
        return [spec.name for spec in self.specs]

    def redacted(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "secrets": [spec.redacted() for spec in self.specs],
            "env_keys": self.env_keys,
        }


def parse_env_file(path: str | Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_inline_comment(value.strip())
        env[key] = _strip_quotes(value)
    return env


def build_secret_plan(
    *,
    env_file: str | Path,
    prefix: str,
    pem_file: str | Path | None = None,
) -> SecretPlan:
    normalized_prefix = prefix.strip("/")
    env = parse_env_file(env_file)

    pem_value = _load_pem_value(env, pem_file)
    if pem_value:
        env["KALSHI_PRIVATE_KEY_PATH"] = RUNTIME_PRIVATE_KEY_PATH
        env.pop("KALSHI_PRIVATE_KEY_PEM", None)

    runtime_env = {key: value for key, value in env.items() if value != ""}
    specs = [
        SecretSpec(
            name=f"{normalized_prefix}/{RUNTIME_ENV_SECRET}",
            description="NullOne runtime environment variables",
            value=json.dumps(runtime_env, indent=2, sort_keys=True),
            content_type="application/json",
        )
    ]
    if pem_value:
        specs.append(
            SecretSpec(
                name=f"{normalized_prefix}/{KALSHI_PRIVATE_KEY_SECRET}",
                description="Kalshi private key PEM for NullOne",
                value=pem_value,
                content_type="application/x-pem-file",
            )
        )

    return SecretPlan(prefix=normalized_prefix, specs=specs, env_keys=sorted(runtime_env))


def sync_secret_plan(plan: SecretPlan, *, region: str | None = None) -> list[dict[str, str]]:
    client = _secretsmanager_client(region=region)
    results: list[dict[str, str]] = []
    for spec in plan.specs:
        try:
            client.describe_secret(SecretId=spec.name)
        except client.exceptions.ResourceNotFoundException:
            response = client.create_secret(
                Name=spec.name,
                Description=spec.description,
                SecretString=spec.value,
            )
            results.append({"name": spec.name, "action": "created", "arn": response["ARN"]})
        else:
            response = client.put_secret_value(SecretId=spec.name, SecretString=spec.value)
            results.append({"name": spec.name, "action": "updated", "arn": response["ARN"]})
    return results


def validate_secret_prefix(prefix: str, *, region: str | None = None) -> list[dict[str, Any]]:
    client = _secretsmanager_client(region=region)
    expected = [
        f"{prefix.strip('/')}/{RUNTIME_ENV_SECRET}",
        f"{prefix.strip('/')}/{KALSHI_PRIVATE_KEY_SECRET}",
    ]
    results: list[dict[str, Any]] = []
    for name in expected:
        try:
            response = client.describe_secret(SecretId=name)
        except client.exceptions.ResourceNotFoundException:
            results.append({"name": name, "exists": False, "arn": None})
        else:
            results.append({"name": name, "exists": True, "arn": response["ARN"]})
    return results


def load_kalshi_env_from_prefix(prefix: str, *, region: str | None = None) -> dict[str, str]:
    """Load Kalshi-related environment variables from the runtime/env secret."""
    client = _secretsmanager_client(region=region)
    secret_id = f"{prefix.strip('/')}/{RUNTIME_ENV_SECRET}"
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except client.exceptions.ResourceNotFoundException as exc:
        raise RuntimeError(f"Secrets Manager secret not found: {secret_id}") from exc

    payload = response.get("SecretString") or ""
    if not payload:
        raise RuntimeError(f"Secrets Manager secret is empty: {secret_id}")

    env = json.loads(payload)
    if not isinstance(env, dict):
        raise RuntimeError(f"runtime/env secret must be a JSON object: {secret_id}")

    result = {str(key): str(value) for key, value in env.items() if value != ""}

    pem_secret_id = f"{prefix.strip('/')}/{KALSHI_PRIVATE_KEY_SECRET}"
    try:
        pem_response = client.get_secret_value(SecretId=pem_secret_id)
        pem_value = (pem_response.get("SecretString") or "").strip()
        if pem_value:
            result["KALSHI_PRIVATE_KEY_PEM"] = pem_value
    except client.exceptions.ResourceNotFoundException:
        pass

    return result


def secret_names_for_prefix(prefix: str) -> list[str]:
    normalized_prefix = prefix.strip("/")
    return [
        f"{normalized_prefix}/{RUNTIME_ENV_SECRET}",
        f"{normalized_prefix}/{KALSHI_PRIVATE_KEY_SECRET}",
    ]


def secret_arns_for_names(names: list[str], *, region: str | None = None, account_id: str | None = None) -> list[str]:
    if not names:
        return []
    if region is None or account_id is None:
        sts = _sts_client(region=region)
        identity = sts.get_caller_identity()
        account_id = account_id or identity["Account"]
        region = region or _session_region()
    return [f"arn:aws:secretsmanager:{region}:{account_id}:secret:{name}*" for name in names]


def _load_pem_value(env: dict[str, str], pem_file: str | Path | None) -> str | None:
    if pem_file:
        return Path(pem_file).read_text(encoding="utf-8").strip()
    inline_pem = env.get("KALSHI_PRIVATE_KEY_PEM", "").strip()
    if inline_pem:
        return inline_pem
    pem_path = env.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    if pem_path and Path(pem_path).exists():
        return Path(pem_path).read_text(encoding="utf-8").strip()
    return None


def _strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(value):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or value[index - 1].isspace():
                return value[:index].strip()
    return value


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _secretsmanager_client(*, region: str | None = None):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to enable Secrets Manager operations") from exc
    return boto3.client("secretsmanager", region_name=region or _session_region())


def _sts_client(*, region: str | None = None):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to resolve secret ARNs") from exc
    return boto3.client("sts", region_name=region or _session_region())


def _session_region() -> str:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to resolve AWS region") from exc
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or boto3.session.Session().region_name
        or "us-east-1"
    )
