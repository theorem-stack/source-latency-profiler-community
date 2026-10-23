from profiler.config import load_targets


def test_load_nullone_targets() -> None:
    targets = load_targets("targets/nullone.yaml")

    assert {target.name for target in targets} == {
        "eia_wngsr_json",
        "eia_wngsr_csv",
        "kalshi_status",
        "kalshi_ws",
    }
    assert targets[0].timeout_ms == 2000

