"""Compute adapters: Lambda (FunctionRuntime), ECS (ContainerRuntime), EC2 (MachineFleet)."""

from __future__ import annotations

import json
from typing import Any

from app.platform.adapters.aws.errors import translate
from app.platform.adapters.aws.session import AwsClientFactory
from app.platform.ports.compute import ContainerTask, InvocationResult, Machine


class LambdaFunctionRuntime:
    def __init__(self, clients: AwsClientFactory) -> None:
        self._lambda = clients.client("lambda")

    def invoke(self, function: str, payload: Any) -> InvocationResult:
        with translate():
            resp = self._lambda.invoke(
                FunctionName=function,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode(),
            )
        raw = resp["Payload"].read()
        return InvocationResult(
            status_code=int(resp["StatusCode"]),
            payload=json.loads(raw) if raw else None,
            function_error=resp.get("FunctionError"),
        )


class EcsContainerRuntime:
    def __init__(self, clients: AwsClientFactory) -> None:
        self._ecs = clients.client("ecs")

    def run_task(
        self,
        cluster: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
        overrides: dict[str, Any] | None = None,
    ) -> ContainerTask:
        with translate():
            resp = self._ecs.run_task(
                cluster=cluster,
                taskDefinition=task_definition,
                launchType="FARGATE",
                count=1,
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": subnets,
                        "securityGroups": security_groups,
                        "assignPublicIp": "DISABLED",
                    }
                },
                overrides=overrides or {},
            )
        task = resp["tasks"][0]
        return ContainerTask(task_arn=task["taskArn"], status=task["lastStatus"])

    def describe_task(self, cluster: str, task_arn: str) -> ContainerTask:
        with translate():
            task = self._ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
        return ContainerTask(task_arn=task["taskArn"], status=task["lastStatus"])


class Ec2MachineFleet:
    def __init__(self, clients: AwsClientFactory) -> None:
        self._ec2 = clients.client("ec2")

    def launch(
        self, image_id: str, instance_type: str, subnet_id: str, tags: dict[str, str]
    ) -> Machine:
        with translate():
            inst = self._ec2.run_instances(
                ImageId=image_id,
                InstanceType=instance_type,
                SubnetId=subnet_id,
                MinCount=1,
                MaxCount=1,
                MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                TagSpecifications=[
                    {
                        "ResourceType": "instance",
                        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
                    }
                ],
            )["Instances"][0]
        return self._machine(inst)

    def describe(self, instance_id: str) -> Machine:
        with translate():
            res = self._ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
        return self._machine(res[0]["Instances"][0])

    def terminate(self, instance_id: str) -> None:
        with translate():
            self._ec2.terminate_instances(InstanceIds=[instance_id])

    @staticmethod
    def _machine(inst: dict[str, Any]) -> Machine:
        return Machine(
            instance_id=inst["InstanceId"],
            state=inst["State"]["Name"],
            private_ip=inst.get("PrivateIpAddress"),
            tags={t["Key"]: t["Value"] for t in inst.get("Tags", [])},
        )
