from datetime import UTC, datetime

from profiler import janitor


def test_find_expired_strategy_runtimes(monkeypatch) -> None:
    client = _Ec2Client()
    monkeypatch.setattr(janitor, "_boto3_client", lambda service, region: client)

    candidates = janitor.find_expired_strategy_runtimes(
        region="us-east-1",
        now=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        project_name="source-latency-profiler",
    )

    assert [candidate.run_id for candidate in candidates] == ["old-run"]
    assert candidates[0].instance_id == "i-old"
    assert client.filters[-1] == {"Name": "tag:Project", "Values": ["source-latency-profiler"]}


class _Ec2Client:
    def __init__(self) -> None:
        self.filters = []

    def describe_instances(self, *, Filters):
        self.filters = Filters
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-old",
                            "State": {"Name": "running"},
                            "Tags": [
                                {"Key": "Component", "Value": "strategy-runtime"},
                                {"Key": "RunId", "Value": "old-run"},
                                {"Key": "Strategy", "Value": "nullone"},
                                {"Key": "ExpiresAt", "Value": "2026-05-30T11:59:00Z"},
                            ],
                        },
                        {
                            "InstanceId": "i-new",
                            "State": {"Name": "running"},
                            "Tags": [
                                {"Key": "Component", "Value": "strategy-runtime"},
                                {"Key": "RunId", "Value": "new-run"},
                                {"Key": "ExpiresAt", "Value": "2026-05-30T12:01:00Z"},
                            ],
                        },
                    ]
                }
            ]
        }
