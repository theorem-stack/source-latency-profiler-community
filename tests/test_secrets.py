from pathlib import Path

from profiler.secrets import build_secret_plan, parse_env_file, secret_names_for_prefix


def test_parse_env_file_handles_quotes_and_comments(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "KALSHI_API_KEY=abc123",
                'NULLONE_MODE="paper"',
                "EMPTY_VALUE=",
                "QUOTED_HASH='value#not-comment'",
                "INLINE_COMMENT=value # comment",
            ]
        ),
        encoding="utf-8",
    )

    env = parse_env_file(env_file)

    assert env["KALSHI_API_KEY"] == "abc123"
    assert env["NULLONE_MODE"] == "paper"
    assert env["EMPTY_VALUE"] == ""
    assert env["QUOTED_HASH"] == "value#not-comment"
    assert env["INLINE_COMMENT"] == "value"


def test_build_secret_plan_redacts_values_and_sets_private_key_path(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    pem_file = tmp_path / "kalshi.pem"
    env_file.write_text(
        "KALSHI_API_KEY=abc123\nKALSHI_PRIVATE_KEY_PATH=/local/key.pem\nNULLONE_MODE=paper\n",
        encoding="utf-8",
    )
    # Built at runtime so the source file does not contain a private-key block.
    kind = "PRIVATE KEY"
    pem_header = f"-----BEGIN {kind}-----"
    pem_file.write_text(f"{pem_header}\nfixture-material\n-----END {kind}-----\n", encoding="utf-8")

    plan = build_secret_plan(env_file=env_file, pem_file=pem_file, prefix="nullone/prod")
    redacted = plan.redacted()

    assert plan.names == ["nullone/prod/runtime/env", "nullone/prod/kalshi/private_key_pem"]
    assert "KALSHI_API_KEY" in plan.env_keys
    assert all(secret["value"] == "<redacted>" for secret in redacted["secrets"])
    assert pem_header not in str(redacted)
    assert "fixture-material" not in str(redacted)
    runtime_secret = plan.specs[0].value
    assert '"/run/secrets/kalshi-private-key.pem"' in runtime_secret


def test_secret_names_for_prefix() -> None:
    assert secret_names_for_prefix("nullone/prod/") == [
        "nullone/prod/runtime/env",
        "nullone/prod/kalshi/private_key_pem",
    ]
