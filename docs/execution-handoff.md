# Execution Handoff Modes

How the orchestrator triggers NullOne `execute` on ephemeral runtime hosts without adding latency at `T+0`.

## Modes

| Mode | When SSM runs | Behavior at release |
|------|----------------|---------------------|
| `ssm-immediate` (CLI default) | Right after readiness | SSM forks `nullone-execute` immediately |
| `ssm-lead` | `execute_at - lead_seconds` | SSM starts execute at lead window; NullOne should block until release |
| `host-timer` (scheduler default) | Once after readiness | `nohup nullone-execute-scheduled` sleeps until lead window, then runs execute |

### `host-timer` (recommended for production)

1. After `nullone ready` passes, orchestrator sends **one** SSM command to start `/usr/local/bin/nullone-execute-scheduled` in the background.
2. The script reads `runtime.execute_at` from `/etc/nullone/runtime.json`.
3. It sleeps until `execute_at - execution_lead_seconds` (default 60s).
4. It runs `nullone-execute` (or diagnostic command) with timeout.
5. It writes `/opt/nullone/execute-result.json`.
6. Orchestrator polls that file via SSM until complete or timeout.

**Benefit:** No SSM agent fork/disk activity at `T+0`.

The script is installed on runtime hosts via `strategy_runtime_user_data.sh` (base64 from `scripts/nullone-execute-scheduled.sh`).

### `ssm-lead`

Orchestrator blocks until the lead window, then uses the existing `run_runtime_execution` SSM path. Useful when testing handoff timing without relying on the host script.

## CLI

```bash
.venv/bin/python -m profiler.cli strategy execute \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --run-id wngsr-$(date -u +%Y%m%d%H%M%S) \
  --secrets-prefix nullone/prod \
  --results-s3-prefix s3://<bucket>/profiler \
  --runtime-s3-prefix s3://<bucket>/runtime \
  --apply \
  --teardown-mode after-execution \
  --execution-handoff host-timer \
  --execution-lead-seconds 60
```

## Scheduler default

`profiler.cli scheduler run` defaults to `--execution-handoff host-timer` for production and sandbox systemd units.

## NullOne expectations

For `ssm-lead` and optimal `host-timer` behavior, NullOne `execute` should tolerate being started slightly before `execute_at` and block until the release clock. If NullOne only starts work at process launch without an internal clock wait, `host-timer` still works because the scheduled script sleeps until the lead window before exec — but starting at `T-1m` requires NullOne to hold connections opened during `ready`.

## Related

- [engineering-review.md](./engineering-review.md) — SSM intrusiveness rationale
- [production_runbooks.md](./production_runbooks.md) — scheduler install
- `scripts/install-systemd-schedulers.sh` — prod/sandbox/diagnostic timers
