"""Dedicated VPC for the EKS cluster (network isolation, single NAT gateway).

``NetworkStack`` provisions a **new, dedicated** Amazon VPC that hosts the EKS
cluster created by ``ClusterStack`` (task 5). It exists as its own stack so the
network is a clean, self-contained unit with a small failure blast radius and a
predictable teardown order (design DD-2, "Stack decomposition").

Two resolved design decisions shape this stack:

* **Isolation (DD-2 / resolved decision 2).** The stack creates a brand-new VPC
  and *never* references, imports, or mutates the account's default VPC. Nothing
  in this module looks up an existing VPC — ``ec2.Vpc(...)`` always synthesizes a
  fresh one. This keeps the sample self-contained: deploying it cannot perturb
  other workloads sharing the default VPC, and ``cdk destroy`` removes the entire
  network without touching anything the Operator did not create with this app.

* **Cost — one NAT gateway.** A production-grade VPC would place one NAT gateway
  per Availability Zone for HA. This is a teaching sample, so it deliberately
  provisions a **single** NAT gateway shared across both AZs. The only recurring
  cost is that one NAT gateway (~$32/month + data processing) instead of one per
  AZ, at the expense of NAT-level AZ redundancy — an acceptable trade-off for a
  sample (resolved decision 2).

The created VPC is exposed as the public attribute :attr:`NetworkStack.vpc` so
``ClusterStack`` can consume it by object reference. Passing the L2 ``ec2.Vpc``
object across stacks lets CDK manage the cross-stack references (exported
subnet/VPC IDs, security groups, etc.) automatically — the Operator never wires
these by hand.

This module is intentionally *not* instantiated in ``app.py`` yet; the stacks are
composed together in task 10. It is written to be importable and synthesizable on
its own so it can be exercised with ``cdk synth`` in isolation.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack, Tags
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from .app_config import AppConfig

# ---------------------------------------------------------------------------
# Network sizing constants.
#
# Kept as module-level names (rather than magic numbers inline) so the cost and
# isolation decisions are self-documenting and easy to audit against the design.
# ---------------------------------------------------------------------------

# Spread across exactly two Availability Zones. Two AZs give the EKS control
# plane and node group basic AZ diversity while keeping the subnet/NAT footprint
# small for a sample (design DD-2: "2 AZs").
MAX_AZS = 2

# Exactly one NAT gateway shared by both private subnets, rather than one per AZ.
# This is the single cost-conscious knob called out in resolved decision 2.
NAT_GATEWAYS = 1

# CIDR mask for each subnet. /24 (256 addresses, minus the 5 AWS reserves) is
# comfortably large for a small managed node group plus load balancer ENIs while
# leaving room in the /16 VPC for future subnets.
SUBNET_CIDR_MASK = 24


class NetworkStack(Stack):
    """A dedicated 2-AZ VPC (single NAT gateway) for the EKS cluster.

    The VPC uses the standard EKS-friendly public + private-with-egress subnet
    layout:

    * **Public subnets** (one per AZ) host the shared NAT gateway and any
      internet-facing load balancers the cluster provisions. They are tagged so
      the AWS Load Balancer Controller can discover them for public (``elb``)
      load balancers.
    * **Private subnets with egress** (one per AZ) host the managed node group.
      Nodes reach the internet (image pulls, AWS APIs, the observability SaaS
      endpoints) *outbound only* through the single NAT gateway, and are never
      directly reachable from the internet. They are tagged for internal
      (``internal-elb``) load balancer discovery.

    Attributes:
        vpc: the created :class:`aws_cdk.aws_ec2.Vpc`. Exposed publicly so
            ``ClusterStack`` can consume it by object reference (cross-stack
            reference managed by CDK).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        **kwargs: Any,
    ) -> None:
        """Create the dedicated VPC.

        Args:
            scope: the parent construct (typically the CDK ``App``).
            construct_id: this stack's logical id within the app.
            config: the resolved :class:`AppConfig`. Accepted for a consistent
                constructor signature across all stacks (and so future
                network inputs can be threaded through) even though the VPC
                shape itself is fixed by the design's cost/isolation decisions.
            **kwargs: forwarded to :class:`aws_cdk.Stack` — notably ``env`` so the
                VPC is created in the Operator's resolved account/region.
        """
        super().__init__(scope, construct_id, **kwargs)

        # Retained for readability / future use; the VPC configuration below is
        # fixed by the design rather than driven by per-deploy inputs today.
        self._config = config

        # Create a NEW, dedicated VPC. This never references the account's
        # default VPC (design DD-2 / resolved decision 2): ``ec2.Vpc(...)`` always
        # provisions a fresh VPC, so the sample cannot perturb shared networking
        # and ``cdk destroy`` cleanly removes everything this stack created.
        self.vpc = ec2.Vpc(
            self,
            "AgentVpc",
            # Exactly two Availability Zones (design DD-2).
            max_azs=MAX_AZS,
            # A single shared NAT gateway instead of one per AZ — the one
            # cost-conscious trade-off in resolved decision 2.
            nat_gateways=NAT_GATEWAYS,
            # Standard EKS layout: public subnets for the NAT/load balancers and
            # private-with-egress subnets for the worker nodes.
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=SUBNET_CIDR_MASK,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    # PRIVATE_WITH_EGRESS: nodes get outbound internet via the
                    # NAT gateway but are not reachable inbound from the internet.
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=SUBNET_CIDR_MASK,
                ),
            ],
        )

        # EKS subnet discovery tags. The AWS Load Balancer Controller and EKS use
        # these tags to decide where to place internet-facing vs. internal load
        # balancers. They are advisory here (the cluster is created in a later
        # task) but tagging the subnets now keeps the network self-describing and
        # avoids a follow-up mutation of this stack.
        for subnet in self.vpc.public_subnets:
            # kubernetes.io/role/elb=1 marks a subnet as usable for public
            # (internet-facing) load balancers.
            self._tag_subnet(subnet, "kubernetes.io/role/elb", "1")
        for subnet in self.vpc.private_subnets:
            # kubernetes.io/role/internal-elb=1 marks a subnet as usable for
            # internal (private) load balancers.
            self._tag_subnet(subnet, "kubernetes.io/role/internal-elb", "1")

    @staticmethod
    def _tag_subnet(subnet: ec2.ISubnet, key: str, value: str) -> None:
        """Apply an EKS discovery tag to a subnet.

        Wrapped in a helper so the intent (EKS load-balancer subnet discovery) is
        explicit at each call site and the tagging mechanism lives in one place.
        """
        Tags.of(subnet).add(key, value)
