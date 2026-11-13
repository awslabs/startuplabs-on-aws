"""Network adapters: VPC (EC2), Cloud Map, Route 53 private zones, internal ALBs."""

from __future__ import annotations

import ipaddress
import time
import uuid
from typing import Any

from app.platform.adapters.aws.errors import translate
from app.platform.adapters.aws.session import AwsClientFactory
from app.platform.ports.network import Endpoint, PrivateNetwork
from app.shared import errors


def _tag(name: str) -> list[dict[str, Any]]:
    return [{"Key": "Name", "Value": name}]


class Ec2NetworkProvisioner:
    def __init__(self, clients: AwsClientFactory) -> None:
        self._ec2 = clients.client("ec2")
        self._region = clients.region

    def ensure_private_network(self, name: str, cidr: str, zones: int = 2) -> PrivateNetwork:
        with translate():
            found = self._ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [name]}])
            if found["Vpcs"]:
                vpc_id = found["Vpcs"][0]["VpcId"]
            else:
                vpc_id = self._ec2.create_vpc(
                    CidrBlock=cidr,
                    TagSpecifications=[{"ResourceType": "vpc", "Tags": _tag(name)}],
                )["Vpc"]["VpcId"]
            existing = self._ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
                "Subnets"
            ]
            subnets = {s["AvailabilityZone"]: s["SubnetId"] for s in existing}
            blocks = list(ipaddress.ip_network(cidr).subnets(new_prefix=24))
            for i in range(zones):
                az = f"{self._region}{'abcdef'[i]}"
                if az not in subnets:
                    subnets[az] = self._ec2.create_subnet(
                        VpcId=vpc_id,
                        CidrBlock=str(blocks[i]),
                        AvailabilityZone=az,
                        TagSpecifications=[
                            {"ResourceType": "subnet", "Tags": _tag(f"{name}-private-{az}")}
                        ],
                    )["Subnet"]["SubnetId"]
        return PrivateNetwork(vpc_id=vpc_id, subnet_ids=tuple(sorted(subnets.values())))

    def ensure_security_group(self, vpc_id: str, name: str, description: str) -> str:
        with translate():
            found = self._ec2.describe_security_groups(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "group-name", "Values": [name]},
                ]
            )["SecurityGroups"]
            if found:
                return str(found[0]["GroupId"])
            return str(
                self._ec2.create_security_group(
                    GroupName=name, Description=description, VpcId=vpc_id
                )["GroupId"]
            )

    def allow_from_group(self, group_id: str, source_group_id: str, port: int) -> None:
        try:
            with translate({"InvalidPermission.Duplicate": errors.Conflict}):
                self._ec2.authorize_security_group_ingress(
                    GroupId=group_id,
                    IpPermissions=[
                        {
                            "IpProtocol": "tcp",
                            "FromPort": port,
                            "ToPort": port,
                            "UserIdGroupPairs": [{"GroupId": source_group_id}],
                        }
                    ],
                )
        except errors.Conflict:
            pass  # idempotent


class CloudMapServiceRegistry:
    def __init__(self, clients: AwsClientFactory) -> None:
        self._sd = clients.client("servicediscovery")

    def ensure_namespace(self, name: str, vpc_id: str) -> str:
        with translate():
            for ns in self._sd.list_namespaces()["Namespaces"]:
                if ns["Name"] == name:
                    return str(ns["Id"])
            op = self._sd.create_private_dns_namespace(
                Name=name, Vpc=vpc_id, CreatorRequestId=str(uuid.uuid4())
            )["OperationId"]
            for _ in range(30):
                operation = self._sd.get_operation(OperationId=op)["Operation"]
                if operation["Status"] == "SUCCESS":
                    return str(operation["Targets"]["NAMESPACE"])
                if operation["Status"] == "FAIL":
                    raise errors.PlatformUnavailable(operation.get("ErrorMessage", "namespace"))
                time.sleep(1)
        raise errors.PlatformUnavailable("namespace creation timed out")

    def _service_id(self, namespace_id: str, service: str) -> str:
        with translate():
            services = self._sd.list_services(
                Filters=[{"Name": "NAMESPACE_ID", "Values": [namespace_id], "Condition": "EQ"}]
            )["Services"]
            for svc in services:
                if svc["Name"] == service:
                    return str(svc["Id"])
            return str(
                self._sd.create_service(
                    Name=service,
                    NamespaceId=namespace_id,
                    DnsConfig={"DnsRecords": [{"Type": "A", "TTL": 10}]},
                )["Service"]["Id"]
            )

    def register(self, namespace: str, service: str, instance_id: str, ep: Endpoint) -> None:
        namespace_id = next(
            (n["Id"] for n in self._sd.list_namespaces()["Namespaces"] if n["Name"] == namespace),
            None,
        )
        if namespace_id is None:
            raise errors.NotFound(f"namespace {namespace}")
        with translate():
            self._sd.register_instance(
                ServiceId=self._service_id(namespace_id, service),
                InstanceId=instance_id,
                Attributes={"AWS_INSTANCE_IPV4": ep.host, "AWS_INSTANCE_PORT": str(ep.port)},
            )

    def discover(self, namespace: str, service: str) -> list[Endpoint]:
        with translate():
            instances = self._sd.discover_instances(NamespaceName=namespace, ServiceName=service)[
                "Instances"
            ]
        return [
            Endpoint(
                i["Attributes"]["AWS_INSTANCE_IPV4"], int(i["Attributes"]["AWS_INSTANCE_PORT"])
            )
            for i in instances
        ]


class Route53Dns:
    def __init__(self, clients: AwsClientFactory) -> None:
        self._r53 = clients.client("route53")
        self._region = clients.region

    def ensure_private_zone(self, name: str, vpc_id: str) -> str:
        fqdn = name.rstrip(".") + "."
        with translate():
            for zone in self._r53.list_hosted_zones_by_name(DNSName=fqdn)["HostedZones"]:
                if zone["Name"] == fqdn and zone.get("Config", {}).get("PrivateZone"):
                    return str(zone["Id"])
            return str(
                self._r53.create_hosted_zone(
                    Name=fqdn,
                    CallerReference=str(uuid.uuid4()),
                    VPC={"VPCRegion": self._region, "VPCId": vpc_id},
                    HostedZoneConfig={"PrivateZone": True, "Comment": "managed by app"},
                )["HostedZone"]["Id"]
            )

    def upsert_a_record(self, zone_id: str, name: str, ip: str, ttl: int = 60) -> None:
        with translate():
            self._r53.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={
                    "Changes": [
                        {
                            "Action": "UPSERT",
                            "ResourceRecordSet": {
                                "Name": name,
                                "Type": "A",
                                "TTL": ttl,
                                "ResourceRecords": [{"Value": ip}],
                            },
                        }
                    ]
                },
            )


class Elbv2LoadBalancers:
    def __init__(self, clients: AwsClientFactory) -> None:
        self._elb = clients.client("elbv2")

    def ensure_internal_alb(self, name: str, subnet_ids: list[str], security_group: str) -> str:
        try:
            with translate({"LoadBalancerNotFound": errors.NotFound}):
                lbs = self._elb.describe_load_balancers(Names=[name])["LoadBalancers"]
                return str(lbs[0]["DNSName"])
        except errors.NotFound:
            pass
        with translate():
            lb = self._elb.create_load_balancer(
                Name=name,
                Subnets=subnet_ids,
                SecurityGroups=[security_group],
                Scheme="internal",
                Type="application",
            )["LoadBalancers"][0]
        return str(lb["DNSName"])
