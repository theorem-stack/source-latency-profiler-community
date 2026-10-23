from __future__ import annotations

import socket
import time


def resolve_hostname(hostname: str, port: int, timeout_ms: int) -> tuple[float, list[str]]:
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout_ms / 1000)
    started = time.perf_counter()
    try:
        answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    finally:
        socket.setdefaulttimeout(previous_timeout)
    elapsed_ms = (time.perf_counter() - started) * 1000

    ips: list[str] = []
    for answer in answers:
        ip = answer[4][0]
        if ip not in ips:
            ips.append(ip)
    return elapsed_ms, ips
