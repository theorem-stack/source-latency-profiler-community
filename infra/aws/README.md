# AWS Infrastructure

This module creates disposable EC2 probe runners and a durable S3 results bucket.

## Results Bucket

By default, the module creates a bucket named:

```text
source-latency-profiler-results-<account_id>
```

The bucket has:

- versioning enabled
- server-side encryption enabled
- public access blocked
- `prevent_destroy = true`

If you already have a bucket, set:

```hcl
create_results_bucket = false
results_bucket_name   = "existing-bucket-name"
```

In that mode, this module grants runners write access to the existing bucket but does not manage bucket settings.

## Runner Teardown

Do not use a plain full-state destroy when you want to preserve historical results. Use the CLI guidance:

```bash
latency-profiler infra destroy --dir infra/aws
```

That command prints a targeted OpenTofu/Terraform destroy for ephemeral runner resources only.

## Strategy Runtime

The module can also create one ephemeral EC2 strategy runtime for NullOne when `strategy_runtime_enabled=true`.

The runtime host:

- downloads a generated NullOne runtime config from S3
- pulls the configured NullOne container image when one is provided
- starts a long-lived `nullone-runtime` container during boot
- runs `nullone ready --config /etc/nullone/runtime.json` inside that container
- exposes `/usr/local/bin/nullone-health`, `/usr/local/bin/nullone-ready`, `/usr/local/bin/nullone-execute`, and `/usr/local/bin/nullone-shutdown` wrappers that use `docker exec`

Generate the required variables with:

```bash
latency-profiler strategy tfvars \
  --infra-recommendation recommendations/<run-id>-infra.json \
  --runtime-config-s3 s3://<bucket>/runtime/runs/<run-id>/combined/<run-id>.json \
  --output .generated/<run-id>/strategy-runtime.tfvars.json
```

Then deploy:

```bash
tofu -chdir=infra/aws apply -var-file="$(pwd)/.generated/<run-id>/strategy-runtime.tfvars.json"
```

The strategy runtime is intended to be torn down after execution. The persistent results bucket remains.
