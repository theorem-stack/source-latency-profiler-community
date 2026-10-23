from __future__ import annotations

import asyncio
import time

from profiler.config import Runner, Target
from profiler.result import ProbeResult


def probe_websocket(target: Target, runner: Runner) -> ProbeResult:
    result = ProbeResult.empty(
        target=target.name,
        target_type=target.type,
        provider=runner.provider,
        region=runner.region,
        runner_id=runner.runner_id or f"{runner.provider}-{runner.region}",
    )
    try:
        return asyncio.run(_probe_websocket_async(target, result))
    except Exception as exc:  # noqa: BLE001 - probe errors should be recorded, not raised.
        result.error = f"{type(exc).__name__}: {exc}"
        return result


async def _probe_websocket_async(target: Target, result: ProbeResult) -> ProbeResult:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("Install with the 'ws' extra to enable WebSocket probes") from exc

    started = time.perf_counter()
    timeout = target.timeout_ms / 1000
    async with websockets.connect(
        target.url,
        open_timeout=timeout,
        close_timeout=timeout,
        additional_headers=target.headers or None,
    ) as websocket:
        result.websocket_upgrade_ms = _elapsed_ms(started)

        if target.subscription_message:
            send_started = time.perf_counter()
            await websocket.send(target.subscription_message)
            result.subscription_send_ms = _elapsed_ms(send_started)

        first_message_started = time.perf_counter()
        try:
            message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            result.first_message_ms = _elapsed_ms(first_message_started)
            result.response_bytes = len(message if isinstance(message, bytes) else message.encode("utf-8"))
        except TimeoutError:
            result.error = "Timed out waiting for first WebSocket message"

    result.total_ms = _elapsed_ms(started)
    return result


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
