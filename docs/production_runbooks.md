# Production Runbooks

These commands assume the orchestrator host has the repo deployed at `/opt/source-latency-profiler`, the virtualenv is installed, and AWS credentials resolve through the instance profile.

## IAM Preflight

Run this before a new account, role, or bucket is used for an applied strategy run:

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli infra preflight \
  --account-id <aws_account_id> \
  --region us-east-1 \
  --results-bucket source-latency-profiler-results-<aws_account_id> \
  --deploy-bucket <deploy_bucket>
```

If it fails, add the missing action names to the orchestrator instance role. Use `--policy-only` to generate an account-specific policy document.

Verify NullOne is installed for mandatory market discovery:

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli infra preflight \
  --account-id <aws_account_id> \
  --region us-east-1 \
  --results-bucket source-latency-profiler-results-<aws_account_id> \
  --check-nullone
```

Set `NULLONE_ROOT=/opt/nullone` on the orchestrator host so `nullone discover` writes specs under NullOne's `specs/` directory. Bootstrap now creates/chowns `${NULLONE_ROOT}/specs` for the scheduler runtime user, so manual permission fixes are not required in the clean path.

## Mandatory market discovery

Kalshi bracket discovery lives in **NullOne only**. The profiler always runs `nullone discover` before `strategy execute` (authoritative refresh). Production strategy YAML uses `markets.source: nullone_discover` without hardcoded brackets.

For calendar-template strategies, use `scheduler run` for manual checks (not `strategy import-markets`, which expects a concrete `event_time` in the strategy file):

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli scheduler run \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --event-calendar calendars/kalshi-wngsr-2026.json \
  --secrets-prefix nullone/prod \
  --state-path /tmp/manual-state.json \
  --events-path /tmp/manual-events.jsonl \
  --once
```

Early refresh failures (markets not listed yet) are expected; retry until discover succeeds.

## Host scheduler (market refresh + warmup execute)

Install scheduler timers with explicit NullOne S3 release contract:

```bash
AWS_REGION=us-east-1 \
INSTALL_SCHEDULERS=1 \
NULLONE_INSTALL_MODE=s3 \
NULLONE_S3_URI=s3://source-latency-profiler-results-<aws_account_id>/nullone/releases/<version>/linux-amd64/nullone \
/opt/source-latency-profiler/scripts/deploy_orchestrator_ec2.sh
```

This installs production (`source-latency-profiler-scheduler-prod.timer`), janitor, and optional sandbox/diagnostic units. Production runs use `--execution-handoff host-timer` so SSM does not fire at `T+0`. See `docs/execution-handoff.md`.

**Sandbox drills:** set `event_time` in `strategies/nullone-wngsr-kalshi-sandbox.yaml` to an upcoming instant, reset `.generated/scheduler/sandbox-state.json` if re-running offsets, then:

```bash
sudo systemctl enable --now source-latency-profiler-scheduler-sandbox.timer
```

Dry-run due actions (with durable state/log paths):

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli scheduler run \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --event-calendar calendars/kalshi-wngsr-2026.json \
  --secrets-prefix nullone/prod \
  --state-path /var/lib/source-latency-profiler/scheduler/manual-prod-state.json \
  --events-path /var/log/source-latency-profiler/manual-prod-events.jsonl \
  --dry-run \
  --once
```

The scheduler runs mandatory `nullone discover` at each `markets.refresh.offsets` entry (`T-7d`, `T-3d`, `T-1d`, `T-6h`, `T-30m`), then runs `strategy execute --apply` at `warmup_starts_at`. Audit logs are written under `/var/log/source-latency-profiler`.

### Scheduler troubleshooting ladder

Run these in order from the orchestrator host:

1. Identity and role:
   - `aws sts get-caller-identity`
   - `aws secretsmanager describe-secret --secret-id nullone/prod/runtime/env --region us-east-1`
2. NullOne binary:
   - `source /etc/source-latency-profiler/nullone.env`
   - `"${NULLONE_BIN}" --help`
3. Unit wiring:
   - `sudo systemctl cat source-latency-profiler-scheduler-prod.service`
4. Runability:
   - check writable state/events paths under `/var/lib/source-latency-profiler/scheduler` and `/var/log/source-latency-profiler`.
5. NullOne specs path:
   - check `${NULLONE_ROOT}/specs` is writable by the scheduler runtime user.
6. Action-level root cause:
   - `tail -n 20 /var/log/source-latency-profiler/prod-events.jsonl`

### Expected strict-fail scenarios

The production scheduler is intentionally strict: any failed due action exits non-zero.
Expected transient failures include:

- `no_tradeable_events` while markets are not listed yet for the current week.
- discover configuration mismatch (`series_ticker` invalid).
- missing/inaccessible secrets for `--secrets-prefix`.

Do not soften these failures in production; let timers retry once inputs become valid.

### Fresh-host validation checklist

After provisioning a new orchestrator EC2 host, run this checklist once:

1. Bootstrap artifacts and runtime:
   - `source /etc/source-latency-profiler/nullone.env`
   - `"${NULLONE_BIN}" --help`
2. Scheduler unit install:
   - `sudo APP_DIR=/opt/source-latency-profiler RESULTS_BUCKET=<results_bucket> AWS_REGION=us-east-1 SCHEDULER_STATE_DIR=/var/lib/source-latency-profiler/scheduler SCHEDULER_EVENTS_DIR=/var/log/source-latency-profiler NULLONE_ENV_FILE=/etc/source-latency-profiler/nullone.env /opt/source-latency-profiler/scripts/install-systemd-schedulers.sh`
3. Unit wiring:
   - `sudo systemctl cat source-latency-profiler-scheduler-prod.service`
   - verify `EnvironmentFile=/etc/source-latency-profiler/nullone.env`
4. One-shot scheduler sanity:
   - `/opt/source-latency-profiler/.venv/bin/python -m profiler.cli scheduler run --strategy strategies/nullone-wngsr-kalshi.yaml --event-calendar calendars/kalshi-wngsr-2026.json --secrets-prefix nullone/prod --state-path /tmp/prod-state.json --events-path /tmp/prod-events.jsonl --once --dry-run`
5. Event log verification:
   - `tail -n 5 /var/log/source-latency-profiler/prod-events.jsonl`

One-pass writable-path probe:

```bash
sudo -u ec2-user sh -c 'touch /var/lib/source-latency-profiler/scheduler/.write-check && rm -f /var/lib/source-latency-profiler/scheduler/.write-check'
sudo -u ec2-user sh -c 'touch /var/log/source-latency-profiler/.write-check && rm -f /var/log/source-latency-profiler/.write-check'
sudo -u ec2-user sh -c 'touch /opt/nullone/specs/.write-check && rm -f /opt/nullone/specs/.write-check'
```

### NullOne release rollout / rollback

Promote a release by changing `NULLONE_S3_URI` to a new immutable key:

```bash
NULLONE_S3_URI=s3://source-latency-profiler-results-<aws_account_id>/nullone/releases/vX.Y.Z/linux-amd64/nullone
```

Rollback by re-running deploy with the prior immutable key:

```bash
NULLONE_S3_URI=s3://source-latency-profiler-results-<aws_account_id>/nullone/releases/vX.Y.(Z-1)/linux-amd64/nullone
```

## Smoke E2E

Use this to prove provisioning, readiness, and teardown without running strategy execution:

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli strategy execute \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --run-id smoke-$(date -u +%Y%m%d%H%M%S) \
  --secrets-prefix nullone/prod \
  --results-s3-prefix s3://<results_bucket>/profiler \
  --runtime-s3-prefix s3://<results_bucket>/runtime \
  --apply \
  --teardown-mode after-ready \
  --samples 1
```

`--secrets-prefix` is required so the orchestrator can run mandatory `nullone discover` before provisioning.

## Diagnostic Hot Path E2E

Use this to get bounded fixture-mode latency artifacts:

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli strategy execute \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --run-id diag-$(date -u +%Y%m%d%H%M%S) \
  --results-s3-prefix s3://<results_bucket>/profiler \
  --runtime-s3-prefix s3://<results_bucket>/runtime \
  --diagnostics hot-path \
  --diagnostics-s3-prefix s3://<results_bucket>/diagnostics \
  --apply \
  --teardown-mode after-execution \
  --execution-timeout-seconds 120 \
  --samples 1
```

Inspect `latency-summary.json` first, then the raw `execution-trace.jsonl` if the largest gap needs deeper investigation.

## Live Paper Rehearsal

Use this for a real release window with live data and forced paper orders:

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli strategy execute \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --run-id live-paper-$(date -u +%Y%m%d%H%M%S) \
  --results-s3-prefix s3://<results_bucket>/profiler \
  --runtime-s3-prefix s3://<results_bucket>/runtime \
  --diagnostics live-paper \
  --diagnostics-s3-prefix s3://<results_bucket>/diagnostics \
  --apply \
  --teardown-mode after-execution \
  --execution-timeout-seconds 3600 \
  --samples 1
```

Confirm the trace includes `release_wait_start`, `release_wait_done`, `live_source_fetch_start`, and `live_source_fetch_done`.

## Stuck SSM Command

List recent commands and cancel a stuck command if needed:

```bash
aws ssm list-command-invocations --details --region us-east-1
aws ssm cancel-command --command-id <command_id> --region us-east-1
```

If cancellation is denied, the orchestrator role is missing `ssm:CancelCommand`.

## Orphan Cleanup

Dry-run expired runtime cleanup:

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli strategy janitor --region us-east-1
```

Execute cleanup:

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli strategy janitor --region us-east-1 --yes
```

The systemd timer templates are in `scripts/source-latency-profiler-janitor.service` and `scripts/source-latency-profiler-janitor.timer`.

## Failed Image Resolution

Verify the runtime image independently:

```bash
/opt/source-latency-profiler/.venv/bin/python -m profiler.cli artifacts resolve-ecr \
  --repository <account>.dkr.ecr.us-east-1.amazonaws.com/nullone \
  --tag-prefix nullone-runtime-
```

Avoid `latest` for live or live-paper releases; production guardrails require an immutable-looking tag or digest.

## Failed Readiness

The readiness failure output includes:

- `/opt/nullone/bootstrap/state.json`
- `/var/log/nullone/bootstrap.log`
- `/var/log/nullone/ready.log`
- `docker ps -a`
- `nullone-health`

Use the latest bootstrap stage to decide whether the failure is S3, secrets, ECR, Docker, wrapper creation, or ready service startup.
