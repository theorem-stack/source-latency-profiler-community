# Engineering Review: Latency-Sensitive Trading Architecture

This document captures an external engineering review of the source-latency-profiler + NullOne architecture, with notes on **what this repo implements today** versus **what belongs in NullOne** or **future orchestrator work**.

See also: [system-overview.md](./system-overview.md), [production_runbooks.md](./production_runbooks.md).

---

## 1. Structural Validation

### The Blackout Window (Separation of Planes)

The **profiler blackout (`profiler_stops_at`, default `T-2m`)** is the most critical design choice for keeping the hot path clean.

- Stopping telemetry probes and heavy orchestration before `T+0` keeps the local network stack, kernel buffers, and CPU cache available for `nullone execute`.
- This reduces context switching and packet queue contention when the EIA release hits.

**In this repo:** Blackout timing is written into `runtime.json` (`profiler_blackout_at`, `execute_at`) via `profiler/runtime_config.py`. The orchestrator stops its own probe/upload work before handoff; NullOne is responsible for honoring blackout inside the runtime.

### OpenTofu Jitter vs. Host Warmup

Provisioning ephemeral EC2 at `warmup_starts_at` (`T-30m`) balances cost and safety.

- 30 minutes absorbs transient AWS API throttling or instances stuck in `pending`.
- The hypervisor and Nitro path have time to settle before the execution blackout window.

**In this repo:** `strategy execute --apply` runs OpenTofu at scheduler warmup offset. Readiness is polled via SSM until `nullone-ready.service` and health checks pass.

---

## 2. Critical Edge Cases & Latency Risks

### Cold Starts & Connection Warmup (`nullone ready`)

`nullone ready` should open and **keep alive** connections (Kalshi WebSocket, etc.) through the blackout window.

| Risk | Mitigation owner |
|------|------------------|
| Sockets opened at warmup sit idle until `T+0` | **NullOne** — heartbeat/ping loop during blackout; hand off cleanly to execute |
| CDN or venue drops idle connections after 5–10 minutes | **NullOne** — reconnect policy or keepalive before release |
| Mid-hot-path reconnect at `T+0` | **NullOne** — must not block on TLS/DNS at execute time if avoidable |

**In this repo:** Orchestrator only starts `nullone ready` via EC2 user-data (`nullone-ready.service`). Connection lifecycle during blackout is **not** implemented here.

### SSM Agent Intrusiveness

The orchestrator triggers execute via **AWS SSM Run Command** (`profiler/orchestrator.py` → `run_runtime_execution`). The `amazon-ssm-agent` daemon can add CPU/disk activity when a command is dispatched.

| Risk | Mitigation |
|------|------------|
| SSM fork/log activity near `T+0` causes scheduling jitter | Fire SSM **before** release (e.g. `T-1m`); let NullOne block on clock until `execute_at` |
| Agent competes with hot-path CPU | Consider `nullone execute` started by **systemd timer** on the host instead of SSM at `T+0` |

**In this repo:** SSM fires when orchestrator reaches the execution step (after readiness), not necessarily aligned to `execute_at`. **Future work:** schedule SSM from `execute_at` minus a lead time, or use a host-local timer driven by `runtime.json`.

### Docker Networking Overhead

Default Docker bridge (`docker0`) adds NAT/`iptables` overhead and jitter.

**In this repo:** `infra/aws/strategy_runtime_user_data.sh` uses **`--network host`** for the NullOne container so the engine binds directly to the instance NIC.

### Kernel & Background Noise

Ephemeral trading hosts benefit from conservative network sysctl tuning and avoiding unnecessary work on the hot path.

**In this repo:** `strategy_runtime_user_data.sh` applies a small **kernel tuning** block at bootstrap (see script). Further hardening (disabling specific daemons) should be validated per AMI before production.

---

## 3. Data Flow Optimization Matrix

Profiler placement weights should reflect **hot-path impact**, not one-time connection costs:

| Metric | Profiler measurement | Hot-path impact if pre-warmed | Suggested priority |
|--------|----------------------|-------------------------------|-------------------|
| TLS handshake | v1 probe phase | Near zero if sockets stay open | Low |
| DNS resolution | v1 probe phase | Near zero if pinned early | Low |
| TTFB (EIA/Kalshi HTTP) | HTTP GET to sources/venues | **Critical** — CDN edge proximity | High |
| Jitter (RTT variance) | Repeated probes / WS connect | **Critical** — route/noisy-neighbor signal | Critical |

**In this repo:** Strategy `objective.weights` in YAML (e.g. `source_ttfb_p50`, `venue_total_p95`, `jitter`, `failure_rate`) drive `build_recommendation`. Tune weights per this matrix when calibrating for production releases.

---

## 4. Enhanced Diagnostics Architecture

Diagnostic runs (`--diagnostics hot-path` / `live-paper`) produce `execution-trace.jsonl` and `latency-summary.json`. Hot-path breakdown:

```text
[execute_start]
│
▼  engine_wakeup_ms — internal prep before first network I/O
[source_fetch_start]
│
▼  source_fetch_transit_ms — network + CDN to first byte
[source_fetch_done]
│
▼  decision_pipeline_ms — parse, map, vector, kelly
[order_submit_start]
│
▼  venue_submit_ack_ms — Kalshi ingress + ack
[order_submit_done]
```

**In this repo:** `profiler/latency.py` exports these derived gaps in `latency-summary.json` so region regressions can be split into **network** vs **compute** vs **venue** buckets.

---

## 5. Implementation Checklist

| Item | Status | Location |
|------|--------|----------|
| Profiler blackout timing in runtime config | Done | `profiler/runtime_config.py` |
| Docker host networking | Done | `infra/aws/strategy_runtime_user_data.sh` |
| Kernel network sysctl bootstrap | Done | `infra/aws/strategy_runtime_user_data.sh` |
| Hot-path trace gap decomposition | Done | `profiler/latency.py` |
| Idle connection keepalive through blackout | **NullOne** | — |
| SSM execute aligned to `execute_at` with lead time | **Done** (`ssm-lead`) | `profiler/execution_handoff.py` |
| Host-local scheduled execute (no SSM at T+0) | **Done** (`host-timer`) | `scripts/nullone-execute-scheduled.sh` |
| Sandbox strategy + scheduler units | **Done** | `strategies/nullone-wngsr-kalshi-sandbox.yaml`, `scripts/install-systemd-schedulers.sh` |
| Profiler weight calibration guide | Documented | This doc §3 |

---

## 6. Suggested Next Steps

1. **NullOne:** Confirm `ready` maintains venue/source keepalives from warmup through blackout handoff.
2. **Orchestrator:** Optionally trigger SSM execute at `T-1m` with NullOne blocking until `execute_at`.
3. **Operations:** Install host scheduler + janitor timers per [production_runbooks.md](./production_runbooks.md).
4. **Calibration:** Run `live-paper` rehearsal before each release; compare `latency-summary.json` gaps across regions.
