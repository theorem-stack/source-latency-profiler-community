# Source Latency Profiler

Source Latency Profiler measures HTTP and WebSocket latency from the machine running the CLI, ranks those samples, and can turn a strategy file into an AWS deployment plan for a separate runtime called NullOne.

Version 0.1.0. Python 3.11+. The console script is `latency-profiler` (`profiler.cli:main`).

It is infrastructure and measurement code. Order handling stays in NullOne. This repository can start that process on an EC2 host and record what it prints. It does not contain a trading engine.

## Related project

[NullOne](https://github.com/theorem-stack/NullOne-community) is a Python bot that trades Kalshi and Polymarket brackets on the EIA weekly natural gas storage report. Paper mode is the default. This profiler measures candidate hosts so that bot can be placed where the source and the venue respond fastest.

## Why it matters

A scheduled data release is only as fast as the path from the host that reads it to the venue that must hear the reaction. Public APIs sit behind CDNs, anycast, and load balancers, so "where the API is hosted" is the wrong question. The useful question is: from this process, what do DNS, TCP, TLS, time to first byte, and total time actually look like, and which labeled location wins on tail latency, jitter, and failures?

The profiler records those phases for endpoints you name explicitly. Strategy profiles then score candidate locations with weights you set, and they refuse an automatic deploy when the sample count, confidence, score margin, or deploy cutoff fails.

## What the code does

**Measure.** `probe` (alias `run`) and `burst` load a YAML target file and open sockets in this process. Each sample records DNS time, resolved addresses, TCP connect, TLS handshake and protocol, status code, time to first byte, download time, total time, redirect chain, selected response headers (`server`, Cloudflare and cache headers), byte count, and the error string when a probe fails. WebSocket targets, when the `ws` extra is installed, record upgrade time and time to the first message. `burst` refuses an interval below 250 ms.

**Rank.** `report` groups JSONL by target, provider, and region. It prints count, success rate, p50, p95, p99, and jitter. Jitter in that table is the median absolute deviation of total time. The same summary also keeps p90, min, max, standard deviation, DNS variance, and the remote-address counts.

**Recommend.** `recommend` scores each provider/region that has every target in a placement profile. The score is a weighted sum of the profile's features. In the checked-in NullOne profiles those features are source TTFB p50 and p95, venue total p50 and p95, jitter, and failure rate. Lower is better. A missing feature adds `weight * 1_000_000`. Failure rate is scaled by `10_000` before the weight is applied. Confidence is 0 until `minimum_samples` is met. After that it is `0.45 * success + 0.25 * sample coverage + 0.30 * score gap` against the next location, rounded to three decimals.

**Orchestrate.** A strategy YAML names sources, venues, candidate locations, an event, market discovery, deploy policy, and a container image. `strategy execute` without `--apply` or `--plan-only` prints the following command sequence. With either flag it runs that sequence in process: resolve markets, write generated target/runner/placement files, probe, upload JSONL to S3, score a placement, resolve an ECR image, write `runtime.json` and OpenTofu variables, then `tofu init` and either `tofu plan` or `tofu apply`. `--apply` continues through SSM readiness, an optional execution handoff, and an optional teardown.

**Schedule.** `scheduler run --event-calendar` loads a strategy that has no `event_time` yet, fills the next calendar entry, and emits market-refresh and warmup actions from offsets such as `T-7d` and `T-30m`. Events whose release time is more than four hours in the past are skipped. Checked-in calendars cover 2026 EIA WNGSR, WPSR, CPI, NFP, and FOMC dates used by the strategy files.

**Guard.** Container image checks accept only `account.dkr.ecr.region.amazonaws.com/repo:tag` and reject the tag `latest` unless `--allow-latest` is set. `strategy infra-plan` and `strategy tfvars` use that rule. Live mode (`execution.mode: live`) is denied on `--apply` unless `--allow-live` is set, `deployment.live_approved` is true, release capital is within `--live-capital-limit-usd`, the kill-switch file is absent, and the image tag is immutable. Every checked-in strategy uses `mode: paper`.

## Architecture

```text
strategy YAML + calendar
        |
        v
in-process HTTP / WebSocket probes  --->  JSONL
        |                                    |
        |                                    v
        |                              report + weighted placement
        v
policy (samples, confidence, margin, cutoff)
        |
        v
runtime.json + OpenTofu tfvars
        |
        v
ephemeral EC2 (Docker, host network) --SSM or host timer--> NullOne
        |
        v
teardown of that instance; S3 results stay
```

Two facts about where probes run:

- With no `--runners` file, every sample is labeled `provider=local`, `region=local`.
- A runners file or a strategy's `candidate_locations` list changes that label. The sockets are still opened by the process that launched the CLI. `strategy execute --apply` probes once per candidate region from the orchestrator host and stamps each region onto those local samples. Comparing regions means running the CLI on a host in each region and combining the JSONL.

`infra/aws` is OpenTofu. It can create an Amazon Linux 2023 probe runner, a versioned and encrypted S3 results bucket with public access blocked and `prevent_destroy`, and one ephemeral strategy runtime. Runner user-data installs Python 3.11, git, and the AWS CLI, writes `/opt/source-latency-profiler/runner.env`, and stops. It does not copy this repo onto the instance or start probes. Strategy-runtime user-data pulls `runtime.json` from S3, starts the configured container with `--network host`, applies a short sysctl file, and installs `nullone-health`, `nullone-ready`, `nullone-execute`, and `nullone-shutdown` wrappers.

`infra/azure/README.md` and `infra/gcp/README.md` are placeholders. There is no Azure or GCP module in this tree.

Execution handoff on the runtime host has three modes, implemented in `profiler/execution_handoff.py`:

| Mode | Behavior | Default |
| --- | --- | --- |
| `ssm-immediate` | SSM runs the execute command when the orchestrator reaches that step | `strategy execute` |
| `ssm-lead` | SSM is sent `execution_lead_seconds` before `execute_at` (default 60) | optional |
| `host-timer` | A host script waits locally, then runs execute | `scheduler run` |

`profiler/latency.py` reads a NullOne JSONL trace and reports gaps between `execute_start`, `source_fetch_start` / `source_fetch_done`, `order_submit_start`, and `order_submit_done`. That summary exists only after a diagnostic run produces a trace.

## Layout

| Path | Role |
| --- | --- |
| `profiler/` | CLI, probes, report, recommendation, policy, orchestrator, scheduler, secrets, ECR image checks, S3 storage |
| `profiler/markets/` | Pinned brackets, or a subprocess call to `nullone discover` and import of the spec it writes |
| `targets/` | Explicit probe lists. `targets/examples.yaml` is `https://example.com/` |
| `runners/` | Labels for AWS regions. They do not open a remote session |
| `strategies/` | NullOne event profiles. Production files omit `event_time` until a calendar is applied |
| `strategies/fixtures/` | Pinned brackets and `event_time`, so they load with no network and no NullOne binary |
| `calendars/` | JSON release calendars for 2026 |
| `infra/aws/` | OpenTofu for the results bucket, probe runner, and strategy runtime |
| `scripts/` | Orchestrator host bootstrap, systemd installers, janitor and diagnostic units, teardown helper |
| `tests/` | Unit tests. No cloud account required |
| `docs/` | System overview, execution handoff, engineering review, production runbooks, NullOne S3 release contract |
| `PRD.md` | Product requirements as drafted on 2026-05-24 |

`secrets plan` reads an env file and a PEM path and prints only redacted values. `secrets sync` writes AWS Secrets Manager entries. Nothing in this snapshot is a live credential.

## Run it locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,ws]"
```

Add the `aws` extra (`boto3`) before any S3, Secrets Manager, ECR, or EC2 command. `strategy execute --apply` also needs OpenTofu on `PATH`, AWS credentials, and a NullOne binary. NullOne is a separate project and is not vendored here. This snapshot also omits the private CI workflows: one checked out the private NullOne repo, and the other assumed an AWS role and sent SSM commands using repository secrets.

Probe the bundled example target and print a report:

```bash
python -m profiler.cli probe \
  --targets targets/examples.yaml \
  --samples 1 \
  --output results/example.jsonl

python -m profiler.cli report --input results/example.jsonl
```

`results/` is gitignored.

Load a pinned strategy with no AWS calls and no NullOne binary:

```bash
python -m profiler.cli strategy plan \
  --strategy strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml
```

`strategies/nullone-wngsr-kalshi.yaml` and the other production profiles are templates. `strategy plan` rejects them because `event_time` is missing. The scheduler supplies that field from a calendar:

```bash
python -m profiler.cli scheduler run \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --event-calendar calendars/kalshi-wngsr-2026.json \
  --once \
  --dry-run
```

Check an ECR image string locally, with no AWS API call:

```bash
python -m profiler.cli artifacts validate \
  --image 123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:run-123
```

Run the tests:

```bash
python -m pytest
```

Other entry points: `recommend`, `collect` (S3 download), `artifacts resolve-ecr`, `artifacts cleanup-plan`, `secrets`, `infra plan|apply|destroy|preflight`, and `strategy` subcommands `profile`, `recommend`, `infra-plan`, `runtime-config`, `tfvars`, `execute`, `deploy`, `teardown`, `janitor`, and `import-markets`. `infra plan`, `infra apply`, and `infra destroy` print OpenTofu commands. They do not invoke OpenTofu themselves. `strategy deploy` prints a `tofu apply` line when the policy decision is `deploy`, `deploy_fallback`, or `deploy_standbys`.

Image URIs in `strategies/` use account `123456789012`, the account id from the AWS documentation examples. Replace it before pointing a strategy at a real registry. The same placeholder appears in `docs/nullone-s3-release-spec.md`.

## Example output

The following was produced on 2026-10-23 from this tree with one sample of `targets/examples.yaml`. The timings are that run, from this network path to `https://example.com/`. They are not a benchmark.

```text
Wrote 1 results to results/example.jsonl
target	provider	region	count	success	p50	p95	p99	jitter
example_http	local	local	1	100.00%	84.08	84.08	84.08	0.00
```

The JSONL record, pretty-printed:

```json
{
  "target": "example_http",
  "target_type": "http",
  "provider": "local",
  "region": "local",
  "runner_id": "local",
  "timestamp": "2026-10-23T05:33:29.757477Z",
  "dns_ms": 2.937,
  "tcp_connect_ms": 0.42,
  "tls_handshake_ms": 8.415,
  "tls_protocol": "TLSv1.3",
  "ttfb_ms": 58.15,
  "download_ms": 0.115,
  "total_ms": 84.077,
  "status_code": 200,
  "response_bytes": 589,
  "server_header": "cloudflare",
  "cdn_headers": {"cf-cache-status": "HIT", "cf-ray": "a4ee5e057d192081-IAD"},
  "remote_ip": "104.20.23.154",
  "resolved_ips": ["104.20.23.154", "172.66.147.243", "2606:4700:10::ac42:93f3", "2606:4700:10::6814:179a"],
  "error": null
}
```

`strategy plan` on the pinned WNGSR fixture:

```text
strategy: nullone_wngsr_kalshi
event_id: eia_wngsr_2026-05-15
event_time: 2026-05-22T10:30:00-04:00
market_brackets: 3 (pinned)
sources: 2
venues: 2
candidate_runners: 1
auto_deploy: True
minimum_confidence: 0.85
profiler_blackout: T-2m
```

`artifacts validate` on the sample image URI:

```json
{
  "account": "123456789012",
  "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:run-123",
  "immutable": true,
  "region": "us-east-1",
  "repository": "nullone",
  "repository_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone",
  "tag": "run-123",
  "type": "container"
}
```

## Further reading

- `docs/system-overview.md` — orchestrator pipeline and the NullOne boundary
- `docs/execution-handoff.md` — `ssm-immediate`, `ssm-lead`, and `host-timer`
- `docs/engineering-review.md` — which latency ideas are implemented in this repo
- `docs/production_runbooks.md` — host commands for a deployed orchestrator
- `docs/nullone-s3-release-spec.md` — immutable S3 keys for the NullOne binary
- `infra/aws/README.md` — bucket, runner, and strategy runtime
- `PRD.md` — original scope, including items that are still only specified

## History

Built by Cameron Gifford with Cursor in May–June 2026.
In October 2026 the code was cleaned up for public release and the documentation was rewritten.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Cameron Gifford.
