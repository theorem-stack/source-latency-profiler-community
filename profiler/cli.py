from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from profiler.artifacts import artifact_with_resolved_image, plan_ecr_image_cleanup, resolve_ecr_image, validate_container_image
from profiler.config import Runner, load_placement_profile, load_runners, load_targets
from profiler.http_probe import probe_http
from profiler.infra_profile import build_infrastructure_recommendation, write_infrastructure_recommendation
from profiler.janitor import run_strategy_runtime_janitor
from profiler.orchestrator import ExecuteOptions, execute_strategy, teardown_strategy_runtime
from profiler.preflight import PreflightRequest, build_orchestrator_policy, run_preflight
from profiler.recommend import build_recommendation, write_recommendation
from profiler.report import build_report, print_report
from profiler.result import ProbeResult, read_jsonl, write_jsonl
from profiler.runtime_config import build_nullone_runtime_config, write_runtime_config
from profiler.secrets import build_secret_plan, secret_arns_for_names, secret_names_for_prefix, sync_secret_plan, validate_secret_prefix
from profiler.storage import build_s3_run_uri, download_s3_prefix, is_s3_uri, read_jsonl_from_s3, upload_file_to_s3
from profiler.markets.resolve import resolve_markets, resolve_strategy_profile
from profiler.scheduler import SchedulerOptions, run_scheduler
from profiler.secrets import load_kalshi_env_from_prefix
from profiler.strategy import load_strategy_profile, strategy_summary, write_strategy_artifacts
from profiler.tfvars import build_strategy_runtime_tfvars, write_tfvars
from profiler.ws_probe import probe_websocket


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI should present concise failures.
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="latency-profiler")
    subcommands = parser.add_subparsers(dest="command", required=True)

    probe = subcommands.add_parser("probe", help="Run local probes against configured targets.")
    probe.add_argument("--targets", required=True)
    probe.add_argument("--runners")
    probe.add_argument("--samples", type=int, default=3)
    probe.add_argument("--interval-ms", type=int, default=1000)
    probe.add_argument("--output", default="results/probe.jsonl")
    probe.add_argument("--upload-s3")
    probe.add_argument("--upload-s3-prefix")
    probe.add_argument("--run-id")
    probe.set_defaults(func=cmd_probe)

    run = subcommands.add_parser("run", help="Alias for probe.")
    run.add_argument("--targets", required=True)
    run.add_argument("--runners")
    run.add_argument("--samples", type=int, default=3)
    run.add_argument("--duration")
    run.add_argument("--interval-ms", type=int, default=1000)
    run.add_argument("--output", default="results/run.jsonl")
    run.add_argument("--upload-s3")
    run.add_argument("--upload-s3-prefix")
    run.add_argument("--run-id")
    run.set_defaults(func=cmd_probe)

    burst = subcommands.add_parser("burst", help="Run high-resolution probes for an event window.")
    burst.add_argument("--targets", required=True)
    burst.add_argument("--runners")
    burst.add_argument("--duration", default="120s")
    burst.add_argument("--interval-ms", type=int, required=True)
    burst.add_argument("--output", default="results/burst.jsonl")
    burst.add_argument("--upload-s3")
    burst.add_argument("--upload-s3-prefix")
    burst.add_argument("--run-id")
    burst.set_defaults(func=cmd_burst)

    report = subcommands.add_parser("report", help="Build a ranked report from JSONL results.")
    report.add_argument("--input", required=True, nargs="+")
    report.set_defaults(func=cmd_report)

    recommend = subcommands.add_parser("recommend", help="Build a placement recommendation.")
    recommend.add_argument("--input", required=True, nargs="+")
    recommend.add_argument("--profile", required=True)
    recommend.add_argument("--output", default="recommendations/placement.json")
    recommend.set_defaults(func=cmd_recommend)

    collect = subcommands.add_parser("collect", help="Collect result objects from S3.")
    collect.add_argument("--from-s3", dest="from_s3", required=True)
    collect.add_argument("--output-dir", default="results/collected")
    collect.set_defaults(func=cmd_collect)

    artifacts = subcommands.add_parser("artifacts", help="Validate runtime artifacts.")
    artifacts_subcommands = artifacts.add_subparsers(dest="artifacts_command", required=True)
    artifacts_validate = artifacts_subcommands.add_parser("validate", help="Validate a container image URI.")
    artifacts_validate.add_argument("--image", required=True)
    artifacts_validate.add_argument("--allow-latest", action="store_true")
    artifacts_validate.set_defaults(func=cmd_artifacts_validate)
    artifacts_resolve = artifacts_subcommands.add_parser("resolve-ecr", help="Resolve the newest ECR image or fallback.")
    artifacts_resolve.add_argument("--repository", required=True, help="ECR repository URI without a tag.")
    artifacts_resolve.add_argument("--tag-prefix")
    artifacts_resolve.add_argument("--fallback-image")
    artifacts_resolve.add_argument("--region")
    artifacts_resolve.set_defaults(func=cmd_artifacts_resolve_ecr)
    artifacts_cleanup = artifacts_subcommands.add_parser("cleanup-plan", help="Plan cleanup for old ECR image tags.")
    artifacts_cleanup.add_argument("--repository", required=True, help="ECR repository URI without a tag.")
    artifacts_cleanup.add_argument("--tag-prefix")
    artifacts_cleanup.add_argument("--keep-last", type=int, default=10)
    artifacts_cleanup.add_argument("--region")
    artifacts_cleanup.set_defaults(func=cmd_artifacts_cleanup_plan)

    secrets = subcommands.add_parser("secrets", help="Manage NullOne runtime secrets in AWS Secrets Manager.")
    secrets_subcommands = secrets.add_subparsers(dest="secrets_command", required=True)

    secrets_plan = secrets_subcommands.add_parser("plan", help="Show redacted Secrets Manager sync plan.")
    _add_secret_source_args(secrets_plan)
    secrets_plan.set_defaults(func=cmd_secrets_plan)

    secrets_sync = secrets_subcommands.add_parser("sync", help="Create or update Secrets Manager secrets.")
    _add_secret_source_args(secrets_sync)
    secrets_sync.add_argument("--yes", action="store_true", help="Apply without interactive confirmation.")
    secrets_sync.set_defaults(func=cmd_secrets_sync)

    secrets_validate = secrets_subcommands.add_parser("validate", help="Validate expected Secrets Manager secrets exist.")
    secrets_validate.add_argument("--prefix", required=True)
    secrets_validate.add_argument("--region")
    secrets_validate.set_defaults(func=cmd_secrets_validate)

    infra = subcommands.add_parser("infra", help="Print IaC command guidance.")
    infra_subcommands = infra.add_subparsers(dest="infra_command", required=True)
    for name in ("plan", "apply", "destroy"):
        command = infra_subcommands.add_parser(name)
        command.add_argument("--dir", default="infra/aws")
        command.set_defaults(func=cmd_infra)
    infra_preflight = infra_subcommands.add_parser("preflight", help="Check orchestrator AWS permissions.")
    infra_preflight.add_argument("--account-id", required=True)
    infra_preflight.add_argument("--region", default="us-east-1")
    infra_preflight.add_argument("--results-bucket", required=True)
    infra_preflight.add_argument("--deploy-bucket")
    infra_preflight.add_argument("--project-name", default="source-latency-profiler")
    infra_preflight.add_argument("--sample-run-id", default="preflight")
    infra_preflight.add_argument(
        "--policy-only",
        action="store_true",
        help="Print the required IAM policy JSON without calling AWS.",
    )
    infra_preflight.add_argument(
        "--check-nullone",
        action="store_true",
        help="Verify nullone is on PATH (for market discovery).",
    )
    infra_preflight.set_defaults(func=cmd_infra_preflight)

    strategy = subcommands.add_parser("strategy", help="Run v2 strategy orchestration workflows.")
    strategy_subcommands = strategy.add_subparsers(dest="strategy_command", required=True)

    strategy_plan = strategy_subcommands.add_parser("plan", help="Validate and summarize a strategy profile.")
    strategy_plan.add_argument("--strategy", required=True)
    strategy_plan.set_defaults(func=cmd_strategy_plan)

    strategy_profile = strategy_subcommands.add_parser("profile", help="Materialize strategy configs and run local probes.")
    strategy_profile.add_argument("--strategy", required=True)
    strategy_profile.add_argument("--run-id")
    strategy_profile.add_argument("--samples", type=int, default=3)
    strategy_profile.add_argument("--interval-ms", type=int, default=1000)
    strategy_profile.add_argument("--output")
    strategy_profile.add_argument("--artifacts-dir")
    strategy_profile.add_argument("--upload-s3")
    strategy_profile.add_argument("--upload-s3-prefix")
    strategy_profile.set_defaults(func=cmd_strategy_profile)

    strategy_recommend = strategy_subcommands.add_parser("recommend", help="Build a strategy placement recommendation.")
    strategy_recommend.add_argument("--strategy", required=True)
    strategy_recommend.add_argument("--input", required=True, nargs="+")
    strategy_recommend.add_argument("--output")
    strategy_recommend.set_defaults(func=cmd_strategy_recommend)

    strategy_infra = strategy_subcommands.add_parser("infra-plan", help="Build a deployable infrastructure recommendation.")
    strategy_infra.add_argument("--strategy", required=True)
    strategy_infra.add_argument("--recommendation", required=True)
    strategy_infra.add_argument("--run-id", required=True)
    strategy_infra.add_argument("--output")
    strategy_infra.add_argument(
        "--no-resolve-artifact-image",
        action="store_true",
        help="Skip ECR lookup and use the image pinned in the strategy profile.",
    )
    strategy_infra.set_defaults(func=cmd_strategy_infra_plan)

    strategy_runtime = strategy_subcommands.add_parser("runtime-config", help="Generate a NullOne runtime config.")
    strategy_runtime.add_argument("--strategy", required=True)
    strategy_runtime.add_argument("--infra-recommendation", required=True)
    strategy_runtime.add_argument("--output", required=True)
    strategy_runtime.add_argument("--upload-s3")
    strategy_runtime.add_argument("--upload-s3-prefix")
    strategy_runtime.add_argument("--run-id")
    strategy_runtime.set_defaults(func=cmd_strategy_runtime_config)

    strategy_tfvars = strategy_subcommands.add_parser("tfvars", help="Generate OpenTofu variables for strategy runtime deploy.")
    strategy_tfvars.add_argument("--infra-recommendation", required=True)
    strategy_tfvars.add_argument("--runtime-config-s3", required=True)
    strategy_tfvars.add_argument("--output", required=True)
    strategy_tfvars.add_argument("--secrets-prefix")
    strategy_tfvars.add_argument("--secret-name", action="append", default=[])
    strategy_tfvars.add_argument("--secret-arn", action="append", default=[])
    strategy_tfvars.set_defaults(func=cmd_strategy_tfvars)

    strategy_execute = strategy_subcommands.add_parser("execute", help="Print dry-run orchestration steps for a strategy run.")
    strategy_execute.add_argument("--strategy", required=True)
    strategy_execute.add_argument("--run-id", required=True)
    strategy_execute.add_argument("--secrets-prefix")
    strategy_execute.add_argument("--results-s3-prefix")
    strategy_execute.add_argument("--runtime-s3-prefix")
    strategy_execute.add_argument("--apply", action="store_true", help="Run the workflow instead of printing a dry-run plan.")
    strategy_execute.add_argument("--plan-only", action="store_true", help="Run through OpenTofu plan but do not apply.")
    strategy_execute.add_argument("--auto-teardown", action="store_true", help="Alias for --teardown-mode after-ready.")
    strategy_execute.add_argument(
        "--teardown-mode",
        choices=["never", "after-ready", "after-execution", "ttl"],
        default="never",
        help="When to tear down runtime resources after apply.",
    )
    strategy_execute.add_argument("--teardown-after-seconds", type=int, default=0, help="Seconds to wait before TTL teardown.")
    strategy_execute.add_argument("--execution-timeout-seconds", type=int, default=900, help="Timeout for after-execution mode.")
    strategy_execute.add_argument(
        "--execution-handoff",
        choices=["ssm-immediate", "ssm-lead", "host-timer"],
        default="ssm-immediate",
        help="How execute is triggered on the runtime host (default: ssm-immediate).",
    )
    strategy_execute.add_argument(
        "--execution-lead-seconds",
        type=int,
        default=60,
        help="Lead time before execute_at for ssm-lead/host-timer handoff.",
    )
    strategy_execute.add_argument(
        "--diagnostics",
        choices=["none", "hot-path", "live-paper"],
        default="none",
        help="Capture built-in runtime diagnostics during execution.",
    )
    strategy_execute.add_argument("--diagnostics-s3-prefix", help="Optional S3 prefix for diagnostics bundle uploads.")
    strategy_execute.add_argument("--require-public-ip", action="store_true", help="Override tfvars to assign a public IP.")
    strategy_execute.add_argument("--work-dir", help="Run workspace directory.")
    strategy_execute.add_argument("--samples", type=int, default=3)
    strategy_execute.add_argument("--interval-ms", type=int, default=1000)
    strategy_execute.add_argument("--tofu-dir", default="infra/aws")
    strategy_execute.add_argument("--ready-timeout-seconds", type=int, default=600)
    strategy_execute.add_argument(
        "--runtime-ttl-seconds",
        type=int,
        default=7200,
        help="TTL tag applied to runtime resources for independent janitor cleanup.",
    )
    strategy_execute.add_argument("--allow-live", action="store_true", help="Permit live mode if all profile guardrails pass.")
    strategy_execute.add_argument(
        "--live-capital-limit-usd",
        type=float,
        default=0.0,
        help="Maximum release capital allowed for live mode.",
    )
    strategy_execute.add_argument(
        "--kill-switch-file",
        default="/opt/source-latency-profiler/kill-switch",
        help="If this file exists, applied strategy execution is denied.",
    )
    strategy_execute.set_defaults(func=cmd_strategy_execute)

    strategy_deploy = strategy_subcommands.add_parser("deploy", help="Print or apply strategy runtime deployment.")
    strategy_deploy.add_argument("--infra-recommendation", required=True)
    strategy_deploy.add_argument("--tfvars", required=True)
    strategy_deploy.add_argument("--dir", default="infra/aws")
    strategy_deploy.set_defaults(func=cmd_strategy_deploy)

    strategy_teardown = strategy_subcommands.add_parser("teardown", help="Print teardown guidance for a strategy run.")
    strategy_teardown.add_argument("--run-id", required=True)
    strategy_teardown.add_argument("--dir", default="infra/aws")
    strategy_teardown.add_argument("--yes", action="store_true", help="Run teardown helper without prompting.")
    strategy_teardown.set_defaults(func=cmd_strategy_teardown)

    strategy_janitor = strategy_subcommands.add_parser("janitor", help="Clean up expired strategy runtime resources.")
    strategy_janitor.add_argument("--region", default="us-east-1")
    strategy_janitor.add_argument("--project-name", default="source-latency-profiler")
    strategy_janitor.add_argument("--app-dir", default=".")
    strategy_janitor.add_argument("--yes", action="store_true", help="Perform teardown instead of dry-run planning.")
    strategy_janitor.set_defaults(func=cmd_strategy_janitor)

    strategy_import_markets = strategy_subcommands.add_parser(
        "import-markets",
        help="Run nullone discover and import markets into the strategy profile.",
    )
    strategy_import_markets.add_argument("--strategy", required=True)
    strategy_import_markets.add_argument("--secrets-prefix")
    strategy_import_markets.add_argument("--output-dir", default=".generated/markets")
    strategy_import_markets.set_defaults(func=cmd_strategy_import_markets)

    scheduler = subcommands.add_parser("scheduler", help="Run strategy scheduling workflows.")
    scheduler_subcommands = scheduler.add_subparsers(dest="scheduler_command", required=True)
    scheduler_run = scheduler_subcommands.add_parser("run", help="Run market refresh schedule and warmup execute.")
    scheduler_run.add_argument("--strategy", default="strategies/nullone-wngsr-kalshi.yaml")
    scheduler_run.add_argument("--secrets-prefix")
    scheduler_run.add_argument("--state-path", default=".generated/scheduler/state.json")
    scheduler_run.add_argument("--events-path", default=".generated/scheduler/events.jsonl")
    scheduler_run.add_argument("--poll-seconds", type=int, default=60)
    scheduler_run.add_argument("--once", action="store_true", help="Evaluate due actions once and exit.")
    scheduler_run.add_argument("--dry-run", action="store_true", help="Print due actions without running them.")
    scheduler_run.add_argument("--results-s3-prefix")
    scheduler_run.add_argument("--runtime-s3-prefix")
    scheduler_run.add_argument("--run-id")
    scheduler_run.add_argument("--teardown-mode", default="after-execution")
    scheduler_run.add_argument("--diagnostics", choices=["none", "hot-path", "live-paper"], default="none")
    scheduler_run.add_argument(
        "--execution-handoff",
        choices=["ssm-immediate", "ssm-lead", "host-timer"],
        default="host-timer",
    )
    scheduler_run.add_argument("--execution-lead-seconds", type=int, default=60)
    scheduler_run.add_argument("--app-dir", default=".")
    scheduler_run.add_argument(
        "--event-calendar",
        dest="event_calendar_path",
        default=None,
        help=(
            "Path to a JSON event calendar file. When provided, the strategy YAML is used as a "
            "template and event_time/event_id are supplied per-cycle from the calendar."
        ),
    )
    scheduler_run.set_defaults(func=cmd_scheduler_run)

    return parser


def cmd_probe(args: argparse.Namespace) -> int:
    targets = load_targets(args.targets)
    runners = load_runners(args.runners)
    samples = _samples_from_args(args)
    results = run_probes(targets, runners, samples=samples, interval_ms=args.interval_ms)
    write_jsonl(args.output, results)
    uploaded_to = _maybe_upload(args.output, args)
    if uploaded_to:
        print(f"Uploaded results to {uploaded_to}")
    print(f"Wrote {len(results)} results to {args.output}")
    return 0


def cmd_burst(args: argparse.Namespace) -> int:
    if args.interval_ms < 250:
        raise ValueError("burst mode requires --interval-ms >= 250")
    targets = load_targets(args.targets)
    runners = load_runners(args.runners)
    samples = max(1, int((_parse_duration_seconds(args.duration) * 1000) / args.interval_ms))
    results = run_probes(targets, runners, samples=samples, interval_ms=args.interval_ms)
    write_jsonl(args.output, results)
    uploaded_to = _maybe_upload(args.output, args)
    if uploaded_to:
        print(f"Uploaded results to {uploaded_to}")
    print(f"Wrote {len(results)} burst results to {args.output}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    results = _read_inputs(args.input)
    print(print_report(build_report(results)))
    return 0


def cmd_recommend(args: argparse.Namespace) -> int:
    results = _read_inputs(args.input)
    profile = load_placement_profile(args.profile)
    recommendation = build_recommendation(results, profile)
    write_recommendation(args.output, recommendation)
    print(f"Wrote placement recommendation to {args.output}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    downloaded = download_s3_prefix(args.from_s3, args.output_dir)
    print(f"Downloaded {len(downloaded)} files to {args.output_dir}")
    return 0


def cmd_artifacts_validate(args: argparse.Namespace) -> int:
    details = validate_container_image(args.image, allow_latest=args.allow_latest)
    print(json.dumps(details, indent=2, sort_keys=True))
    return 0


def cmd_artifacts_resolve_ecr(args: argparse.Namespace) -> int:
    resolved = resolve_ecr_image(
        args.repository,
        tag_prefix=args.tag_prefix,
        fallback_image=args.fallback_image,
        region=args.region,
    )
    print(json.dumps(resolved, indent=2, sort_keys=True))
    return 0


def cmd_artifacts_cleanup_plan(args: argparse.Namespace) -> int:
    plan = plan_ecr_image_cleanup(
        args.repository,
        keep_last=args.keep_last,
        tag_prefix=args.tag_prefix,
        region=args.region,
    )
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


def cmd_secrets_plan(args: argparse.Namespace) -> int:
    plan = build_secret_plan(env_file=args.env_file, pem_file=args.pem_file, prefix=args.prefix)
    print(json.dumps(plan.redacted(), indent=2, sort_keys=True))
    return 0


def cmd_secrets_sync(args: argparse.Namespace) -> int:
    plan = build_secret_plan(env_file=args.env_file, pem_file=args.pem_file, prefix=args.prefix)
    print(json.dumps(plan.redacted(), indent=2, sort_keys=True))
    if not args.yes:
        raise ValueError("Refusing to sync secrets without --yes")
    results = sync_secret_plan(plan, region=args.region)
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


def cmd_secrets_validate(args: argparse.Namespace) -> int:
    results = validate_secret_prefix(args.prefix, region=args.region)
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0 if all(result["exists"] for result in results) else 1


def cmd_infra(args: argparse.Namespace) -> int:
    if args.infra_command == "destroy":
        print(f"Run: tofu -chdir={args.dir} destroy \\")
        print("  -target=aws_instance.runner \\")
        print("  -target=aws_iam_instance_profile.runner \\")
        print("  -target=aws_iam_role_policy.runner_results \\")
        print("  -target=aws_iam_role.runner \\")
        print("  -target=aws_security_group.runner")
        print("This destroys ephemeral runner resources and leaves the S3 results bucket intact.")
        return 0

    print(f"Run: tofu -chdir={args.dir} {args.infra_command}")
    print("The S3 results bucket is persistent. Use create_results_bucket=false to reference an existing bucket.")
    return 0


def cmd_infra_preflight(args: argparse.Namespace) -> int:
    request = PreflightRequest(
        account_id=args.account_id,
        region=args.region,
        results_bucket=args.results_bucket,
        deploy_bucket=args.deploy_bucket,
        project_name=args.project_name,
        sample_run_id=args.sample_run_id,
    )
    if args.policy_only:
        print(json.dumps(build_orchestrator_policy(request), indent=2, sort_keys=True))
        return 0

    result = run_preflight(request)
    missing = result.get("missing_actions", [])
    if args.check_nullone:
        result["nullone"] = _check_nullone_cli()
    print(json.dumps(result, indent=2, sort_keys=True))
    if missing:
        print(f"Missing permissions: {', '.join(missing)}", file=sys.stderr)
        return 1
    nullone_check = result.get("nullone", {})
    if nullone_check.get("status") == "failed":
        print(f"NullOne check failed: {nullone_check.get('error')}", file=sys.stderr)
        return 1
    return 0


def cmd_strategy_plan(args: argparse.Namespace) -> int:
    strategy = load_strategy_profile(args.strategy)
    print(strategy_summary(strategy))
    return 0


def cmd_strategy_import_markets(args: argparse.Namespace) -> int:
    env = load_kalshi_env_from_prefix(args.secrets_prefix) if args.secrets_prefix else None
    strategy = load_strategy_profile(args.strategy)
    markets = resolve_markets(strategy, env=env, audit_dir=args.output_dir)
    print(
        f"Imported {len(markets.get('brackets', []))} brackets "
        f"from {markets.get('spec_path')} (kalshi_event={markets.get('kalshi_event_ticker')})"
    )
    print(f"Audit artifacts: {args.output_dir}")
    return 0


def cmd_scheduler_run(args: argparse.Namespace) -> int:
    return run_scheduler(
        SchedulerOptions(
            strategy_path=args.strategy,
            secrets_prefix=args.secrets_prefix,
            state_path=args.state_path,
            events_path=args.events_path,
            poll_seconds=args.poll_seconds,
            once=args.once,
            dry_run=args.dry_run,
            results_s3_prefix=args.results_s3_prefix,
            runtime_s3_prefix=args.runtime_s3_prefix,
            run_id=args.run_id,
            teardown_mode=args.teardown_mode,
            diagnostics=args.diagnostics,
            execution_handoff=args.execution_handoff,
            execution_lead_seconds=args.execution_lead_seconds,
            app_dir=args.app_dir,
            event_calendar_path=getattr(args, "event_calendar_path", None),
        )
    )


def cmd_strategy_profile(args: argparse.Namespace) -> int:
    strategy = load_strategy_profile(args.strategy)
    run_id = args.run_id or _default_run_id()
    artifacts_dir = args.artifacts_dir or f".generated/{strategy.name}/{run_id}"
    artifacts = write_strategy_artifacts(strategy, artifacts_dir)
    output = args.output or f"results/{strategy.name}-{run_id}.jsonl"
    local_runner = Runner(provider="local", region="local", runtime="local", runner_id="local")
    results = run_probes(strategy.targets, [local_runner], samples=args.samples, interval_ms=args.interval_ms)
    write_jsonl(output, results)
    uploaded_to = _maybe_upload(output, args)
    print(f"Wrote strategy artifacts to {artifacts_dir}")
    for name, path in artifacts.items():
        print(f"{name}: {path}")
    if uploaded_to:
        print(f"Uploaded results to {uploaded_to}")
    print(f"Wrote {len(results)} strategy probe results to {output}")
    return 0


def cmd_strategy_recommend(args: argparse.Namespace) -> int:
    strategy = load_strategy_profile(args.strategy)
    results = _read_inputs(args.input)
    recommendation = build_recommendation(results, strategy.to_placement_profile())
    output = args.output or f"recommendations/{strategy.name}-placement.json"
    write_recommendation(output, recommendation)
    print(f"Wrote strategy placement recommendation to {output}")
    return 0


def cmd_strategy_infra_plan(args: argparse.Namespace) -> int:
    strategy = load_strategy_profile(args.strategy)
    recommendation = _read_json(args.recommendation)
    artifact = None
    if not args.no_resolve_artifact_image:
        artifact = artifact_with_resolved_image(strategy.deployment.get("artifact", {}))
    infra_recommendation = build_infrastructure_recommendation(
        strategy,
        recommendation,
        run_id=args.run_id,
        artifact_override=artifact,
    )
    output = args.output or f"recommendations/{strategy.name}-{args.run_id}-infra.json"
    write_infrastructure_recommendation(output, infra_recommendation)
    print(f"Wrote infrastructure recommendation to {output}")
    print(f"policy_decision: {infra_recommendation['policy_decision']['decision']}")
    resolved = infra_recommendation["recommended_infrastructure"]["artifact"].get("resolved")
    if resolved:
        print(f"artifact_image_source: {resolved['source']}")
        print(f"artifact_image: {infra_recommendation['recommended_infrastructure']['artifact']['image']}")
    return 0


def cmd_strategy_runtime_config(args: argparse.Namespace) -> int:
    strategy = load_strategy_profile(args.strategy)
    infra_recommendation = _read_json(args.infra_recommendation)
    runtime_config = build_nullone_runtime_config(strategy, infra_recommendation)
    write_runtime_config(args.output, runtime_config)
    uploaded_to = _maybe_upload(args.output, args)
    if uploaded_to:
        print(f"Uploaded runtime config to {uploaded_to}")
    print(f"Wrote NullOne runtime config to {args.output}")
    return 0


def cmd_strategy_tfvars(args: argparse.Namespace) -> int:
    infra_recommendation = _read_json(args.infra_recommendation)
    secret_names = list(args.secret_name)
    if args.secrets_prefix:
        secret_names.extend(secret_names_for_prefix(args.secrets_prefix))
    secret_arns = list(args.secret_arn)
    if secret_names:
        secret_arns.extend(secret_arns_for_names(secret_names, region=infra_recommendation["recommended_infrastructure"]["region"]))
    tfvars = build_strategy_runtime_tfvars(
        infra_recommendation,
        runtime_config_s3_uri=args.runtime_config_s3,
        secret_arns=secret_arns,
        secret_names=secret_names,
    )
    write_tfvars(args.output, tfvars)
    print(f"Wrote strategy runtime OpenTofu variables to {args.output}")
    return 0


def cmd_strategy_execute(args: argparse.Namespace) -> int:
    if args.apply or args.plan_only:
        if not args.results_s3_prefix or not args.runtime_s3_prefix:
            raise ValueError("--results-s3-prefix and --runtime-s3-prefix are required with --apply or --plan-only")
        result = execute_strategy(
            ExecuteOptions(
                strategy_path=args.strategy,
                run_id=args.run_id,
                results_s3_prefix=args.results_s3_prefix,
                runtime_s3_prefix=args.runtime_s3_prefix,
                secrets_prefix=args.secrets_prefix,
                work_dir=args.work_dir,
                tofu_dir=args.tofu_dir,
                samples=args.samples,
                interval_ms=args.interval_ms,
                apply=args.apply,
                plan_only=args.plan_only,
                auto_teardown=args.auto_teardown,
                teardown_mode=args.teardown_mode,
                teardown_after_seconds=args.teardown_after_seconds,
                execution_timeout_seconds=args.execution_timeout_seconds,
                execution_handoff=args.execution_handoff,
                execution_lead_seconds=args.execution_lead_seconds,
                diagnostics_mode=args.diagnostics,
                diagnostics_s3_prefix=args.diagnostics_s3_prefix,
                require_public_ip=args.require_public_ip,
                ready_timeout_seconds=args.ready_timeout_seconds,
                runtime_ttl_seconds=args.runtime_ttl_seconds,
                allow_live=args.allow_live,
                live_capital_limit_usd=args.live_capital_limit_usd,
                kill_switch_file=args.kill_switch_file,
            )
        )
        print(f"Orchestration workspace: {result.work_dir}")
        print(f"Infrastructure recommendation: {result.infra_recommendation_path}")
        print(f"Runtime config: {result.runtime_config_path}")
        print(f"Runtime config S3 URI: {result.runtime_config_s3_uri}")
        print(f"OpenTofu variables: {result.tfvars_path}")
        if result.instance_id:
            print(f"Runtime instance: {result.instance_id}")
        if result.execution_status:
            print(f"Runtime execution status: {result.execution_status['status']}")
        if result.diagnostics_path:
            print(f"Diagnostics bundle: {result.diagnostics_path}")
            summary_path = result.diagnostics_path.parent / "latency-summary.json"
            if summary_path.exists():
                summary = _read_json(summary_path)
                print(
                    "Latency summary: "
                    f"{summary.get('status')} total_ms={summary.get('total_wall_clock_ms')} "
                    f"hot_path_ms={summary.get('hot_path_ms')} largest_gap={summary.get('largest_gap')}"
                )
        if result.diagnostics_s3_uri:
            print(f"Diagnostics S3 URI: {result.diagnostics_s3_uri}")
        if result.teardown_performed:
            print("Teardown completed")
        return 0

    env = load_kalshi_env_from_prefix(args.secrets_prefix) if args.secrets_prefix else None
    strategy = resolve_strategy_profile(args.strategy, env=env)
    results_prefix = args.results_s3_prefix or "s3://<results-bucket>/profiler"
    runtime_prefix = args.runtime_s3_prefix or "s3://<results-bucket>/runtime"
    print("Dry-run orchestration plan:")
    print(f"1. Profile strategy: {strategy.name}")
    print(
        ".venv/bin/python -m profiler.cli strategy profile "
        f"--strategy {args.strategy} --run-id {args.run_id} "
        f"--upload-s3-prefix {results_prefix}"
    )
    print("2. Build placement recommendation from uploaded results")
    print("3. Build policy-gated infrastructure recommendation with immutable Docker image resolution")
    print(
        ".venv/bin/python -m profiler.cli strategy infra-plan "
        f"--strategy {args.strategy} --recommendation recommendations/{strategy.name}-{args.run_id}-placement.json "
        f"--run-id {args.run_id} "
        f"--output recommendations/{strategy.name}-{args.run_id}-infra.json"
    )
    print("4. Generate and upload NullOne runtime config")
    print(
        ".venv/bin/python -m profiler.cli strategy runtime-config "
        f"--strategy {args.strategy} --infra-recommendation recommendations/{strategy.name}-{args.run_id}-infra.json "
        f"--output runtime/nullone/{args.run_id}.json --upload-s3-prefix {runtime_prefix} --run-id {args.run_id}"
    )
    print("5. Generate strategy runtime tfvars")
    secrets_arg = f" --secrets-prefix {args.secrets_prefix}" if args.secrets_prefix else ""
    print(
        ".venv/bin/python -m profiler.cli strategy tfvars "
        f"--infra-recommendation recommendations/{strategy.name}-{args.run_id}-infra.json "
        f"--runtime-config-s3 {runtime_prefix}/runs/{args.run_id}/combined/{args.run_id}.json "
        f"--output .generated/{args.run_id}/strategy-runtime.tfvars.json{secrets_arg}"
    )
    print("6. Deploy with the printed tofu apply command from strategy deploy")
    print("7. After execution, teardown the runtime host")
    print(f".venv/bin/python -m profiler.cli strategy teardown --run-id {args.run_id}")
    cleanup = (strategy.deployment.get("artifact", {}).get("ecr", {}).get("cleanup") or {})
    repository = strategy.deployment.get("artifact", {}).get("ecr", {}).get("repository")
    tag_prefix = strategy.deployment.get("artifact", {}).get("ecr", {}).get("tag_prefix")
    if repository:
        keep_last = cleanup.get("keep_last", 10)
        tag_prefix_arg = f" --tag-prefix {tag_prefix}" if tag_prefix else ""
        print("8. Review ECR image cleanup plan")
        print(
            ".venv/bin/python -m profiler.cli artifacts cleanup-plan "
            f"--repository {repository}{tag_prefix_arg} --keep-last {keep_last}"
        )
    return 0


def cmd_strategy_deploy(args: argparse.Namespace) -> int:
    infra_recommendation = _read_json(args.infra_recommendation)
    decision = infra_recommendation.get("policy_decision", {}).get("decision")
    if decision not in {"deploy", "deploy_fallback", "deploy_standbys"}:
        raise ValueError(f"Policy decision is {decision}; refusing to print deploy command")
    infra = infra_recommendation["recommended_infrastructure"]
    print(
        "Selected infrastructure: "
        f"{infra['provider']}/{infra['region']}/{infra.get('instance_type')} "
        f"runtime={infra.get('runtime')}"
    )
    print(f"Run: tofu -chdir={args.dir} apply -var-file={Path(args.tfvars).resolve()}")
    return 0


def cmd_strategy_teardown(args: argparse.Namespace) -> int:
    if args.yes:
        teardown_strategy_runtime(args.run_id, app_dir=Path.cwd(), yes=True)
        print(f"Teardown helper completed for run {args.run_id}")
        return 0

    print(f"Run: tofu -chdir={args.dir} destroy \\")
    print(f"  -var='run_id={args.run_id}' \\")
    print("  -var='strategy_runtime_enabled=true' \\")
    print("  -target=aws_instance.strategy_runtime \\")
    print("  -target=aws_iam_instance_profile.strategy_runtime \\")
    print("  -target=aws_iam_role_policy.strategy_runtime \\")
    print("  -target=aws_iam_role.strategy_runtime \\")
    print("  -target=aws_security_group.strategy_runtime")
    print("This destroys ephemeral strategy runtime resources and preserves profiler results.")
    return 0


def cmd_strategy_janitor(args: argparse.Namespace) -> int:
    actions = run_strategy_runtime_janitor(
        region=args.region,
        app_dir=Path(args.app_dir),
        dry_run=not args.yes,
        project_name=args.project_name,
    )
    print(json.dumps(actions, indent=2, sort_keys=True))
    return 0


def run_probes(
    targets: list,
    runners: list[Runner],
    *,
    samples: int,
    interval_ms: int,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for sample_index in range(samples):
        for runner in runners:
            for target in targets:
                if target.type == "http":
                    results.append(probe_http(target, runner))
                elif target.type == "websocket":
                    results.append(probe_websocket(target, runner))
                else:
                    result = ProbeResult.empty(
                        target=target.name,
                        target_type=target.type,
                        provider=runner.provider,
                        region=runner.region,
                        runner_id=runner.runner_id or f"{runner.provider}-{runner.region}",
                    )
                    result.error = f"Unsupported target type: {target.type}"
                    results.append(result)
        if sample_index < samples - 1:
            time.sleep(interval_ms / 1000)
    return results


def _read_inputs(inputs: list[str]) -> list[dict]:
    results: list[dict] = []
    for input_path in inputs:
        if is_s3_uri(input_path):
            results.extend(read_jsonl_from_s3(input_path))
            continue
        path = Path(input_path)
        if path.is_dir():
            for jsonl in sorted(path.glob("*.jsonl")):
                results.extend(read_jsonl(jsonl))
        else:
            results.extend(read_jsonl(path))
    return results


def _read_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _maybe_upload(output_path: str, args: argparse.Namespace) -> str | None:
    if args.upload_s3 and args.upload_s3_prefix:
        raise ValueError("Use only one of --upload-s3 or --upload-s3-prefix")
    if args.upload_s3:
        upload_file_to_s3(output_path, args.upload_s3)
        return args.upload_s3
    if args.upload_s3_prefix:
        run_id = args.run_id or _default_run_id()
        destination = build_s3_run_uri(args.upload_s3_prefix, run_id, Path(output_path).name)
        upload_file_to_s3(output_path, destination)
        return destination
    return None


def _samples_from_args(args: argparse.Namespace) -> int:
    if getattr(args, "duration", None):
        return max(1, int((_parse_duration_seconds(args.duration) * 1000) / args.interval_ms))
    return args.samples


def _default_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _parse_duration_seconds(value: str) -> int:
    if value.endswith("ms"):
        return max(1, int(value[:-2]) // 1000)
    if value.endswith("s"):
        return int(value[:-1])
    if value.endswith("m"):
        return int(value[:-1]) * 60
    return int(value)


def _check_nullone_cli() -> dict[str, str]:
    import shutil

    binary = os.environ.get("NULLONE_BIN", "nullone")
    path = shutil.which(binary)
    if path is None:
        return {"status": "failed", "error": f"{binary} not found on PATH"}
    return {"status": "ok", "binary": path}


def _add_secret_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--pem-file")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--region")


if __name__ == "__main__":
    raise SystemExit(main())
