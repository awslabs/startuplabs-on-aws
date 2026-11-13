"""Network ports: private networks (VPC), service discovery (Cloud Map), DNS (Route 53),
load balancers (ELBv2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PrivateNetwork:
    vpc_id: str
    subnet_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int


class NetworkProvisioner(Protocol):
    def ensure_private_network(self, name: str, cidr: str, zones: int = 2) -> PrivateNetwork:
        """Idempotent: VPC tagged `Name=<name>` plus one private subnet per zone."""
        ...

    def ensure_security_group(self, vpc_id: str, name: str, description: str) -> str: ...

    def allow_from_group(self, group_id: str, source_group_id: str, port: int) -> None:
        """Ingress on `port` from members of `source_group_id` only. Never 0.0.0.0/0."""
        ...


class ServiceRegistry(Protocol):
    def ensure_namespace(self, name: str, vpc_id: str) -> str: ...
    def register(self, namespace: str, service: str, instance_id: str, ep: Endpoint) -> None: ...
    def discover(self, namespace: str, service: str) -> list[Endpoint]: ...


class Dns(Protocol):
    def ensure_private_zone(self, name: str, vpc_id: str) -> str: ...
    def upsert_a_record(self, zone_id: str, name: str, ip: str, ttl: int = 60) -> None: ...


class LoadBalancers(Protocol):
    def ensure_internal_alb(self, name: str, subnet_ids: list[str], security_group: str) -> str:
        """Returns the DNS name. Scheme is always `internal`."""
        ...
