from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from infra.rollout import Context, _diff_summary
from infra.settings import Deployment, output_id
from infra.tracker import StackTracker, service_of

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


class FakeCfn:
    def __init__(self) -> None:
        self.events: dict[str, list[dict[str, Any]]] = {}

    def add(self, stack: str, logical: str, rtype: str, status: str, reason: str = "") -> None:
        evs = self.events.setdefault(stack, [])
        evs.insert(  # CloudFormation returns newest first
            0,
            {
                "EventId": f"{stack}-{len(evs)}",
                "LogicalResourceId": logical,
                "ResourceType": rtype,
                "ResourceStatus": status,
                "ResourceStatusReason": reason,
                "Timestamp": T0 + timedelta(seconds=len(evs)),
            },
        )

    def describe_stack_events(self, StackName: str) -> dict[str, Any]:
        if StackName not in self.events:
            raise ClientError(
                {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                "DescribeStackEvents",
            )
        return {"StackEvents": self.events[StackName]}


def _tracker(tmp_path: Path, cfn: FakeCfn, mode: str = "deploy") -> StackTracker:
    template = {
        "Resources": {
            "Db": {"Type": "AWS::RDS::DBCluster"},
            "DbSecret": {"Type": "AWS::SecretsManager::Secret"},
            "Pool": {"Type": "AWS::Cognito::UserPool"},
            "Meta": {"Type": "AWS::CDK::Metadata"},
        }
    }
    (tmp_path / "s-platform.template.json").write_text(json.dumps(template))
    t = StackTracker(cfn, ("s-platform",), mode=mode, since=T0 - timedelta(seconds=1))
    t.load_templates(tmp_path)
    return t


def test_service_mapping() -> None:
    assert service_of("AWS::RDS::DBInstance") == "Database (Aurora)"
    assert service_of("AWS::ApiGatewayV2::Route") == "API (API Gateway)"
    assert service_of("Custom::S3AutoDeleteObjects") == "Storage (S3)"
    assert service_of("AWS::Foo::Bar") == "Other"


def test_deploy_progress_and_failure(tmp_path: Path) -> None:
    cfn = FakeCfn()
    t = _tracker(tmp_path, cfn)
    t.poll()
    assert t.stack_status["s-platform"] == "NOT_STARTED"
    assert {s.name: (s.status, s.total) for s in t.services()} == {
        "Identity (Cognito)": ("WAITING", 1),
        "Database (Aurora)": ("WAITING", 2),
    }
    cfn.add("s-platform", "s-platform", "AWS::CloudFormation::Stack", "CREATE_IN_PROGRESS")
    cfn.add("s-platform", "Pool", "AWS::Cognito::UserPool", "CREATE_COMPLETE")
    cfn.add("s-platform", "DbSecret", "AWS::SecretsManager::Secret", "CREATE_COMPLETE")
    cfn.add("s-platform", "Db", "AWS::RDS::DBCluster", "CREATE_IN_PROGRESS")
    t.poll()
    svc = {s.name: s for s in t.services()}
    assert svc["Identity (Cognito)"].status == "DONE"
    assert svc["Database (Aurora)"].status == "RUNNING"
    assert (svc["Database (Aurora)"].done, svc["Database (Aurora)"].total) == (1, 2)
    cfn.add("s-platform", "Db", "AWS::RDS::DBCluster", "CREATE_FAILED", "quota exceeded")
    cfn.add("s-platform", "s-platform", "AWS::CloudFormation::Stack", "ROLLBACK_IN_PROGRESS")
    t.poll()
    db = {s.name: s for s in t.services()}["Database (Aurora)"]
    assert db.status == "FAILED" and db.reason == "quota exceeded"
    assert t.failed()


def test_delete_progress(tmp_path: Path) -> None:
    cfn = FakeCfn()
    t = _tracker(tmp_path, cfn, mode="delete")
    t.stack_status["s-platform"] = "DELETE_IN_PROGRESS"
    cfn.add("s-platform", "Pool", "AWS::Cognito::UserPool", "DELETE_COMPLETE")
    t.poll()
    assert {s.name: s.status for s in t.services()}["Identity (Cognito)"] == "DONE"
    del cfn.events["s-platform"]  # stack gone
    t.poll()
    assert t.stack_status["s-platform"] == "DELETE_COMPLETE"
    assert all(s.status == "DONE" for s in t.services())


@pytest.mark.parametrize(
    ("stage", "alias", "arn", "prod"),
    [
        ("dev", "", "arn:aws:sts::1:assumed-role/Admin/me", False),
        ("prod", "", "arn:aws:sts::1:assumed-role/Admin/me", True),
        ("dev", "acme-prod", "arn:aws:sts::1:assumed-role/Admin/me", True),
        ("dev", "", "arn:aws:sts::1:assumed-role/ProdDeployer/me", True),
        ("dev", "", "arn:aws:sts::1:assumed-role/PRODAdmin/me", True),
        ("dev", "", "arn:aws:sts::1:assumed-role/deploy-prd/me", True),
        ("dev", "product-team", "arn:aws:sts::1:assumed-role/Admin/me", False),
    ],
)
def test_production_signals(stage: str, alias: str, arn: str, prod: bool) -> None:
    ctx = Context(Deployment("sup", stage), "us-east-1", account="1", arn=arn, alias=alias)
    assert bool(ctx.prod_like()) is prod


def test_diff_summary_flags_stateful_replacement(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text(
        "synth noise [-] AWS::RDS::DBCluster Old\n"
        "[+] AWS::Lambda::Function New\n"
        "[~] AWS::IAM::Role Role\n"
        "[~] AWS::RDS::DBCluster Database replace\n"
        "[-] AWS::DynamoDB::Table Profiles\n"
    )
    offset = len("synth noise [-] AWS::RDS::DBCluster Old\n")
    summary, risky = _diff_summary(str(log), offset)
    assert summary == "+1 ~2 -1 resources"
    assert len(risky) == 2


def test_stage_validation_and_names() -> None:
    d = Deployment("sup", "dev")
    assert d.stacks == ("sup-dev-platform", "sup-dev-api")
    assert output_id("assets_bucket") == "OutAssetsBucket"
    with pytest.raises(ValueError, match="lowercase"):
        Deployment("sup", "Dev-1")


def test_done_time_is_kept_and_stack_delete_event_completes_rows(tmp_path: Path) -> None:
    cfn = FakeCfn()
    t = _tracker(tmp_path, cfn, mode="delete")
    cfn.add("s-platform", "Pool", "AWS::Cognito::UserPool", "DELETE_COMPLETE")
    t.poll()
    first = {s.name: s.finished for s in t.services()}["Identity (Cognito)"]
    assert first is not None
    assert {s.name: s.finished for s in t.services()}["Identity (Cognito)"] == first
    cfn.add("s-platform", "s-platform", "AWS::CloudFormation::Stack", "DELETE_COMPLETE")
    t.poll()  # Db/DbSecret never emitted events, the stack event closes them
    assert all(s.status == "DONE" for s in t.services())
