# Commands

Install, probe, and the other `latency-profiler` entry points. None of the examples below need a cloud account except where a command is named as an AWS call.

Version 0.1.0. Python 3.11+. The console script is `latency-profiler` (`profiler.cli:main`).

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,ws]"
```

Add the `aws` extra (`boto3`) before any S3, Secrets Manager, ECR, or EC2 command. `strategy execute --apply` also needs OpenTofu on `PATH`, AWS credentials, and a NullOne binary. NullOne is a separate project and is not vendored here. The public CI workflow runs `python -m pytest tests` on Python 3.12 with the `dev` extra only. It does not check out NullOne, assume an AWS role, or use repository secrets.

## Probe and report

Probe the bundled example target and print a report:

```bash
python -m profiler.cli probe \
  --targets targets/examples.yaml \
  --samples 1 \
  --output results/example.jsonl

python -m profiler.cli report --input results/example.jsonl
```

`results/` is gitignored.

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

## Strategy plan

Load a pinned strategy with no AWS calls and no NullOne binary:

```bash
python -m profiler.cli strategy plan \
  --strategy strategies/fixtures/nullone-wngsr-kalshi-pinned.yaml
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

`strategies/nullone-wngsr-kalshi.yaml` and the other production profiles are templates. `strategy plan` rejects them because `event_time` is missing. The scheduler supplies that field from a calendar.

## Scheduler

```bash
python -m profiler.cli scheduler run \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --event-calendar calendars/kalshi-wngsr-2026.json \
  --once \
  --dry-run
```

## Image check

Check an ECR image string locally, with no AWS API call:

```bash
python -m profiler.cli artifacts validate \
  --image 123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:run-123
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

Image URIs in `strategies/` use account `123456789012`, the account id from the AWS documentation examples. Replace it before pointing a strategy at a real registry. The same placeholder appears in `docs/nullone-s3-release-spec.md`.

## Tests

The unit tests do not need a cloud account, network access to a venue, or repository secrets:

```bash
python -m pytest tests
```

## Other entry points

`recommend`, `collect` (S3 download), `artifacts resolve-ecr`, `artifacts cleanup-plan`, `secrets`, `infra plan|apply|destroy|preflight`, and `strategy` subcommands `profile`, `recommend`, `infra-plan`, `runtime-config`, `tfvars`, `execute`, `deploy`, `teardown`, `janitor`, and `import-markets`. `infra plan`, `infra apply`, and `infra destroy` print OpenTofu commands. They do not invoke OpenTofu themselves. `strategy deploy` prints a `tofu apply` line when the policy decision is `deploy`, `deploy_fallback`, or `deploy_standbys`.

`secrets plan` reads an env file and a PEM path and prints only redacted values. `secrets sync` writes AWS Secrets Manager entries. Nothing in this snapshot is a live credential.
