# PRD: Source Latency Profiler and Strategy Deployment Orchestrator

**Status:** Draft (v2)  
**Proposed repo:** `source-latency-profiler`  
**Author:** NullOne team  
**Last updated:** 2026-05-24

---

## 1. Overview

### 1.1 Problem

Latency-sensitive systems depend on external information sources and execution venues, but deployment decisions are often made with incomplete network data.

For NullOne, the critical path includes:

- EIA WNGSR JSON/CSV publication endpoints
- Kalshi REST APIs
- Kalshi WebSocket orderbook feeds
- Kalshi order and batch order endpoints
- Optional Polymarket APIs

The user-facing question is often phrased as "where are the servers?", but modern APIs are frequently behind CDNs, load balancers, Anycast, regional routing, and cloud edge networks. The more actionable question is:

> From each possible deployment location, which source or venue has the lowest median latency, lowest tail latency, lowest jitter, and highest reliability?

### 1.2 Proposal

Build a standalone **Source Latency Profiler** that measures endpoint latency from multiple cloud providers and regions using repeatable infrastructure-as-code deployments, then evolve it into a **Strategy Deployment Orchestrator** that can automatically select, deploy, warm, execute, and teardown ephemeral strategy infrastructure for scheduled events.

The tool should:

1. Define public or authenticated data sources in a target config file.
2. Deploy identical probe runners across selected AWS, Azure, GCP, and optional local/on-prem regions.
3. Measure DNS, TCP, TLS, HTTP, WebSocket, and optional trace metrics.
4. Store normalized JSONL results centrally.
5. Produce reports ranking provider/region choices by p50, p95, p99, jitter, failure rate, and freshness.
6. Support event-window burst probing for scheduled releases.
7. Produce machine-readable placement recommendations that downstream systems can use for warmup or deployment decisions.
8. Define strategy profiles that combine sources, venues, candidate locations, infrastructure requirements, timing windows, and automation policy.
9. Run a scheduled warmup state machine that profiles candidate locations, selects infrastructure, deploys the strategy runtime, enters a profiler blackout window, executes, and tears down ephemeral resources.
10. Preserve measurement and decision telemetry for post-event audit and model improvement.

### 1.3 Why Separate Repo

This should be a separate repository from NullOne because it is general infrastructure, measurement, and deployment orchestration tooling, not trading strategy logic.

The profiler can include a `targets/nullone.yaml` target pack and strategy profiles for NullOne, but it should not import NullOne trading logic. Deployment integration should happen through explicit artifacts such as container images, startup commands, environment variables, or infrastructure variables.

### 1.4 Success Metrics

| Metric | Target |
|--------|--------|
| Region comparison | Rank at least 3 AWS regions for EIA + Kalshi targets |
| Multi-cloud comparison | Support AWS and Azure in v1; GCP in v2 |
| Measurement completeness | Capture DNS, TCP connect, TLS handshake, TTFB, total response, status/error |
| Repeatability | One command can deploy probes, run measurement, collect results, and destroy infra |
| Safety | Explicit targets only; no port scanning; low-rate defaults |
| NullOne utility | Produce a concrete deployment recommendation for NullOne region/provider |
| Warmup automation | Run profiling, deployment, warmup, blackout, execution handoff, and teardown for a scheduled strategy event |
| Auditability | Persist profiler results, recommendations, deployment decisions, and orchestration state transitions |

---

## 2. Product Scope

### 2.1 In Scope

- HTTP/HTTPS endpoint profiling
- WebSocket connect/subscription/first-message profiling
- DNS resolution timing and resolved IP capture
- TLS handshake timing
- TTFB and full response timing
- Redirect chain recording
- Basic response validation
- Cloud-region probe runners
- Central result collection
- Report generation and region ranking
- Event-window burst mode
- Dynamic placement recommendations for source/venue pairs
- Strategy profiles that generate targets, placement profiles, runner plans, and deployment variables
- Scheduled warmup orchestration for strategy events
- Policy-gated automatic deployment of ephemeral strategy infrastructure
- Profiler blackout before the execution window
- Strategy runtime warmup hooks and execution handoff hooks
- Post-execution teardown of ephemeral compute while preserving telemetry
- NullOne target pack

### 2.2 Out of Scope

- Port scanning
- Unauthorized endpoint discovery
- High-rate scraping
- Packet capture of third-party traffic
- Bypassing rate limits, WAFs, or geo controls
- Exchange trading logic
- Secret-heavy authenticated trading probes in v1
- Fully managed long-running monitoring service in v1
- Autonomous trading decisions or capital allocation
- Executing orders from the profiler/orchestrator itself
- Circumventing exchange controls, throttles, or compliance rules
- Guaranteeing sub-millisecond latency on public cloud or public internet paths

### 2.3 Users

- Latency-sensitive application developers
- Trading/event-driven system operators
- Infrastructure engineers selecting deployment regions
- Researchers comparing information-source performance across networks

---

## 3. Target Model

### 3.1 Target Config

Targets are declared explicitly in YAML:

```yaml
targets:
  - name: eia_wngsr_json
    type: http
    url: https://ir.eia.gov/ngs/wngsr.json
    method: GET
    expected_status: 200
    timeout_ms: 2000

  - name: eia_wngsr_csv
    type: http
    url: https://ir.eia.gov/ngs/wngsr.csv
    method: GET
    expected_status: 200
    timeout_ms: 2000

  - name: kalshi_exchange_status
    type: http
    url: https://external-api.kalshi.com/trade-api/v2/exchange/status
    method: GET
    expected_status: 200
    timeout_ms: 2000

  - name: kalshi_ws
    type: websocket
    url: wss://external-api-ws.kalshi.com/trade-api/ws/v2
    timeout_ms: 3000
```

### 3.2 Runner Config

Probe locations are declared separately:

```yaml
runners:
  - provider: aws
    region: us-east-1
    runtime: vm

  - provider: aws
    region: us-east-2
    runtime: vm

  - provider: azure
    region: eastus
    runtime: vm
```

### 3.3 Probe Modes

| Mode | Purpose |
|------|---------|
| `once` | One-shot comparison from every runner |
| `baseline` | Low-rate periodic measurements over hours/days |
| `burst` | High-resolution measurements around scheduled event windows |
| `trace` | Add traceroute/MTR-style path data where available |
| `ws` | WebSocket-specific connect/subscription/first-message profiling |
| `warmup` | Pre-flight profiling before a trading system starts or enters an event window |

### 3.4 Placement Profiles

Placement profiles define how to score multiple targets together for a specific operational objective.

For example, a NullOne WNGSR/Kalshi profile may weight:

- EIA JSON/CSV first-byte latency and freshness
- Kalshi REST status and order-path latency
- Kalshi WebSocket connect, subscription, and first-message latency
- p95/p99 tail latency
- jitter
- recent failure rate
- DNS and remote IP stability

```yaml
profile:
  name: nullone_wngsr_kalshi
  objective: source_to_venue
  source_targets:
    - eia_wngsr_json
    - eia_wngsr_csv
  venue_targets:
    - kalshi_status
    - kalshi_ws
  weights:
    source_ttfb_p50: 0.25
    source_ttfb_p95: 0.20
    venue_total_p50: 0.20
    venue_total_p95: 0.20
    jitter: 0.10
    failure_rate: 0.05
  minimum_samples: 30
  lookback: 10m
```

The profiler should report both the best location and close alternates. It should include enough scoring detail for operators to understand whether a recommendation is decisive or marginal.

### 3.5 Strategy Profiles

Strategy profiles are the top-level operator-facing configuration. A strategy profile should describe:

- the scheduled event or execution window
- event metadata, market mappings, brackets, and execution parameters that would otherwise live inside NullOne
- source targets and venue targets
- candidate cloud providers, regions, and instance families
- scoring weights and minimum confidence requirements
- warmup, profiler blackout, execution, and teardown timing
- deployment artifact and runtime requirements
- fallback behavior when confidence is low or deployment fails

The strategy profile should be the canonical trading/event profile. NullOne should receive a generated runtime config and should not own separate strategy YAML. The profile should generate or reference the lower-level target config, runner config, placement profile, deployment variables, and NullOne runtime config. Operators should not need to hand-maintain duplicate YAML across repositories.

Example:

```yaml
schema_version: 1

strategy:
  name: nullone_wngsr_kalshi
  description: Trade Kalshi WNGSR markets using EIA release data.
  event_id: eia_wngsr_2026-05-15
  release_at_et: "2026-05-22T10:30:00-04:00"
  event_time: "2026-05-22T10:30:00-04:00"
  mode: paper

event:
  source: eia_wngsr
  week_ending: "2026-05-15"
  metric: net_change_bcf
  region: lower48

markets:
  brackets:
    - label: "96-100"
      min: 96
      max: 100
      kalshi:
        market_ticker: "KXGASSTORAGE-MAY15-98"
      polymarket:
        token_id: "1234567890"
        tick_size: "0.01"
        neg_risk: false
    - label: "101-105"
      min: 101
      max: 105
      kalshi:
        market_ticker: "KXGASSTORAGE-MAY15-103"
      polymarket:
        token_id: "0987654321"
        tick_size: "0.01"
        neg_risk: false

execution:
  min_edge_after_fees: 0.05
  max_entry_price: 0.92
  release_capital_usd: 100
  execution_strategy: single_leg
  mode: paper

targets:
  sources:
    - name: eia_wngsr_json
      type: http
      url: https://ir.eia.gov/ngs/wngsr.json
      expected_status: 200
      timeout_ms: 2000
    - name: eia_wngsr_csv
      type: http
      url: https://ir.eia.gov/ngs/wngsr.csv
      expected_status: 200
      timeout_ms: 2000
  venues:
    - name: kalshi_status
      type: http
      url: https://external-api.kalshi.com/trade-api/v2/exchange/status
      expected_status: 200
      timeout_ms: 2000
    - name: kalshi_ws
      type: websocket
      url: wss://external-api-ws.kalshi.com/trade-api/ws/v2
      timeout_ms: 3000

candidate_locations:
  aws:
    regions:
      - us-east-1
      - us-east-2
      - us-west-2
    instance_types:
      - c7i.large
      - c7i.xlarge
      - m7i.large
  azure:
    regions:
      - eastus
      - eastus2

objective:
  weights:
    source_ttfb_p50: 0.25
    source_ttfb_p95: 0.20
    venue_total_p50: 0.20
    venue_total_p95: 0.20
    jitter: 0.10
    failure_rate: 0.05
  minimum_samples: 100
  minimum_confidence: 0.85
  minimum_score_margin_pct: 15
  lookback: 10m

automation:
  allow_auto_deploy: true
  warmup_starts_at: "T-30m"
  profiler_stops_at: "T-2m"
  latest_deploy_cutoff: "T-3m"
  execution_starts_at: "T+0"
  auto_teardown: true
  fallback:
    mode: deploy_default_region
    provider: aws
    region: us-east-1

deployment:
  runtime: ec2
  engine: nullone
  artifact:
    type: container
    image: "<account>.dkr.ecr.us-east-1.amazonaws.com/nullone:latest"
  startup_command: "nullone ready --config /etc/nullone/runtime.json"
  execution_command: "nullone execute --config /etc/nullone/runtime.json"
  secrets:
    source: aws_secrets_manager
    names:
      - nullone/kalshi/read_only
      - nullone/kalshi/trading
```

### 3.6 Orchestration State Machine

Scheduled strategy runs should use an explicit state machine:

| State | Purpose |
|-------|---------|
| `pending` | Strategy event is registered but warmup has not started |
| `profiling` | Candidate locations are being measured |
| `scoring` | Results are converted into placement and infrastructure recommendations |
| `deploying` | Ephemeral strategy infrastructure is being created |
| `strategy_warming` | Strategy runtime starts, loads state, opens allowed connections, and performs readiness checks |
| `profiler_blackout` | Profiler and telemetry-heavy jobs are stopped before the hot execution window |
| `execution_ready` | Strategy runtime is warmed and waiting for the event |
| `executing` | Strategy runtime owns the hot path |
| `tearing_down` | Ephemeral compute is destroyed after execution or abort |
| `done` | Run completed and telemetry is retained |
| `failed` | Run failed with an explicit reason and teardown policy |

The orchestrator should persist every state transition, timestamp, decision input, recommendation, deployment artifact, and teardown result to the result store.

---

## 4. Measurement Requirements

### 4.1 HTTP Metrics

Each HTTP probe should capture:

- `dns_ms`
- `tcp_connect_ms`
- `tls_handshake_ms`
- `ttfb_ms`
- `download_ms`
- `total_ms`
- `status_code`
- `response_bytes`
- `redirect_count`
- `redirect_chain`
- `resolved_ips`
- `remote_ip`
- `tls_protocol`
- `server_header`
- `cdn_headers` where visible
- `error`

### 4.2 WebSocket Metrics

Each WebSocket probe should capture:

- DNS timing
- TCP connect timing
- TLS handshake timing
- WebSocket upgrade timing
- optional auth timing
- optional subscription send timing
- first message latency
- close code/error

### 4.3 DNS Metrics

DNS should capture:

- resolver used
- query duration
- record type
- returned records
- TTL if available
- DNS errors

### 4.4 Trace Metrics

Trace mode should be optional because it may require elevated permissions or provider-specific setup.

Trace output should include:

- hop count
- per-hop latency where available
- timeout hops
- ASN/provider enrichment if available

Trace data is diagnostic only. Ranking should primarily use application-level timings.

---

## 5. Infrastructure

### 5.1 IaC Tooling

Use **OpenTofu/Terraform** for infrastructure.

Rationale:

- Mature support for AWS, Azure, and GCP
- Repeatable region deployments
- Easy teardown
- Clear provider-specific modules
- Portable for users who already know Terraform-style workflows

### 5.2 Cloud Runners

V1 should use tiny VM instances rather than serverless functions.

Reasons:

- More consistent networking behavior
- Easier WebSocket probes
- Easier traceroute/MTR support
- Easier custom binaries
- Fewer cold-start artifacts

Serverless runners may be added later for cheaper broad sampling.

### 5.3 Initial Cloud Support

| Provider | V1 | Notes |
|----------|----|-------|
| AWS | Yes | Primary target; start with EC2 tiny instances |
| Azure | Yes | Compare against AWS East regions |
| GCP | Later | Add once runner abstraction stabilizes |
| Local | Yes | Run from laptop or existing EC2 without IaC |

### 5.4 Result Collection

V1 result collection should use a persistent AWS S3 bucket as the primary durable store for cloud-runner output:

- Runner writes local JSONL.
- Runner uploads JSONL objects to the configured S3 results bucket.
- Control machine can also pull results over SSH for debugging or local-only runs.
- Reports can read from local files or from the S3 bucket.

The AWS infrastructure module should be able to create the S3 results bucket if it does not already exist. The bucket is persistent project infrastructure, not ephemeral test-runner infrastructure.

Teardown behavior:

- `latency-profiler infra destroy` should destroy runner instances, security groups, temporary IAM roles, and other ephemeral probe resources.
- The S3 results bucket should remain after runner teardown by default.
- Bucket deletion should require an explicit destructive command or Terraform/OpenTofu variable, such as `destroy_results_bucket=true`.
- Result objects should be partitioned by profile/run/provider/region/date to support historical analysis.

Suggested S3 key shape:

```text
s3://<results-bucket>/runs/<run_id>/provider=<provider>/region=<region>/target=<target>/<timestamp>.jsonl
```

V2 can add:

- SQLite/DuckDB report database
- Scheduled recurring probes
- Dashboard/export
- Additional result sinks such as Azure Blob or GCS

### 5.5 Strategy Runtime Infrastructure

The orchestrator should support ephemeral strategy runtime infrastructure separate from probe-runner infrastructure.

Initial AWS runtime support should include:

- EC2 instance or Auto Scaling group with one desired instance
- security group with least-privilege egress for configured sources, venues, package registries, and telemetry sinks
- IAM role for reading required secrets and writing orchestration telemetry
- optional ECR image pull permissions
- user data or SSM command to start the strategy warmup command
- tags tying resources to `strategy_name`, `run_id`, `event_time`, and `orchestration_state`

The strategy runtime should be disposable. It should be created during warmup and torn down after execution or abort. Persistent storage should be avoided unless explicitly required by the strategy profile.

### 5.6 Host Scheduler

A long-lived host scheduler should run on a trusted control host, such as an EC2 instance, a local operator machine, or a small always-on VM.

Responsibilities:

- read scheduled strategy profiles
- compute warmup start times from event datetimes
- invoke profiling and placement workflows
- apply safety policy gates before deployment
- deploy selected strategy infrastructure
- start strategy warmup
- enforce profiler blackout
- hand off to execution command
- teardown ephemeral infrastructure
- persist audit logs and state transitions

The host scheduler should not be on the hot execution path after `profiler_blackout`. Once the strategy runtime is warmed and execution-ready, scheduler work should be limited to coarse state observation and post-execution teardown.

### 5.7 Secret Management

The control plane should manage NullOne runtime secrets through AWS Secrets Manager rather than requiring operators to copy `.env` or `.pem` files onto strategy hosts.

Initial secret layout:

```text
nullone/<env>/runtime/env
nullone/<env>/kalshi/private_key_pem
```

The `runtime/env` secret should contain parsed NullOne environment variables as JSON. The private key secret should contain PEM text. The strategy runtime host should fetch these secrets during boot, write `/run/secrets/nullone.env`, write `/run/secrets/kalshi-private-key.pem` with `0600`, and pass them to the NullOne process or container.

---

## 6. CLI Requirements

### 6.1 Local Commands

```bash
latency-profiler probe --targets targets/nullone.yaml --once
latency-profiler report --input results/*.jsonl
```

### 6.2 Multi-Region Workflow

```bash
latency-profiler infra plan --runners runners/aws-east.yaml
latency-profiler infra apply --runners runners/aws-east.yaml
latency-profiler run --targets targets/nullone.yaml --runners runners/aws-east.yaml --duration 10m
latency-profiler collect --from-s3 --output results/nullone-aws-east.jsonl
latency-profiler report --input s3://source-latency-profiler-results/runs/<run_id>/
latency-profiler infra destroy --runners runners/aws-east.yaml
```

By default, destroying runner infrastructure should not delete the S3 results bucket or its historical measurements.

### 6.3 Event Burst Workflow

```bash
latency-profiler burst \
  --targets targets/nullone.yaml \
  --runners runners/aws-azure-east.yaml \
  --start-at 2026-05-28T10:29:30-04:00 \
  --duration 120s \
  --interval-ms 250
```

### 6.4 Placement Recommendation Workflow

```bash
latency-profiler recommend \
  --targets targets/nullone.yaml \
  --profile .generated/nullone_wngsr_kalshi/<run_id>/placement-profile.yaml \
  --runners runners/aws-azure-east.yaml \
  --duration 10m \
  --output recommendations/nullone-placement.json
```

The recommendation command should support two operating styles:

- `warmup`: run shortly before a trading system starts or before a scheduled event window.
- `baseline`: run periodically to detect route drift and update placement recommendations over time.

The profiler may emit recommendations to local files in v1. Later versions may publish to object storage, Redis, HTTP webhooks, or other integration points.

### 6.5 Strategy Orchestration Workflow

One-shot scheduled execution:

```bash
latency-profiler strategy execute \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --run-id wngsr-2026-05-28
```

Stepwise operator-controlled workflow:

```bash
latency-profiler strategy plan \
  --strategy strategies/nullone-wngsr-kalshi.yaml

latency-profiler strategy profile \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --run-id wngsr-2026-05-28

latency-profiler strategy recommend \
  --strategy strategies/nullone-wngsr-kalshi.yaml \
  --run-id wngsr-2026-05-28

latency-profiler strategy infra-plan \
  --recommendation recommendations/wngsr-2026-05-28.json

latency-profiler strategy deploy \
  --recommendation recommendations/wngsr-2026-05-28.json

latency-profiler strategy teardown \
  --run-id wngsr-2026-05-28
```

Host scheduler workflow:

```bash
latency-profiler scheduler run \
  --strategies strategies/*.yaml \
  --state-store s3://source-latency-profiler-results/orchestrator/
```

Secret workflow:

```bash
latency-profiler secrets plan \
  --env-file ../NullOne/.env \
  --pem-file ../NullOne/kalshi-private-key.pem \
  --prefix nullone/prod

latency-profiler secrets sync \
  --env-file ../NullOne/.env \
  --pem-file ../NullOne/kalshi-private-key.pem \
  --prefix nullone/prod \
  --yes

latency-profiler secrets validate --prefix nullone/prod
```

The `strategy execute` command may perform automatic deployment only when:

- `automation.allow_auto_deploy` is true
- minimum sample thresholds are met
- minimum confidence is met
- minimum score margin is met
- current time is before `latest_deploy_cutoff`
- the selected deployment target satisfies all strategy runtime requirements

### 6.6 Profiler Blackout Requirements

The orchestrator must stop profiler activity before the execution window.

Default policy:

- profiler stops no later than `T-2m`
- no burst probes during blackout
- no S3 uploads from the execution host during blackout unless explicitly marked low-priority and async
- no report generation on the execution host during blackout
- strategy runtime receives final selected endpoints, resolved IPs, and placement metadata before blackout

The blackout phase is designed to prevent profiling telemetry, logging, DNS lookups, socket churn, garbage collection, or API calls from competing with the execution loop.

---

## 7. Result Schema

Each probe result should be one JSON object per line:

```json
{
  "schema_version": 1,
  "target": "eia_wngsr_json",
  "target_type": "http",
  "provider": "aws",
  "region": "us-east-1",
  "runner_id": "aws-us-east-1-a",
  "timestamp": "2026-05-24T21:00:00.123Z",
  "dns_ms": 2.4,
  "tcp_connect_ms": 7.8,
  "tls_handshake_ms": 12.6,
  "ttfb_ms": 24.1,
  "download_ms": 1.8,
  "total_ms": 27.0,
  "status_code": 200,
  "response_bytes": 2048,
  "resolved_ips": ["203.0.113.10"],
  "remote_ip": "203.0.113.10",
  "error": null
}
```

### 7.1 Report Metrics

Reports should compute by target/provider/region:

- count
- success rate
- p50
- p90
- p95
- p99
- min/max
- standard deviation
- median absolute deviation
- DNS variance
- remote IP distribution
- winner by target
- winner by weighted portfolio

### 7.2 Recommendation Schema

Placement recommendations should be emitted as machine-readable JSON:

```json
{
  "schema_version": 1,
  "profile": "nullone_wngsr_kalshi",
  "recommended_location": {
    "provider": "aws",
    "region": "us-east-1"
  },
  "confidence": 0.87,
  "score": 12.4,
  "rationale": [
    "Lowest combined EIA TTFB and Kalshi REST p95",
    "Stable WebSocket first-message latency",
    "No probe failures in the lookback window"
  ],
  "alternates": [
    {
      "provider": "aws",
      "region": "us-east-2",
      "score": 15.1
    },
    {
      "provider": "azure",
      "region": "eastus",
      "score": 18.8
    }
  ],
  "sample_window": {
    "started_at": "2026-05-24T21:50:00Z",
    "ended_at": "2026-05-24T22:00:00Z",
    "minimum_samples_met": true
  },
  "measured_at": "2026-05-24T22:00:00Z"
}
```

Recommendations should be conservative. A location should not be marked as high-confidence unless it has enough recent successful samples, stable tail latency, and a meaningful score advantage over alternates.

The placement advisor should not itself execute strategy logic. It should produce placement intelligence and infrastructure recommendations that the policy-gated orchestrator can consume.

### 7.3 Infrastructure Recommendation Schema

The orchestrator should convert placement recommendations into deployable infrastructure recommendations:

```json
{
  "schema_version": 1,
  "strategy": "nullone_wngsr_kalshi",
  "run_id": "wngsr-2026-05-28",
  "event_time": "2026-05-28T10:30:00-04:00",
  "recommended_infrastructure": {
    "provider": "aws",
    "region": "us-east-1",
    "runtime": "ec2",
    "instance_type": "c7i.large",
    "network": {
      "public_ip": false,
      "placement": "single_az_low_latency"
    },
    "artifact": {
      "type": "container",
      "image": "<account>.dkr.ecr.us-east-1.amazonaws.com/nullone:latest"
    }
  },
  "confidence": 0.89,
  "policy_decision": {
    "auto_deploy_allowed": true,
    "minimum_samples_met": true,
    "minimum_confidence_met": true,
    "minimum_score_margin_met": true,
    "before_latest_deploy_cutoff": true,
    "decision": "deploy"
  },
  "fallback": {
    "mode": "deploy_default_region",
    "provider": "aws",
    "region": "us-east-1"
  },
  "generated_at": "2026-05-28T10:24:30-04:00"
}
```

### 7.4 NullOne Runtime Config Schema

Before deployment, the orchestrator should generate a runtime config that NullOne consumes through `nullone ready --config` and `nullone execute --config`.

```json
{
  "schema_version": 1,
  "engine": "nullone",
  "strategy": {
    "name": "nullone_wngsr_kalshi",
    "event_id": "eia_wngsr_2026-05-15",
    "release_at_et": "2026-05-22T10:30:00-04:00",
    "mode": "paper"
  },
  "event": {
    "source": "eia_wngsr",
    "week_ending": "2026-05-15",
    "metric": "net_change_bcf",
    "region": "lower48"
  },
  "markets": {
    "brackets": [
      {
        "label": "96-100",
        "min": 96,
        "max": 100,
        "kalshi": {
          "market_ticker": "KXGASSTORAGE-MAY15-98"
        },
        "polymarket": {
          "token_id": "1234567890",
          "tick_size": "0.01",
          "neg_risk": false
        }
      }
    ]
  },
  "execution": {
    "min_edge_after_fees": 0.05,
    "max_entry_price": 0.92,
    "release_capital_usd": 100,
    "execution_strategy": "single_leg",
    "mode": "paper"
  },
  "placement": {
    "provider": "aws",
    "region": "us-east-1",
    "runtime": "ec2",
    "instance_type": "c7i.large"
  },
  "runtime": {
    "run_id": "wngsr-2026-05-22",
    "warmup_starts_at": "2026-05-22T10:00:00-04:00",
    "profiler_blackout_at": "2026-05-22T10:28:00-04:00",
    "execute_at": "2026-05-22T10:30:00-04:00"
  }
}
```

NullOne should treat this runtime config as its input contract. It should not need to parse profiler scoring weights, candidate cloud locations, OpenTofu variables, or orchestration policy.

### 7.5 Orchestration Event Schema

Every state transition should be emitted as JSONL:

```json
{
  "schema_version": 1,
  "event_type": "orchestration_state_transition",
  "strategy": "nullone_wngsr_kalshi",
  "run_id": "wngsr-2026-05-28",
  "from_state": "scoring",
  "to_state": "deploying",
  "timestamp": "2026-05-28T10:24:31.123Z",
  "decision": {
    "recommendation": "aws/us-east-1/c7i.large",
    "confidence": 0.89,
    "policy": "auto_deploy"
  },
  "artifacts": {
    "recommendation_uri": "s3://source-latency-profiler-results/orchestrator/wngsr-2026-05-28/recommendation.json",
    "infra_plan_uri": "s3://source-latency-profiler-results/orchestrator/wngsr-2026-05-28/infra-plan.json"
  },
  "error": null
}
```

The event stream should be sufficient to reconstruct what the orchestrator knew, decided, deployed, executed, and destroyed.

---

## 8. NullOne Target Pack

The profiler should ship with `targets/nullone.yaml`:

```yaml
targets:
  - name: eia_wngsr_json
    type: http
    url: https://ir.eia.gov/ngs/wngsr.json
    method: GET
    expected_status: 200

  - name: eia_wngsr_csv
    type: http
    url: https://ir.eia.gov/ngs/wngsr.csv
    method: GET
    expected_status: 200

  - name: kalshi_status
    type: http
    url: https://external-api.kalshi.com/trade-api/v2/exchange/status
    method: GET
    expected_status: 200

  - name: kalshi_ws
    type: websocket
    url: wss://external-api-ws.kalshi.com/trade-api/ws/v2
```

Authenticated Kalshi probes can be added later, but v1 should avoid private keys and write-capable credentials.

---

## 9. Safety and Compliance

### 9.1 Guardrails

- Probe only explicitly configured targets.
- Use low-rate defaults.
- Require an explicit `--burst` or `--interval-ms` acknowledgement for high-frequency probing.
- Require explicit `automation.allow_auto_deploy: true` before deploying strategy infrastructure.
- Require minimum confidence, minimum sample count, and score-margin gates before automatic deployment.
- Enforce `latest_deploy_cutoff` so infrastructure is not created too close to the event.
- Enforce `profiler_blackout` before the execution window.
- Preserve strategy execution as a separate runtime concern; the orchestrator starts and observes strategy commands but does not place orders.
- Do not scan ports or IP ranges.
- Do not evade rate limits or access controls.
- Do not store secrets in Terraform state.
- Redact headers and credentials from result logs.
- Use read-only/auth-free endpoints by default.
- For authenticated probes, require explicit read-only credentials unless a strategy profile intentionally permits trading-path warmup checks.
- Never print secret values in `secrets plan`, `secrets sync`, `strategy execute`, or Terraform variable generation.
- Do not upload raw `.env` or `.pem` files to S3.
- Pass secret ARNs and secret IDs to runtime infrastructure; fetch values only on the runtime host.
- Store orchestration audit logs and recommendations in durable object storage.
- Teardown ephemeral compute after execution, abort, or timeout unless a profile explicitly disables teardown.

### 9.2 Rate Defaults

Suggested defaults:

| Mode | Default |
|------|---------|
| `once` | 3 samples per target |
| `baseline` | 1 sample per minute |
| `burst` | 250ms minimum interval, explicit opt-in |
| `trace` | Manual only |

### 9.3 Automated Deployment Safety Policy

Default policy for automatic deployment:

| Gate | Default |
|------|---------|
| Auto-deploy opt-in | Required per strategy |
| Minimum confidence | `0.85` |
| Minimum samples | `100` across required source and venue targets |
| Minimum score margin | `15%` better than next alternate |
| Latest deploy cutoff | `T-3m` |
| Profiler blackout | `T-2m` |
| Fallback mode | Deploy known-good default or abort |
| Teardown | Enabled by default |

Automatic deployment should fail closed. If required gates are not met, the orchestrator should avoid deploying an untrusted recommendation and should use the configured fallback behavior.

---

## 10. Repository Structure

```text
source-latency-profiler/
  README.md
  pyproject.toml
  profiler/
    __init__.py
    cli.py
    config.py
    http_probe.py
    ws_probe.py
    dns_probe.py
    result.py
    report.py
    strategy.py
    orchestrator.py
    policy.py
    infra_profile.py
  targets/
    nullone.yaml
    examples.yaml
  strategies/
    nullone-wngsr-kalshi.yaml
  runners/
    aws-east.yaml
    aws-azure-east.yaml
  infra/
    aws/
      main.tf
      variables.tf
      outputs.tf
      results_bucket.tf
    azure/
      main.tf
      variables.tf
      outputs.tf
    gcp/
      main.tf
      variables.tf
      outputs.tf
  tests/
  results/
    .gitkeep
  recommendations/
    .gitkeep
```

---

## 11. Implementation Plan

### Phase 1 — Local Profiler

- [ ] Implement target YAML parser
- [ ] Implement HTTP timing probe
- [ ] Implement JSONL result schema
- [ ] Implement local CLI
- [ ] Implement report command
- [ ] Add NullOne target pack

### Phase 2 — WebSocket and DNS Detail

- [ ] Add WebSocket connect/first-message timing
- [ ] Add DNS timing and resolved IP capture
- [ ] Add redirect-chain capture
- [ ] Add basic CDN/header hints

### Phase 3 — AWS IaC

- [ ] Add AWS OpenTofu/Terraform module
- [ ] Add persistent S3 results bucket creation/import
- [ ] Deploy tiny EC2 runner per region
- [ ] Add runner upload to S3 results bucket
- [ ] Add SSH-based result collection for debugging/local fallback
- [ ] Ensure runner teardown preserves the S3 results bucket by default
- [ ] Compare `us-east-1`, `us-east-2`, and `us-west-2`

### Phase 4 — Azure and Multi-Cloud Reports

- [ ] Add Azure runner module
- [ ] Normalize provider/region metadata
- [ ] Rank AWS vs Azure regions for NullOne targets

### Phase 5 — Burst Mode

- [ ] Add scheduled burst runner
- [ ] Add interval controls
- [ ] Add safeguards for minimum interval and max duration
- [ ] Produce event-window latency report

### Phase 6 — Optional Enhancements

- [ ] GCP runner module
- [ ] Azure Blob and GCS result sinks
- [ ] DuckDB report backend
- [ ] ASN/provider enrichment
- [ ] Trace/MTR mode
- [ ] HTML report export

### Phase 7 — Dynamic Placement Advisor

- [ ] Add placement profile YAML schema
- [ ] Add weighted source/venue scoring
- [ ] Emit machine-readable placement recommendations
- [ ] Add confidence scoring and minimum sample thresholds
- [ ] Add warmup workflow before scheduled trading windows
- [ ] Add recurring recommendation mode for route drift detection
- [ ] Add optional recommendation sinks such as S3, Blob, Redis, or HTTP webhook

### Phase 8 — Strategy Profiles

- [ ] Add strategy profile YAML schema
- [ ] Generate target config from strategy sources and venues
- [ ] Generate runner matrix from candidate locations
- [ ] Generate placement profile from strategy objective
- [ ] Generate infrastructure variables from deployment requirements
- [ ] Add strategy validation command
- [ ] Add example `strategies/nullone-wngsr-kalshi.yaml`

### Phase 9 — Policy-Gated Strategy Orchestrator

- [ ] Implement orchestration state machine
- [ ] Persist orchestration state transitions to S3 JSONL
- [ ] Implement automatic deployment safety gates
- [ ] Implement fallback behavior for low-confidence recommendations
- [ ] Add `strategy plan`, `strategy profile`, `strategy recommend`, `strategy infra-plan`, `strategy deploy`, and `strategy teardown`
- [ ] Add `strategy execute` for one-shot warmup-to-teardown execution
- [ ] Enforce latest deployment cutoff and profiler blackout timing

### Phase 10 — Strategy Runtime Deployment

- [ ] Add AWS strategy runtime infrastructure module
- [ ] Generate OpenTofu variable files from infrastructure recommendations
- [ ] Support EC2 runtime startup command
- [ ] Support container artifact pull from ECR
- [ ] Support read-only and trading secret injection via AWS Secrets Manager
- [ ] Add runtime readiness checks
- [ ] Add execution handoff command
- [ ] Add post-execution teardown workflow

### Phase 11 — Host Scheduler

- [ ] Implement scheduler loop for multiple strategy profiles
- [ ] Load event datetimes and compute warmup windows
- [ ] Run profiles at scheduled warmup start
- [ ] Prevent overlapping incompatible strategy deployments
- [ ] Emit scheduler heartbeats and audit events
- [ ] Add dry-run mode for schedule validation

---

## 12. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| CDN/load balancer hides true origin | Medium | Report observed path and performance, not claimed server location |
| Cloud cold-start or VM boot variance | Medium | Separate deployment time from probe time; warm runners before measuring |
| Rate limits or endpoint abuse | High | Explicit targets, low defaults, burst opt-in |
| Terraform state leaks secrets | High | Keep v1 auth-free; never place PEM/API secrets in state |
| Traceroute blocked | Low | Treat trace as optional diagnostic |
| Results overfit one day | Medium | Support baseline mode and historical reports |
| Different DNS by resolver | Medium | Record resolver and resolved IPs |
| Recommendation overreacts to noisy samples | High | Require minimum samples, confidence scoring, and score-margin thresholds |
| Orchestrator deploys suboptimal infrastructure | High | Fail closed with minimum confidence, sample, and score-margin gates |
| Profiler competes with hot execution path | High | Enforce profiler blackout and stop telemetry-heavy work before execution |
| Strategy runtime starts too late | High | Enforce latest deployment cutoff and fallback behavior |
| Secrets leak through generated infrastructure | High | Use secret references, never plaintext secrets in state, logs, or generated artifacts |
| Deployment automation creates unexpected cost | Medium | Tag resources by run, default teardown, max runtime limits, and budget alarms |
| Profiler becomes coupled to NullOne strategy logic | Medium | Integrate through artifacts and commands; do not import trading logic |
| Accidental deletion of historical results | High | Preserve the S3 results bucket on runner teardown; require explicit destructive opt-in for bucket deletion |
| Public-cloud latency does not meet strategy requirements | High | Report measured reality, support abort/fallback, and avoid promising sub-millisecond performance |
| Authenticated warmup probes violate venue rules | High | Require explicit opt-in, read-only defaults, compliance review, and low-rate safeguards |

---

## 13. Open Questions

1. Should v1 be Python for speed of development or Go for easier single-binary runner distribution?
2. Should the S3 bucket be owned by this repo's IaC state, imported if it already exists, or referenced as an externally managed bucket?
3. Which AWS regions should be tested first for NullOne: `us-east-1`, `us-east-2`, `us-west-2`, `ca-central-1`?
4. Which Azure regions are most relevant: `eastus`, `eastus2`, `centralus`?
5. Should authenticated Kalshi REST probes be included in v1, or deferred until secret handling is mature?
6. What weighted objective should rank regions for NullOne: EIA first-byte latency, Kalshi order latency, or a combined score?
7. What minimum confidence should NullOne require before using a placement recommendation for warmup or deployment?
8. Should recommendations be consumed from local files, object storage, Redis, HTTP webhooks, or another control-plane interface?
9. Should the first automated deployment target be EC2, ECS, or an existing NullOne host image?
10. What fallback should be used when no candidate region meets confidence gates: abort, deploy default, or deploy multiple standbys?
11. How should strategy execution success/failure be reported back to the orchestrator?
12. Which secrets are required during warmup versus execution, and how should they be scoped?
13. Should the scheduler run as a systemd service on EC2, a container, or a managed job service?
14. What is the minimum safe profiler blackout interval for NullOne: `T-2m`, `T-1m`, or strategy-specific?
15. Should the orchestrator support deploying multiple hot standbys when recommendations are close?

---

## 14. Decision Summary

| Decision | Current Position |
|----------|------------------|
| Separate repo | Yes |
| IaC | OpenTofu/Terraform |
| First runtime | VM-based runners |
| First provider | AWS |
| Second provider | Azure |
| First target pack | NullOne |
| V1 auth | Avoid secrets; public/read-only endpoints only |
| Primary output | JSONL in persistent S3 + ranked report |
| Result retention | S3 bucket persists after runner teardown by default |
| Placement advisor | Yes, as recommendation output consumed by a policy-gated orchestrator |
| Automatic deployment | Yes, when explicitly enabled by strategy policy and safety gates pass |
| Profiler blackout | Required before strategy execution |
| Strategy logic boundary | Orchestrator starts configured artifacts/commands but does not place orders |
| Teardown | Ephemeral strategy compute is torn down by default; telemetry persists |

