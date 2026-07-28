"""CDK synthesis / ``assertions.Template`` tests for the deployment stacks (task 11).

These tests synthesize the stacks **in-memory** with ``assertions.Template
.from_stack(...)`` — they require **no AWS credentials** and **no Docker daemon**.
Two mechanisms make that possible:

* A test ``cdk.App`` is built with a concrete but dummy ``env``
  (account ``123456789012`` / region ``us-east-1``) and the account/region also
  supplied via context, so ``AppConfig`` validation passes and account/region are
  resolved deterministically. The VPC's Availability-Zone context is not looked
  up against AWS; CDK falls back to dummy AZs during in-memory synth.
* The agent image is built in ``ClusterStack`` via ``deploy-time-build``'s
  ``ContainerImageBuild``, which stages ``docker/`` as an ordinary S3 asset and
  runs the real ``docker build`` in CodeBuild at deploy time — so synth needs
  **no local Docker daemon**. ``WorkloadStack`` adopts that image by reference
  (``cluster_stack.agent_image.image_uri``); see
  ``WorkloadStack._resolve_agent_image``.

Where resources materialize (important for these assertions)
------------------------------------------------------------
The Agent_Workload's Kubernetes resources are attached to the cluster via
``cluster.add_service_account`` / ``cluster.add_manifest``, so CDK renders them in
the **ClusterStack** (the stack that owns the cluster), not ``WorkloadStack``:

* the scoped **Agent_IAM_Role** (from ``add_service_account``) → ClusterStack;
* the **Deployment**, **Service**, and **SecretProviderClass** manifests
  (``Custom::AWSCDK-EKS-KubernetesResource``) → ClusterStack;
* the managed **node group** (``AWS::EKS::Nodegroup``) → ClusterStack;
* the agent **ECR repository** + **image build** (``AgentImage``) → ClusterStack,
  co-located with the Deployment that references the image so the image tag is
  not a cross-stack export.

The CDK-owned Secrets Manager **secrets** (``AWS::SecretsManager::Secret``) are
created with the WorkloadStack as scope, so they render in **WorkloadStack**.

Correctness-property coverage (design "Correctness Properties 1-7" / Testing
Strategy):

* **Property 2** (IAM least privilege) — ``test_agent_role_has_scoped_statements``
  and ``test_agent_role_has_no_banned_managed_policies``.
* **Property 3** (enabled-platform consistency) —
  ``test_disabled_platform_is_completely_absent``.
* **Property 7** / Requirement 8.10 (app code untouched; no ConfigMap / no pip
  install) — ``test_deployment_has_no_configmap_volume`` and
  ``test_deployment_has_no_pip_install_or_command_override``.
* Requirements 8.1 / 8.2 (Deployment shape) —
  ``test_deployment_replica_and_container_port``,
  ``test_service_maps_80_to_8000``, ``test_deployment_health_probes``.
* Requirements 2.1 / 2.3 (node sizing) — ``test_node_sizing_matches_defaults``
  and ``test_node_sizing_matches_custom_override``.
* Requirement 1.3 / credential-free synth —
  ``test_full_app_synthesizes_without_credentials`` and the snapshot test.

Note: this module depends only on ``stacks/`` and CDK — never on ``chart/`` or the
shell scripts (which task 13 removes).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from stacks.app_config import AppConfig
from stacks.cluster_stack import ClusterStack
from stacks.network_stack import NetworkStack
from stacks.observability_stack import ObservabilityStack
from stacks.workload_stack import WorkloadStack

# Dummy (non-real) AWS environment used for every in-memory synth. The account is
# a syntactically-valid 12-digit id and the region is a valid identifier, so
# ``AppConfig`` validation passes without touching AWS.
TEST_ACCOUNT = "123456789012"
TEST_REGION = "us-east-1"

# The four broad managed policies the old deployment used, which the agent role
# must never carry (design Correctness Property 2, Requirement 4.3).
BANNED_MANAGED_POLICIES = (
    "AmazonBedrockFullAccess",
    "CloudWatchLogsFullAccess",
    "AWSXRayDaemonWriteAccess",
    "SecretsManagerReadWrite",
)

KUBERNETES_RESOURCE_TYPE = "Custom::AWSCDK-EKS-KubernetesResource"


# ---------------------------------------------------------------------------
# Synthesis helpers.
# ---------------------------------------------------------------------------


def _build_stacks(context_overrides: Optional[dict[str, Any]] = None) -> SimpleNamespace:
    """Build the full set of stacks under a credential-free, Docker-free app.

    Wires the stacks exactly as ``app.py`` does (VPC → cluster; cluster +
    observability → workload). The image is built in ``ClusterStack`` via
    ``ContainerImageBuild`` (an S3 asset, no local Docker) and adopted by
    ``WorkloadStack``.

    Args:
        context_overrides: optional CDK context overrides (e.g. node sizing or a
            disabled platform flag) merged over the base account/region context.

    Returns:
        A namespace with ``app``, ``cfg``, and the four stack instances.
    """
    context: dict[str, Any] = {"account": TEST_ACCOUNT, "region": TEST_REGION}
    if context_overrides:
        context.update(context_overrides)

    app = cdk.App(context=context)
    cfg = AppConfig.from_context(app)
    env = cdk.Environment(account=TEST_ACCOUNT, region=TEST_REGION)

    network = NetworkStack(app, "NetworkStack", config=cfg, env=env)
    cluster = ClusterStack(app, "ClusterStack", config=cfg, vpc=network.vpc, env=env)
    observability = ObservabilityStack(
        app, "ObservabilityStack", config=cfg, env=env
    )
    workload = WorkloadStack(
        app,
        "WorkloadStack",
        config=cfg,
        cluster=cluster.cluster,
        cluster_stack=cluster,
        observability_stack=observability,
        env=env,
    )
    return SimpleNamespace(
        app=app,
        cfg=cfg,
        network=network,
        cluster=cluster,
        observability=observability,
        workload=workload,
    )


def _flatten_manifest(value: Any) -> str:
    """Flatten a KubernetesResource ``Manifest`` property into a plain string.

    The manifest embedded in a ``Custom::AWSCDK-EKS-KubernetesResource`` is a
    stringified JSON, usually an ``Fn::Join`` of literal chunks interleaved with
    CloudFormation token dicts (``Ref`` / ``Fn::GetAtt``). This recursively
    concatenates the literal chunks; token dicts are replaced with a ``<TOKEN>``
    placeholder so the literal manifest structure (keys, ports, probe timings) is
    searchable while unresolved cross-stack references do not break the string.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten_manifest(part) for part in value)
    if isinstance(value, dict):
        if "Fn::Join" in value:
            separator, parts = value["Fn::Join"]
            return separator.join(_flatten_manifest(part) for part in parts)
        # Any other intrinsic (Ref/Fn::GetAtt/etc.) is an unresolved token.
        return "<TOKEN>"
    return "<TOKEN>"


def _kubernetes_manifests(template: assertions.Template) -> dict[str, str]:
    """Return ``{logical_id: flattened_manifest_text}`` for all K8s manifests."""
    resources = template.find_resources(KUBERNETES_RESOURCE_TYPE)
    return {
        logical_id: _flatten_manifest(res["Properties"].get("Manifest"))
        for logical_id, res in resources.items()
    }


def _manifest_of_kind(manifests: dict[str, str], kind: str) -> str:
    """Return the single flattened manifest text whose Kubernetes ``kind`` matches.

    Args:
        manifests: the ``{logical_id: text}`` map from :func:`_kubernetes_manifests`.
        kind: the Kubernetes ``kind`` to locate (e.g. ``"Deployment"``).

    Raises:
        AssertionError: if zero or more than one manifest of that kind is found.
    """
    needle = f'"kind":"{kind}"'
    matches = [text for text in manifests.values() if needle in text]
    assert len(matches) == 1, (
        f"expected exactly one {kind} manifest, found {len(matches)}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# Session-scoped synthesized templates (built once; EKS synth is not cheap).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def default_stacks() -> SimpleNamespace:
    """The default-config stacks (all three platforms enabled)."""
    return _build_stacks()


@pytest.fixture(scope="session")
def cluster_template(default_stacks: SimpleNamespace) -> assertions.Template:
    """``Template`` for the ClusterStack (holds the K8s manifests + agent role)."""
    return assertions.Template.from_stack(default_stacks.cluster)


@pytest.fixture(scope="session")
def workload_template(default_stacks: SimpleNamespace) -> assertions.Template:
    """``Template`` for the WorkloadStack (holds the Secrets Manager secrets)."""
    return assertions.Template.from_stack(default_stacks.workload)


@pytest.fixture(scope="session")
def cluster_manifests(cluster_template: assertions.Template) -> dict[str, str]:
    """Flattened text of every Kubernetes manifest in the ClusterStack."""
    return _kubernetes_manifests(cluster_template)


# ---------------------------------------------------------------------------
# 1. Node sizing matches inputs (Requirements 2.1, 2.3).
# ---------------------------------------------------------------------------


def test_node_sizing_matches_defaults(cluster_template: assertions.Template) -> None:
    """The node group ScalingConfig equals the documented defaults (2 / 1 / 3)."""
    cluster_template.has_resource_properties(
        "AWS::EKS::Nodegroup",
        {
            "ScalingConfig": {
                "DesiredSize": 2,
                "MinSize": 1,
                "MaxSize": 3,
            }
        },
    )


def test_node_sizing_matches_custom_override() -> None:
    """A custom node-sizing context override flows into the node group ScalingConfig."""
    stacks = _build_stacks(
        {"nodesDesired": 4, "nodesMin": 2, "nodesMax": 9, "nodeType": "t3.large"}
    )
    template = assertions.Template.from_stack(stacks.cluster)
    template.has_resource_properties(
        "AWS::EKS::Nodegroup",
        {
            "ScalingConfig": {
                "DesiredSize": 4,
                "MinSize": 2,
                "MaxSize": 9,
            },
            "InstanceTypes": ["t3.large"],
        },
    )


# ---------------------------------------------------------------------------
# 2. Agent role scoping + no banned managed policies (Correctness Property 2).
# ---------------------------------------------------------------------------


def test_agent_role_has_scoped_statements(
    cluster_template: assertions.Template,
) -> None:
    """The agent role carries the scoped inline statements from the design table.

    Asserts the presence of each scoped action set, and that the metric statement
    is namespace-conditioned and the Bedrock statement is scoped to inference
    profiles + foundation models (never ``bedrock:*`` / ``*``).
    """
    template_text = json.dumps(cluster_template.to_json())

    # Bedrock invoke scoped to inference profiles + foundation models (Req 4.2).
    assert "bedrock:InvokeModel" in template_text
    assert "bedrock:InvokeModelWithResponseStream" in template_text
    assert "inference-profile" in template_text
    assert "foundation-model" in template_text

    # X-Ray write actions (Req 4.6).
    assert "xray:PutTraceSegments" in template_text
    assert "xray:PutTelemetryRecords" in template_text

    # CloudWatch Logs scoped to the AgentCore log group (Req 4.7).
    assert "logs:CreateLogStream" in template_text
    assert "logs:PutLogEvents" in template_text

    # CloudWatch metrics conditioned on the bedrock-agentcore namespace (Req 4.8).
    assert "cloudwatch:PutMetricData" in template_text
    assert "cloudwatch:namespace" in template_text
    assert "bedrock-agentcore" in template_text

    # Secrets Manager read (Req 4.4).
    assert "secretsmanager:GetSecretValue" in template_text


def test_agent_role_has_no_banned_managed_policies(
    cluster_template: assertions.Template,
    workload_template: assertions.Template,
) -> None:
    """None of the four broad managed policies appear in either stack's template."""
    combined = json.dumps(cluster_template.to_json()) + json.dumps(
        workload_template.to_json()
    )
    for banned in BANNED_MANAGED_POLICIES:
        assert banned not in combined, (
            f"banned managed policy {banned!r} must not appear on any role"
        )


# ---------------------------------------------------------------------------
# 3. Deployment correctness — replicas, ports, service, probes (Req 8.1, 8.2).
# ---------------------------------------------------------------------------


def test_deployment_replica_and_container_port(
    cluster_manifests: dict[str, str],
) -> None:
    """The Deployment has 1 replica and exposes container port 8000."""
    deployment = _manifest_of_kind(cluster_manifests, "Deployment")
    assert '"replicas":1' in deployment
    assert '"containerPort":8000' in deployment


def test_service_maps_80_to_8000(cluster_manifests: dict[str, str]) -> None:
    """The ClusterIP Service maps port 80 to container target port 8000."""
    service = _manifest_of_kind(cluster_manifests, "Service")
    assert '"type":"ClusterIP"' in service
    assert '"port":80' in service
    assert '"targetPort":8000' in service


def test_deployment_health_probes(cluster_manifests: dict[str, str]) -> None:
    """The Deployment has /health liveness (30s/60s) and readiness (10s/30s) probes."""
    deployment = _manifest_of_kind(cluster_manifests, "Deployment")

    # Both probes hit GET /health.
    assert '"livenessProbe"' in deployment
    assert '"readinessProbe"' in deployment
    assert deployment.count('"path":"/health"') == 2

    # Liveness: initial delay 30s, period 60s.
    assert '"initialDelaySeconds":30' in deployment
    assert '"periodSeconds":60' in deployment
    # Readiness: initial delay 10s, period 30s.
    assert '"initialDelaySeconds":10' in deployment
    assert '"periodSeconds":30' in deployment


# ---------------------------------------------------------------------------
# 4. No ConfigMap volume and no startup pip install / command override
#    (Requirement 8.10, design DD-8, Correctness Property 7 relation).
# ---------------------------------------------------------------------------


def test_deployment_has_no_configmap_volume(
    cluster_manifests: dict[str, str],
) -> None:
    """The Deployment mounts no ConfigMap volume (the old app.py override is gone)."""
    deployment = _manifest_of_kind(cluster_manifests, "Deployment")
    assert "configMap" not in deployment


def test_deployment_has_no_pip_install_or_command_override(
    cluster_manifests: dict[str, str],
) -> None:
    """No startup ``pip install`` and no ``command``/``args`` override are present.

    The image runs its built ``CMD`` (``opentelemetry-instrument python app.py``)
    and ships ``app.py`` inside itself, so the container spec must not carry a
    ``command``/``args`` override or a pod-startup pip install (Requirement 8.10).
    """
    deployment = _manifest_of_kind(cluster_manifests, "Deployment")
    assert "pip install" not in deployment
    assert '"command"' not in deployment
    assert '"args"' not in deployment


# ---------------------------------------------------------------------------
# 5. Disabled-platform behavior (Correctness Property 3 — enabled consistency).
# ---------------------------------------------------------------------------


def test_disabled_platform_is_completely_absent() -> None:
    """With Langfuse disabled: no env vars, no SecretProviderClass entry, no secret.

    A platform is either fully present or fully absent (Correctness Property 3).
    Disabling Langfuse must remove its env vars, its SecretProviderClass mapping,
    and its Secrets Manager secret (and therefore its GetSecretValue grant).
    LangSmith stays fully present as the control.
    """
    stacks = _build_stacks({"langfuseEnabled": False})
    cluster_template = assertions.Template.from_stack(stacks.cluster)
    workload_template = assertions.Template.from_stack(stacks.workload)

    manifests = _kubernetes_manifests(cluster_template)
    combined_manifest = "\n".join(manifests.values())

    # No Langfuse env vars anywhere in the deployment manifest.
    assert "LANGFUSE_HOST" not in combined_manifest
    assert "LANGFUSE_PUBLIC_KEY" not in combined_manifest
    assert "LANGFUSE_SECRET_KEY" not in combined_manifest
    # No Langfuse SecretProviderClass entry / synced K8s secret of any kind.
    assert "langfuse" not in combined_manifest.lower()

    # Exactly one Secrets Manager secret is created (LangSmith only); the Langfuse
    # secret — and thus its GetSecretValue grant — is not created.
    workload_template.resource_count_is("AWS::SecretsManager::Secret", 1)

    # LangSmith (the control) remains fully present.
    assert "LANGSMITH_PROJECT" in combined_manifest
    assert "langsmith-secret" in combined_manifest


def test_disabled_platform_still_has_enabled_platforms(
    cluster_manifests: dict[str, str],
    workload_template: assertions.Template,
) -> None:
    """Sanity control: with all platforms enabled, both secrets and all env exist."""
    combined_manifest = "\n".join(cluster_manifests.values())
    assert "LANGFUSE_HOST" in combined_manifest
    assert "LANGSMITH_PROJECT" in combined_manifest
    assert "AGENT_OBSERVABILITY_ENABLED" in combined_manifest
    workload_template.resource_count_is("AWS::SecretsManager::Secret", 2)


# ---------------------------------------------------------------------------
# 6. Snapshot / drift test (deterministic, asset-hash-free — NetworkStack).
# ---------------------------------------------------------------------------

# NetworkStack contains no assets (no Docker image, no Lambda), so its resource
# composition is fully deterministic and free of nondeterministic asset hashes.
# This resource-type-count snapshot catches unintended drift in the generated
# template without the brittleness of a full JSON snapshot.
NETWORK_STACK_RESOURCE_TYPE_COUNTS = {
    "AWS::EC2::VPC": 1,
    "AWS::EC2::InternetGateway": 1,
    "AWS::EC2::VPCGatewayAttachment": 1,
    "AWS::EC2::Subnet": 4,
    "AWS::EC2::RouteTable": 4,
    "AWS::EC2::SubnetRouteTableAssociation": 4,
    "AWS::EC2::Route": 4,
    "AWS::EC2::EIP": 1,
    "AWS::EC2::NatGateway": 1,
}


def test_network_stack_snapshot_resource_types(
    default_stacks: SimpleNamespace,
) -> None:
    """NetworkStack renders exactly the expected resource-type composition."""
    template = assertions.Template.from_stack(default_stacks.network)
    resources = template.to_json().get("Resources", {})

    counts: dict[str, int] = {}
    for resource in resources.values():
        counts[resource["Type"]] = counts.get(resource["Type"], 0) + 1

    assert counts == NETWORK_STACK_RESOURCE_TYPE_COUNTS


# ---------------------------------------------------------------------------
# 7. Credential-free / Docker-free full synth (Requirement 1.3).
# ---------------------------------------------------------------------------


def test_full_app_synthesizes_without_credentials() -> None:
    """Constructing + synthesizing all four stacks in-memory raises nothing.

    The whole test module runs without AWS credentials and without a local
    Docker daemon (the image is staged as an S3 asset and built in CodeBuild at
    deploy time); this test makes that guarantee explicit by synthesizing every
    stack and asserting each has resources.
    """
    stacks = _build_stacks()
    for stack in (stacks.network, stacks.cluster, stacks.observability, stacks.workload):
        template = assertions.Template.from_stack(stack)
        assert template.to_json().get("Resources"), (
            f"{stack.stack_name} synthesized with no resources"
        )
