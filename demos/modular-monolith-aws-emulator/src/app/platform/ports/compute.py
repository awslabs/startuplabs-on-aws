"""Compute ports: functions (Lambda), containers (ECS/Fargate), machines (EC2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class InvocationResult:
    status_code: int
    payload: Any
    function_error: str | None = None


@dataclass(frozen=True, slots=True)
class ContainerTask:
    task_arn: str
    status: str


@dataclass(frozen=True, slots=True)
class Machine:
    instance_id: str
    state: str
    private_ip: str | None = None
    tags: dict[str, str] = field(default_factory=dict)


class FunctionRuntime(Protocol):
    def invoke(self, function: str, payload: Any) -> InvocationResult: ...


class ContainerRuntime(Protocol):
    def run_task(
        self,
        cluster: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
        overrides: dict[str, Any] | None = None,
    ) -> ContainerTask: ...

    def describe_task(self, cluster: str, task_arn: str) -> ContainerTask: ...


class MachineFleet(Protocol):
    def launch(
        self, image_id: str, instance_type: str, subnet_id: str, tags: dict[str, str]
    ) -> Machine: ...

    def describe(self, instance_id: str) -> Machine: ...
    def terminate(self, instance_id: str) -> None: ...
