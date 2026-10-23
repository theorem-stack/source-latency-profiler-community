from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class ProbeResult:
    schema_version: int
    target: str
    target_type: str
    provider: str
    region: str
    runner_id: str
    timestamp: str
    dns_ms: float | None = None
    tcp_connect_ms: float | None = None
    tls_handshake_ms: float | None = None
    websocket_upgrade_ms: float | None = None
    subscription_send_ms: float | None = None
    first_message_ms: float | None = None
    ttfb_ms: float | None = None
    download_ms: float | None = None
    total_ms: float | None = None
    status_code: int | None = None
    response_bytes: int | None = None
    redirect_count: int = 0
    redirect_chain: list[str] = field(default_factory=list)
    resolved_ips: list[str] = field(default_factory=list)
    remote_ip: str | None = None
    tls_protocol: str | None = None
    server_header: str | None = None
    cdn_headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def empty(
        cls,
        *,
        target: str,
        target_type: str,
        provider: str,
        region: str,
        runner_id: str,
    ) -> "ProbeResult":
        return cls(
            schema_version=1,
            target=target,
            target_type=target_type,
            provider=provider,
            region=region,
            runner_id=runner_id,
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                results.append(json.loads(stripped))
    return results


def write_jsonl(path: str | Path, results: list[ProbeResult]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(result.to_json())
            handle.write("\n")
