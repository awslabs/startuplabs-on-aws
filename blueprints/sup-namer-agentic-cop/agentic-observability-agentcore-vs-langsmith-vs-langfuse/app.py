#!/usr/bin/env python3
"""CDK application entrypoint for the Agentic Observability & Evaluations sample.

This app provisions everything the sample needs to run the LangGraph shopping
agent on Amazon EKS, instrumented for three observability platforms
(AgentCore Observability, LangSmith, and Langfuse):

  * NetworkStack        - dedicated VPC for the cluster
  * ClusterStack        - EKS cluster, node group, OIDC/IRSA, add-ons, image build
  * ObservabilityStack  - AgentCore log group + Transaction Search config
  * WorkloadStack       - scoped IAM, secrets, Deployment/Service

Deploy with a single command:  ``cdk deploy --all``
Tear down with:                ``cdk destroy --all``

Input resolution and fail-fast validation live in ``AppConfig.from_context``
(precedence: CDK context > env var > ``config.json`` > built-in default), so any
invalid or missing Deployment_Input raises here — before any stack is
synthesized (Requirements 5.6, 5.7, 11.3, 11.6; design Correctness Property 5).

Cross-stack wiring is by object reference: ``NetworkStack.vpc`` flows into
``ClusterStack``; ``ClusterStack`` (cluster + built image) and the
``ObservabilityStack`` flow into ``WorkloadStack``. The container image is built
in ``ClusterStack`` so it is co-located with the workload's Kubernetes manifests
(which attach to the cluster and therefore materialize in ClusterStack); this
keeps the Deployment's image reference intra-stack and avoids a cross-stack
export of the changing image tag. Every stack is given a concrete ``env``
(account + region) so account/region are resolved at synth time rather than
deferred (Requirements 1.1, 1.2, 11.2).
"""

import aws_cdk as cdk

from stacks.app_config import AppConfig
from stacks.cluster_stack import ClusterStack
from stacks.network_stack import NetworkStack
from stacks.observability_stack import ObservabilityStack
from stacks.workload_stack import WorkloadStack

app = cdk.App()

# Resolve + validate every Deployment_Input before any stack is created. This
# raises on invalid/missing inputs (fail-fast, before synth side effects).
cfg = AppConfig.from_context(app)

# Concrete account/region for all stacks so nothing is environment-agnostic at
# synth time (the values were validated above: 12-digit account, valid region).
env = cdk.Environment(account=cfg.account, region=cfg.region)

# NetworkStack — the dedicated VPC that hosts the cluster.
network = NetworkStack(app, "NetworkStack", config=cfg, env=env)

# ClusterStack — EKS + node group + OIDC/IRSA + cluster add-ons. Consumes the
# VPC by object reference (CDK manages the cross-stack references).
cluster = ClusterStack(app, "ClusterStack", config=cfg, vpc=network.vpc, env=env)

# ObservabilityStack — the account/region-level AgentCore log group and the
# Transaction Search configuration. Independent of the cluster.
observability = ObservabilityStack(
    app, "ObservabilityStack", config=cfg, env=env
)

# WorkloadStack — owns the scoped IAM, secrets, SecretProviderClass, and the
# Kubernetes Deployment/Service. Consumes the cluster (for IRSA + manifests), the
# ClusterStack (to adopt the image it builds and to order after its prerequisite
# Helm charts), and the ObservabilityStack (to scope the agent role's Logs grant
# to the log group).
workload = WorkloadStack(
    app,
    "WorkloadStack",
    config=cfg,
    cluster=cluster.cluster,
    cluster_stack=cluster,
    observability_stack=observability,
    env=env,
)

# Explicit stack-level dependencies.
#
# Direction note: the Agent_Workload's Kubernetes resources (the IRSA service
# account + role, the SecretProviderClass, the Deployment, and the Service) are
# attached to the cluster via ``cluster.add_service_account`` / ``add_manifest``,
# so CDK materializes them in the *ClusterStack* (the stack that owns the
# cluster), not in WorkloadStack. Those cluster-attached resources reference
# WorkloadStack (the Secrets Manager secret ARNs), ObservabilityStack (the
# AgentCore log-group ARN the agent role is scoped to), and NetworkStack (the
# VPC). The container image is built in ClusterStack, so the Deployment's image
# reference is intra-stack (no cross-stack image-tag export). The real dependency
# direction is therefore ClusterStack -> {NetworkStack, WorkloadStack,
# ObservabilityStack}. CDK already infers these edges from the object references;
# the explicit calls below simply make that deploy/teardown ordering unambiguous.
# Adding the edges in the opposite (design-mental-model) direction would create a
# dependency cycle, precisely because the K8s resources live in ClusterStack.
cluster.add_dependency(network)
cluster.add_dependency(workload)
cluster.add_dependency(observability)

app.synth()
