"""Follows CloudFormation stack events and aggregates them per AWS service."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

SERVICES: list[tuple[str, tuple[str, ...]]] = [
    ("Network (VPC)", ("AWS::EC2::",)),
    ("Identity (Cognito)", ("AWS::Cognito::",)),
    ("Database (Aurora)", ("AWS::RDS::", "AWS::SecretsManager::")),
    ("Documents (DynamoDB)", ("AWS::DynamoDB::",)),
    ("Storage (S3)", ("AWS::S3::", "Custom::S3AutoDeleteObjects")),
    ("Compute (Lambda)", ("AWS::Lambda::",)),
    ("API (API Gateway)", ("AWS::ApiGatewayV2::",)),
    ("Discovery (Cloud Map)", ("AWS::ServiceDiscovery::", "AWS::Route53::")),
    ("IAM", ("AWS::IAM::",)),
    ("Observability", ("AWS::Logs::", "AWS::CloudWatch::", "AWS::SNS::")),
]
OTHER = "Other"
SKIP_TYPES = {"AWS::CDK::Metadata"}
HINTS = {"Database (Aurora)": "Aurora writer usually takes 8-12 min"}


def service_of(resource_type: str) -> str:
    for name, prefixes in SERVICES:
        if resource_type.startswith(prefixes):
            return name
    return OTHER


@dataclass
class ServiceState:
    name: str
    total: int = 0
    done: int = 0
    in_progress: int = 0
    failed: int = 0
    last: str = ""
    reason: str = ""
    started: float | None = None
    finished: float | None = None

    @property
    def status(self) -> str:
        if self.failed:
            return "FAILED"
        if self.total and self.done >= self.total:
            return "DONE"
        if self.in_progress or self.done:
            return "RUNNING"
        return "WAITING"

    def elapsed(self, now: float) -> float | None:
        if self.started is None:
            return None
        return (self.finished or now) - self.started


@dataclass
class _Resource:
    stack: str
    logical: str
    rtype: str
    status: str = "PENDING"
    reason: str = ""


@dataclass
class StackTracker:
    """`mode` is "deploy" (CREATE/UPDATE) or "delete"."""

    cfn: Any
    stacks: tuple[str, ...]
    mode: str = "deploy"
    since: datetime = field(default_factory=lambda: datetime.now(UTC))
    resources: dict[tuple[str, str], _Resource] = field(default_factory=dict)
    stack_status: dict[str, str] = field(default_factory=dict)
    _seen: set[str] = field(default_factory=set)
    _first: dict[str, float] = field(default_factory=dict)
    _done_at: dict[str, float] = field(default_factory=dict)

    def load_templates(self, cdk_out: Path) -> None:
        for stack in self.stacks:
            template = cdk_out / f"{stack}.template.json"
            if not template.exists():
                continue
            for logical, res in json.loads(template.read_text())["Resources"].items():
                if res["Type"] not in SKIP_TYPES:
                    self.resources[(stack, logical)] = _Resource(stack, logical, res["Type"])

    def load_existing(self) -> None:
        """Resources already in the stacks (updates, deletes) start from their current state."""
        for stack in self.stacks:
            try:
                current = self.cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
                self.stack_status[stack] = current
                pages = self.cfn.get_paginator("list_stack_resources").paginate(StackName=stack)
                for page in pages:
                    for r in page["StackResourceSummaries"]:
                        if r["ResourceType"] in SKIP_TYPES:
                            continue
                        key = (stack, r["LogicalResourceId"])
                        res = self.resources.setdefault(
                            key, _Resource(stack, r["LogicalResourceId"], r["ResourceType"])
                        )
                        res.status = "UNCHANGED" if self.mode == "deploy" else r["ResourceStatus"]
            except ClientError:
                self.stack_status.setdefault(stack, "NOT_FOUND")

    def poll(self) -> None:
        for stack in self.stacks:
            try:
                events = self.cfn.describe_stack_events(StackName=stack)["StackEvents"]
            except ClientError as exc:
                msg = str(exc)
                if "does not exist" in msg:
                    gone = self.mode == "delete" and stack in self.stack_status
                    if gone:
                        self.stack_status[stack] = "DELETE_COMPLETE"
                    else:
                        self.stack_status.setdefault(stack, "NOT_STARTED")
                    if gone:
                        self._mark_deleted(stack)
                continue
            for ev in reversed(events):  # oldest first
                if ev["EventId"] in self._seen or ev["Timestamp"] < self.since:
                    continue
                self._seen.add(ev["EventId"])
                logical = ev["LogicalResourceId"]
                if ev["ResourceType"] == "AWS::CloudFormation::Stack" and logical == stack:
                    self.stack_status[stack] = ev["ResourceStatus"]
                    if ev["ResourceStatus"] == "DELETE_COMPLETE":
                        self._mark_deleted(stack)
                    continue
                if ev["ResourceType"] in SKIP_TYPES:
                    continue
                res = self.resources.setdefault(
                    (stack, logical), _Resource(stack, logical, ev["ResourceType"])
                )
                res.status = ev["ResourceStatus"]
                res.reason = ev.get("ResourceStatusReason", "") or ""
                svc = service_of(res.rtype)
                self._first.setdefault(svc, time.time())

    def _mark_deleted(self, stack: str) -> None:
        """A deleted stack has no resources left, whether or not we saw every event."""
        for res in self.resources.values():
            if res.stack == stack and res.status not in {"DELETE_COMPLETE", "DELETE_SKIPPED"}:
                res.status = "DELETE_COMPLETE"

    def services(self) -> list[ServiceState]:
        states = {name: ServiceState(name) for name, _ in SERVICES}
        states[OTHER] = ServiceState(OTHER)
        latest: dict[str, _Resource] = {}
        for res in self.resources.values():
            st = states[service_of(res.rtype)]
            st.total += 1
            s = res.status
            if s.endswith("_FAILED"):
                st.failed += 1
                st.reason = st.reason or res.reason
            if self.mode == "delete":
                if s in {"DELETE_COMPLETE", "DELETE_SKIPPED"}:
                    st.done += 1
                elif s == "DELETE_IN_PROGRESS":
                    st.in_progress += 1
            elif s in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UNCHANGED", "IMPORT_COMPLETE"}:
                st.done += 1
            elif s.endswith("_IN_PROGRESS"):
                st.in_progress += 1
            if s not in {"PENDING", "UNCHANGED"}:
                latest[st.name] = res
        now = time.time()
        for st in states.values():
            st.started = self._first.get(st.name)
            if st.status == "DONE" and st.started is not None:
                st.finished = self._done_at.setdefault(st.name, now)
            else:
                self._done_at.pop(st.name, None)
            newest = latest.get(st.name)
            if newest:
                st.last = f"{newest.logical} {newest.status}"
        return [s for s in states.values() if s.total]

    def failed(self) -> bool:
        return any("ROLLBACK" in v or v.endswith("_FAILED") for v in self.stack_status.values())
