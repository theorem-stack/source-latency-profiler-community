from datetime import datetime, timezone
from typing import Any

import pytest

from profiler.artifacts import artifact_with_resolved_image, plan_ecr_image_cleanup, resolve_ecr_image, validate_artifact, validate_container_image


def test_validate_container_image_accepts_immutable_ecr_tag() -> None:
    details = validate_container_image("123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:run-123")

    assert details["type"] == "container"
    assert details["account"] == "123456789012"
    assert details["region"] == "us-east-1"
    assert details["repository"] == "nullone"
    assert details["repository_uri"] == "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone"
    assert details["tag"] == "run-123"
    assert details["immutable"] is True


def test_validate_container_image_rejects_latest_by_default() -> None:
    with pytest.raises(ValueError, match="latest"):
        validate_container_image("123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:latest")


def test_validate_artifact_rejects_unsupported_type() -> None:
    with pytest.raises(ValueError, match="Unsupported artifact type"):
        validate_artifact({"type": "ami", "image": "ami-123"})


def test_resolve_ecr_image_selects_latest_matching_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("profiler.artifacts._ecr_client", lambda region: _FakeEcrClient())

    resolved = resolve_ecr_image(
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone",
        tag_prefix="nullone-runtime-",
    )

    assert resolved["source"] == "ecr_latest"
    assert resolved["selected"]["image"] == "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:nullone-runtime-new"


def test_resolve_ecr_image_uses_fallback_when_no_tags_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("profiler.artifacts._ecr_client", lambda region: _FakeEcrClient())

    resolved = resolve_ecr_image(
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone",
        tag_prefix="missing-",
        fallback_image="123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:fallback",
    )

    assert resolved["source"] == "fallback"
    assert resolved["selected"]["image"] == "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:fallback"


def test_resolve_ecr_image_uses_fallback_when_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("profiler.artifacts._ecr_client", lambda region: _FailingEcrClient())

    resolved = resolve_ecr_image(
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone",
        tag_prefix="nullone-runtime-",
        fallback_image="123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:fallback",
    )

    assert resolved["source"] == "fallback"
    assert resolved["selected"]["image"] == "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:fallback"
    assert "ECR image lookup failed" in resolved["reason"]


def test_artifact_with_resolved_image_updates_image_from_ecr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("profiler.artifacts._ecr_client", lambda region: _FakeEcrClient())

    artifact = artifact_with_resolved_image(
        {
            "type": "container",
            "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:fallback",
            "ecr": {
                "repository": "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone",
                "tag_prefix": "nullone-runtime-",
            },
        }
    )

    assert artifact["image"] == "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone:nullone-runtime-new"
    assert artifact["resolved"]["source"] == "ecr_latest"


def test_plan_ecr_image_cleanup_keeps_newest_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("profiler.artifacts._ecr_client", lambda region: _FakeEcrClient())

    plan = plan_ecr_image_cleanup(
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/nullone",
        tag_prefix="nullone-runtime-",
        keep_last=1,
    )

    assert [image["tag"] for image in plan["keep"]] == ["nullone-runtime-new"]
    assert [image["tag"] for image in plan["delete"]] == ["nullone-runtime-old"]


class _FakeEcrClient:
    def get_paginator(self, name: str) -> "_FakePaginator":
        assert name == "describe_images"
        return _FakePaginator()


class _FailingEcrClient:
    def get_paginator(self, name: str) -> "_FailingPaginator":
        assert name == "describe_images"
        return _FailingPaginator()


class _FailingPaginator:
    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ecr unavailable")


class _FakePaginator:
    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["registryId"] == "123456789012"
        assert kwargs["repositoryName"] == "nullone"
        return [
            {
                "imageDetails": [
                    {
                        "imageDigest": "sha256:old",
                        "imageTags": ["nullone-runtime-old", "latest"],
                        "imagePushedAt": datetime(2026, 5, 24, tzinfo=timezone.utc),
                    },
                    {
                        "imageDigest": "sha256:smoke",
                        "imageTags": ["wngsr-smoke-001"],
                        "imagePushedAt": datetime(2026, 5, 25, 12, tzinfo=timezone.utc),
                    },
                    {
                        "imageDigest": "sha256:new",
                        "imageTags": ["nullone-runtime-new"],
                        "imagePushedAt": datetime(2026, 5, 25, 13, tzinfo=timezone.utc),
                    },
                ]
            }
        ]
