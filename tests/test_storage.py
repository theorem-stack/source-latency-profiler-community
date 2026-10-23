import pytest

from profiler.storage import build_s3_run_uri, parse_s3_uri


def test_parse_s3_uri() -> None:
    assert parse_s3_uri("s3://bucket/runs/example.jsonl") == ("bucket", "runs/example.jsonl")


def test_parse_s3_uri_rejects_non_s3() -> None:
    with pytest.raises(ValueError):
        parse_s3_uri("https://example.com/file.jsonl")


def test_build_s3_run_uri_adds_run_partition() -> None:
    uri = build_s3_run_uri("s3://bucket/profiler", "run-123", "results.jsonl")

    assert uri == "s3://bucket/profiler/runs/run-123/combined/results.jsonl"


def test_build_s3_run_uri_handles_bucket_root() -> None:
    uri = build_s3_run_uri("s3://bucket", "run-123", "results.jsonl")

    assert uri == "s3://bucket/runs/run-123/combined/results.jsonl"
