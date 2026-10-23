from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse


def is_s3_uri(uri: str) -> bool:
    return uri.startswith("s3://")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def build_s3_run_uri(prefix: str, run_id: str, filename: str) -> str:
    bucket, key_prefix = parse_s3_uri(prefix)
    parts = [part.strip("/") for part in [key_prefix, "runs", run_id, "combined", filename] if part.strip("/")]
    return f"s3://{bucket}/{'/'.join(parts)}"


def upload_file_to_s3(local_path: str | Path, destination: str) -> None:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to enable S3 uploads") from exc

    bucket, key = parse_s3_uri(destination)
    boto3.client("s3").upload_file(str(local_path), bucket, key)


def read_jsonl_from_s3(source: str) -> list[dict]:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to enable S3 reads") from exc

    bucket, key = parse_s3_uri(source)
    client = boto3.client("s3")
    keys = [key] if key.endswith(".jsonl") else _list_jsonl_keys(client, bucket, key)

    results: list[dict] = []
    for object_key in keys:
        response = client.get_object(Bucket=bucket, Key=object_key)
        body = response["Body"].read().decode("utf-8")
        for line in body.splitlines():
            stripped = line.strip()
            if stripped:
                results.append(json.loads(stripped))
    return results


def download_s3_prefix(source: str, output_dir: str | Path) -> list[Path]:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install with the 'aws' extra to enable S3 downloads") from exc

    bucket, prefix = parse_s3_uri(source)
    client = boto3.client("s3")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    for key in _list_object_keys(client, bucket, prefix):
        relative = Path(key.removeprefix(prefix).lstrip("/")) if prefix else Path(key)
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket, key, str(destination))
        downloaded.append(destination)
    return downloaded


def _list_jsonl_keys(client, bucket: str, prefix: str) -> list[str]:
    return [key for key in _list_object_keys(client, bucket, prefix) if key.endswith(".jsonl")]


def _list_object_keys(client, bucket: str, prefix: str) -> list[str]:
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if not key.endswith("/"):
                keys.append(key)
    return keys
