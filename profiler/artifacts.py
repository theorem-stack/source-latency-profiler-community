from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ECR_IMAGE_RE = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9._/-]+):(?P<tag>[A-Za-z0-9_.-]+)$"
)
ECR_REPOSITORY_RE = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9._/-]+)$"
)


def validate_container_image(image: str, *, allow_latest: bool = False) -> dict[str, Any]:
    match = ECR_IMAGE_RE.match(image)
    if not match:
        raise ValueError(f"Unsupported container image URI: {image}")
    details = match.groupdict()
    if details["tag"] == "latest" and not allow_latest:
        raise ValueError("Refusing to use mutable 'latest' container tag")
    return {
        "type": "container",
        "image": image,
        **details,
        "repository_uri": repository_uri_from_image(image),
        "immutable": details["tag"] != "latest",
    }


def validate_artifact(artifact: dict[str, Any], *, allow_latest: bool = False) -> dict[str, Any]:
    artifact_type = artifact.get("type")
    if artifact_type != "container":
        raise ValueError(f"Unsupported artifact type: {artifact_type}")
    image = artifact.get("image")
    if not image:
        raise ValueError("Container artifact requires image")
    return validate_container_image(str(image), allow_latest=allow_latest)


def parse_ecr_repository_uri(repository_uri: str) -> dict[str, str]:
    match = ECR_REPOSITORY_RE.match(repository_uri)
    if not match:
        raise ValueError(f"Unsupported ECR repository URI: {repository_uri}")
    return match.groupdict()


def repository_uri_from_image(image: str) -> str:
    match = ECR_IMAGE_RE.match(image)
    if not match:
        raise ValueError(f"Unsupported container image URI: {image}")
    details = match.groupdict()
    return f"{details['account']}.dkr.ecr.{details['region']}.amazonaws.com/{details['repository']}"


def load_image_manifest(path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("artifact_type") != "container":
        raise ValueError(f"Unsupported manifest artifact_type: {manifest.get('artifact_type')}")
    validate_container_image(manifest["image"])
    return manifest


def resolve_ecr_image(
    repository_uri: str,
    *,
    tag_prefix: str | None = None,
    fallback_image: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    repo = parse_ecr_repository_uri(repository_uri)
    client = _ecr_client(region or repo["region"])
    try:
        candidates = _tagged_image_candidates(client, repo["account"], repo["repository"], repository_uri, tag_prefix=tag_prefix)
    except Exception as exc:
        if fallback_image:
            return {
                "source": "fallback",
                "selected": validate_container_image(fallback_image),
                "candidates": [],
                "reason": f"ECR image lookup failed: {exc}",
            }
        raise
    if candidates:
        selected = candidates[0]
        return {"source": "ecr_latest", "selected": selected, "candidates": candidates}
    if fallback_image:
        return {
            "source": "fallback",
            "selected": validate_container_image(fallback_image),
            "candidates": [],
            "reason": "No matching ECR images were found",
        }
    raise ValueError("No matching ECR images were found and no fallback image was provided")


def plan_ecr_image_cleanup(
    repository_uri: str,
    *,
    keep_last: int = 10,
    tag_prefix: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    if keep_last < 1:
        raise ValueError("keep_last must be >= 1")
    repo = parse_ecr_repository_uri(repository_uri)
    client = _ecr_client(region or repo["region"])
    candidates = _tagged_image_candidates(client, repo["account"], repo["repository"], repository_uri, tag_prefix=tag_prefix)
    keep = candidates[:keep_last]
    delete = candidates[keep_last:]
    return {
        "repository": repository_uri,
        "tag_prefix": tag_prefix,
        "keep_last": keep_last,
        "keep": keep,
        "delete": delete,
    }


def artifact_with_resolved_image(artifact: dict[str, Any], *, region: str | None = None) -> dict[str, Any]:
    if artifact.get("type") != "container":
        raise ValueError(f"Unsupported artifact type: {artifact.get('type')}")
    ecr_config = artifact.get("ecr") or {}
    repository_uri = ecr_config.get("repository")
    if not repository_uri:
        validate_artifact(artifact)
        return artifact
    fallback_image = ecr_config.get("fallback_image") or artifact.get("image")
    resolved = resolve_ecr_image(
        str(repository_uri),
        tag_prefix=ecr_config.get("tag_prefix"),
        fallback_image=fallback_image,
        region=region,
    )
    updated = dict(artifact)
    updated["image"] = resolved["selected"]["image"]
    updated["resolved"] = {
        "source": resolved["source"],
        "repository": repository_uri,
        "tag_prefix": ecr_config.get("tag_prefix"),
    }
    validate_artifact(updated)
    return updated


def _tagged_image_candidates(
    client: Any,
    registry_id: str,
    repository_name: str,
    repository_uri: str,
    *,
    tag_prefix: str | None,
) -> list[dict[str, Any]]:
    paginator = client.get_paginator("describe_images")
    candidates: list[dict[str, Any]] = []
    for page in paginator.paginate(
        registryId=registry_id,
        repositoryName=repository_name,
        filter={"tagStatus": "TAGGED"},
    ):
        for image in page.get("imageDetails", []):
            pushed_at = image.get("imagePushedAt")
            for tag in image.get("imageTags", []):
                if tag == "latest":
                    continue
                if tag_prefix and not tag.startswith(tag_prefix):
                    continue
                image_uri = f"{repository_uri}:{tag}"
                details = validate_container_image(image_uri)
                details.update(
                    {
                        "digest": image.get("imageDigest"),
                        "pushed_at": pushed_at.isoformat() if hasattr(pushed_at, "isoformat") else pushed_at,
                    }
                )
                candidates.append(details)
    return sorted(candidates, key=lambda candidate: str(candidate.get("pushed_at") or ""), reverse=True)


def _ecr_client(region: str) -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised only without aws extra.
        raise RuntimeError("Install AWS support with: python -m pip install -e '.[aws]'") from exc
    return boto3.client("ecr", region_name=region)
