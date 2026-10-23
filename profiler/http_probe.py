from __future__ import annotations

import socket
import ssl
import time
from urllib.parse import urljoin, urlparse

import certifi

from profiler.config import Runner, Target
from profiler.dns_probe import resolve_hostname
from profiler.result import ProbeResult

CDN_HEADER_NAMES = {
    "cf-cache-status",
    "cf-ray",
    "x-amz-cf-id",
    "x-amz-cf-pop",
    "x-cache",
    "x-served-by",
}


def probe_http(target: Target, runner: Runner, max_redirects: int = 5) -> ProbeResult:
    result = ProbeResult.empty(
        target=target.name,
        target_type=target.type,
        provider=runner.provider,
        region=runner.region,
        runner_id=runner.runner_id or f"{runner.provider}-{runner.region}",
    )
    try:
        _probe_url(target, target.url, result, max_redirects=max_redirects)
    except Exception as exc:  # noqa: BLE001 - probe errors should be recorded, not raised.
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _probe_url(target: Target, url: str, result: ProbeResult, max_redirects: int) -> None:
    current_url = url
    total_started = time.perf_counter()

    for redirect_index in range(max_redirects + 1):
        parsed = urlparse(current_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
        host = parsed.hostname
        if host is None:
            raise ValueError(f"URL is missing hostname: {current_url}")

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        dns_ms, resolved_ips = resolve_hostname(host, port, target.timeout_ms)
        result.dns_ms = _add_ms(result.dns_ms, dns_ms)
        result.resolved_ips = _merge_ips(result.resolved_ips, resolved_ips)
        if not resolved_ips:
            raise OSError(f"No DNS records returned for {host}")

        remote_ip = resolved_ips[0]
        result.remote_ip = remote_ip

        sock = socket.socket(socket.AF_INET if ":" not in remote_ip else socket.AF_INET6, socket.SOCK_STREAM)
        sock.settimeout(target.timeout_ms / 1000)
        try:
            connect_started = time.perf_counter()
            sock.connect((remote_ip, port))
            result.tcp_connect_ms = _add_ms(result.tcp_connect_ms, _elapsed_ms(connect_started))

            active_sock: socket.socket | ssl.SSLSocket = sock
            if parsed.scheme == "https":
                context = ssl.create_default_context(cafile=certifi.where())
                tls_started = time.perf_counter()
                active_sock = context.wrap_socket(sock, server_hostname=host)
                result.tls_handshake_ms = _add_ms(result.tls_handshake_ms, _elapsed_ms(tls_started))
                result.tls_protocol = active_sock.version()

            headers = {
                "Host": host,
                "User-Agent": "source-latency-profiler/0.1",
                "Accept": "*/*",
                "Connection": "close",
                **target.headers,
            }
            request = _format_request(target.method, path, headers)

            active_sock.sendall(request)
            first_byte_started = time.perf_counter()
            first_chunk = active_sock.recv(1)
            result.ttfb_ms = _add_ms(result.ttfb_ms, _elapsed_ms(first_byte_started))
            if not first_chunk:
                raise ConnectionError("Connection closed before response")

            download_started = time.perf_counter()
            body = bytearray(first_chunk)
            while True:
                chunk = active_sock.recv(65536)
                if not chunk:
                    break
                body.extend(chunk)
            result.download_ms = _add_ms(result.download_ms, _elapsed_ms(download_started))
        finally:
            sock.close()

        status_code, response_headers, response_body = _parse_response(bytes(body))
        result.status_code = status_code
        result.response_bytes = len(response_body)
        result.server_header = response_headers.get("server")
        result.cdn_headers.update(
            {name: value for name, value in response_headers.items() if name in CDN_HEADER_NAMES}
        )

        location = response_headers.get("location")
        if status_code in {301, 302, 303, 307, 308} and location and redirect_index < max_redirects:
            result.redirect_count += 1
            result.redirect_chain.append(location)
            current_url = urljoin(current_url, location)
            continue

        if target.expected_status is not None and status_code != target.expected_status:
            result.error = f"Expected status {target.expected_status}, got {status_code}"
        break

    result.total_ms = round(_elapsed_ms(total_started), 3)


def _format_request(method: str, path: str, headers: dict[str, str]) -> bytes:
    header_lines = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return f"{method.upper()} {path} HTTP/1.1\r\n{header_lines}\r\n".encode("ascii")


def _parse_response(raw: bytes) -> tuple[int | None, dict[str, str], bytes]:
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    header_text = header_blob.decode("iso-8859-1", errors="replace")
    lines = header_text.split("\r\n")
    status_code: int | None = None
    if lines:
        parts = lines[0].split(" ", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            status_code = int(parts[1])

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return status_code, headers, body


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _add_ms(existing: float | None, value: float) -> float:
    return round((existing or 0.0) + value, 3)


def _merge_ips(existing: list[str], new_ips: list[str]) -> list[str]:
    merged = list(existing)
    for ip in new_ips:
        if ip not in merged:
            merged.append(ip)
    return merged
