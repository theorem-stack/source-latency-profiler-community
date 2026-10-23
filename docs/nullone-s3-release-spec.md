# NullOne S3 Release Spec

## Purpose

Define the release artifact contract for the NullOne repository so `source-latency-profiler` can install a deterministic NullOne binary during orchestrator host bootstrap.

This spec is the producer/consumer agreement between:

- **Producer**: NullOne CI/CD pipeline (build + publish)
- **Consumer**: `source-latency-profiler` deploy scripts (`deploy_orchestrator_ec2.sh` and `deploy_orchestrator_host.sh`)

## Scope

This spec covers:

- binary artifact naming and S3 key structure
- checksum sidecar behavior
- required IAM permissions
- release promotion and rollback semantics
- compatibility guarantees expected by `source-latency-profiler`

This spec does **not** prescribe a specific build toolchain for NullOne.

## Canonical Artifact Contract

### Required binary object

NullOne CI must publish a Linux x86_64 executable to:

`s3://<results-bucket>/nullone/releases/<version>/linux-amd64/nullone`

Where:

- `<results-bucket>` is the shared bucket used by `source-latency-profiler`
- `<version>` is immutable (for example `v1.2.3` or `v1.2.3+build.7`)

### Optional checksum sidecar (recommended)

When present, publish:

`s3://<results-bucket>/nullone/releases/<version>/linux-amd64/nullone.sha256`

Expected format:

`<sha256>  nullone`

If this file exists, `source-latency-profiler` host bootstrap verifies it before installing the binary.

## Immutability Rules

- Release keys under `/nullone/releases/<version>/...` are immutable.
- Do not overwrite a previously published `<version>`.
- Do not use mutable `latest` keys for scheduler-enabled production deploys.

Rationale: rollout and rollback must be a single URI change without ambiguity.

## Consumer Input (source-latency-profiler)

`source-latency-profiler` deploys with:

- `NULLONE_INSTALL_MODE=s3`
- `NULLONE_S3_URI=s3://<results-bucket>/nullone/releases/<version>/linux-amd64/nullone`

`source-latency-profiler` enforces:

- URI must be in the same `RESULTS_BUCKET`
- URI must be under `nullone/releases/`
- URI must end with `/nullone`
- URI cannot include `/latest/`

## IAM Requirements

### NullOne release writer identity (CI)

Must be allowed:

- `s3:PutObject` on `arn:aws:s3:::<results-bucket>/nullone/releases/*`
- `s3:AbortMultipartUpload` on same prefix (if multipart upload used)

Optional (for publish checks):

- `s3:ListBucket` on `arn:aws:s3:::<results-bucket>`

### source-latency-profiler orchestrator host role

Must be allowed:

- `s3:GetObject` on `arn:aws:s3:::<results-bucket>/nullone/releases/*`

Already expected in profiler IAM templates for release consumption.

## NullOne CI Publishing Flow

1. Build Linux AMD64 NullOne binary named `nullone`.
2. Ensure executable bit is set.
3. Compute SHA256 checksum.
4. Upload binary to:
   - `s3://<results-bucket>/nullone/releases/<version>/linux-amd64/nullone`
5. Upload checksum to:
   - `.../nullone.sha256` (recommended)
6. Emit release metadata to CI logs:
   - `version`
   - `git_sha`
   - `build_timestamp_utc`
   - `target_os`
   - `target_arch`
   - published S3 URIs

## Release Promotion

To promote a new NullOne release in `source-latency-profiler`:

1. Publish new NullOne binary to immutable version key.
2. Run profiler deploy with updated `NULLONE_S3_URI`.
3. Verify scheduler services are healthy.

No in-place binary mutation on the existing host is required.

## Rollback

Rollback is done by re-running deploy with a previous immutable `NULLONE_S3_URI`:

`s3://<results-bucket>/nullone/releases/<previous-version>/linux-amd64/nullone`

Because keys are immutable, rollback behavior is deterministic.

## Compatibility Expectations

NullOne binary published to S3 must:

- run on Amazon Linux 2023 x86_64
- respond to `nullone --help` with exit code `0`
- support the discover command used by profiler strategy refresh flows

If breaking CLI changes are introduced in NullOne, bump major version and update profiler integration before promotion.

## Example Values

`123456789012` is the AWS documentation sample account, substituted for a private account id. Replace it before any real deployment.

- bucket: `source-latency-profiler-results-123456789012`
- release URI:
  - `s3://source-latency-profiler-results-123456789012/nullone/releases/v1.8.0/linux-amd64/nullone`
- checksum URI:
  - `s3://source-latency-profiler-results-123456789012/nullone/releases/v1.8.0/linux-amd64/nullone.sha256`

