"""Agent workload stack — identity, secrets, and the Kubernetes Deployment.

``WorkloadStack`` is the final stack in the app (design DD-2, "Stack
decomposition"): it consumes the EKS cluster from ``ClusterStack`` and the
published image from the ``AgentImage`` construct, and it owns everything that is
specific to running the Agent_Workload — the CDK-owned Secrets Manager secrets,
the scoped Agent_IAM_Role + IRSA service account, the ``SecretProviderClass``,
and the Kubernetes ``Deployment`` + ``Service``.

This stack is built up across several sub-tasks. **This module implements tasks
8.1, 8.2, 8.3, and 9** — the CDK-owned Secrets Manager secrets, the scoped
Agent_IAM_Role + IRSA service account, the ``SecretProviderClass`` that syncs
those secrets into Kubernetes, and the Kubernetes ``Deployment`` + ``Service``
that run the Agent_Workload:

* **Task 8.1 (this module) — CDK-owned empty secrets.** For each *enabled*
  observability platform, create a ``secretsmanager.Secret`` with a deterministic
  name and an empty/placeholder JSON body (design DD-5). See
  :meth:`_create_platform_secrets`.
* **Task 8.2 (this module) — scoped Agent_IAM_Role + IRSA service account.**
  Uses ``cluster.add_service_account(...)`` and attaches inline, resource-scoped
  statements only — no broad managed policies (Bedrock invoke on the
  model/judge inference-profile *and* underlying foundation-model ARNs, X-Ray
  write, Logs on the AgentCore log group, metrics conditioned on the
  ``bedrock-agentcore`` namespace, and ``secretsmanager:GetSecretValue`` on
  exactly the ARNs of the enabled secrets created in task 8.1). See
  :meth:`_create_agent_role_and_service_account`. The created service account is
  exposed as :attr:`service_account` for tasks 8.3 and 9.
* **Task 8.3 (this module) — ``SecretProviderClass``.** Maps the Secrets Manager
  JSON keys of the secrets created here into synced Kubernetes Secrets
  (``langsmith-secret`` / ``langfuse-secret``) for the enabled platforms only,
  applied via ``cluster.add_manifest`` and ordered after the CSI driver + ASCP
  provider Helm charts (design DD-7). See :meth:`_create_secret_provider_class`.
  Exposed as :attr:`secret_provider_class` for task 9 ordering.
* **Task 9 (this module) — ``Deployment`` + ``Service``.** Applies a 1-replica
  Kubernetes ``Deployment`` (digest-pinned image, container port 8000, the IRSA
  service account, base + observability env vars, ``/health`` liveness/readiness
  probes, and — when a platform is enabled — the Secrets Store CSI volume) and a
  ``ClusterIP`` ``Service`` (port 80 → 8000), with **no** ``command``/``args``
  override and **no** ConfigMap volume so the image runs as built
  (Requirement 8.10, design DD-8). Ordered after the service account, the
  ``SecretProviderClass``, and the cluster prerequisite Helm charts; emits the
  port-forward/test command outputs (Requirement 8.9). See
  :meth:`_create_deployment_and_service`. Exposed as :attr:`deployment` /
  :attr:`service`.

Secret handling (design DD-5, "Secret handling")
------------------------------------------------
The critical invariant this module enforces: **no real secret value ever enters
CDK.** CDK creates the secret *resource* (so it owns the ARN natively for the
task 8.2 IAM grant and the task 8.3 ``SecretProviderClass`` — no lookup, no
hand-passed ARN and clean teardown), but the *value* is created empty. The
Operator populates the real credential once, after the first deploy, with
``aws secretsmanager put-secret-value`` (documented in the README, task 12).

To create a secret with the expected key *shape* but no real value, this uses a
:class:`~aws_cdk.aws_secretsmanager.SecretStringGenerator` whose
``secret_string_template`` contains the expected keys with empty-string values
and whose ``generate_string_key`` is a throwaway key
(:data:`PLACEHOLDER_GENERATE_KEY`). Secrets Manager (not CDK) fills that
throwaway key with a random value at create time, so:

* the placeholder JSON with the expected keys (``api-key`` / ``public-key`` /
  ``secret-key``) is present for the ``SecretProviderClass`` mapping, and
* no plaintext credential — and no credential-shaped literal — is ever written
  into CDK source, the CloudFormation template, a change set, or deploy output
  (Requirements 6.2, 6.3, 6.5; design Correctness Property 1).

The throwaway ``_unused`` key is ignored by the task 8.3 ``SecretProviderClass``
(which maps only the expected keys) and by the Operator's ``put-secret-value``
(which overwrites the whole JSON body with the real keys).

Removal policy (Requirements 6.x, 9.6)
--------------------------------------
The secrets are created with ``RemovalPolicy.RETAIN`` so that an accidental
``cdk destroy`` does not immediately shred a populated credential. This makes the
secrets a *retained* resource on teardown — documented per Requirement 9.6 (the
README, task 12, lists it and the manual step to remove it, e.g.
``aws secretsmanager delete-secret --secret-id <name>``).

Per-platform enable/disable (Requirement 6.6, 11.7)
---------------------------------------------------
A secret is created **only** for an enabled platform. If a platform is disabled,
no secret is created and there is nothing for the Operator to populate for it.
The created secret objects are exposed as :attr:`langsmith_secret` /
:attr:`langfuse_secret` (each ``None`` when its platform is disabled) so tasks
8.2 and 8.3 can reference their ARNs.

This module is intentionally *not* instantiated in ``app.py`` yet; the stacks are
composed together in task 10. It is written to be importable and synthesizable on
its own so it can be exercised with ``cdk synth`` in isolation.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from aws_cdk import ArnFormat, CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from .agent_image import AgentImage
from .app_config import AppConfig
from .observability_config import (
    AGENTCORE_METRIC_NAMESPACE,
    LANGFUSE_K8S_SECRET_NAME,
    LANGFUSE_PUBLIC_KEY_SECRET_KEY,
    LANGFUSE_SECRET_KEY_SECRET_KEY,
    LANGSMITH_API_KEY_SECRET_KEY,
    LANGSMITH_K8S_SECRET_NAME,
    agentcore_env,
    langfuse_env,
    langsmith_env,
)

# ---------------------------------------------------------------------------
# Secret placeholder templates (design DD-5).
#
# These are the *key shapes* CDK stamps into each secret at create time — every
# value is an empty string, so no credential material is present. The keys match
# exactly what the task 8.3 ``SecretProviderClass`` maps and what the pod's
# ``secretKeyRef``s (via observability_config) expect, so the Operator's later
# ``put-secret-value`` lands the real values under the right keys.
#
#   LangSmith: a single API key.
#   Langfuse:  a public/secret key pair.
# ---------------------------------------------------------------------------
LANGSMITH_SECRET_TEMPLATE: dict[str, str] = {"api-key": ""}
LANGFUSE_SECRET_TEMPLATE: dict[str, str] = {"public-key": "", "secret-key": ""}

# Throwaway key used only to satisfy ``SecretStringGenerator``'s requirement that
# a template be paired with a generated key. Secrets Manager fills this key with a
# random value server-side; it is NOT one of the expected keys, so the task 8.3
# ``SecretProviderClass`` never maps it and the Operator's ``put-secret-value``
# overwrites the whole body without it. Its only purpose is to let CDK create the
# secret with the placeholder template while keeping all real keys empty and
# keeping any credential-shaped literal out of the template.
PLACEHOLDER_GENERATE_KEY = "_unused"

# ---------------------------------------------------------------------------
# Agent service account / IRSA constants (task 8.2).
# ---------------------------------------------------------------------------

# Kubernetes namespace the Agent_Service_Account lives in. ``default`` matches
# the namespace the old Helm chart deployed into (its SA was
# ``langgraph-shopping-agent-sa`` in ``default``), so port-forward/test commands
# and the Deployment (task 9) reference the same namespace.
AGENT_SERVICE_ACCOUNT_NAMESPACE = "default"

# Suffix appended to the agent name to form the deterministic service-account
# name (e.g. ``langgraph-shopping-agent`` -> ``langgraph-shopping-agent-sa``),
# matching the old chart's ``langgraph-shopping-agent-sa``.
AGENT_SERVICE_ACCOUNT_SUFFIX = "-sa"

# ---------------------------------------------------------------------------
# SecretProviderClass constants (task 8.3, design "SecretProviderClass (synced
# Kubernetes Secret)" data model).
#
# The AWS Secrets Store CSI Driver reads a ``SecretProviderClass`` custom
# resource (``secrets-store.csi.x-k8s.io/v1``) to know which AWS Secrets Manager
# secrets to pull and how to sync them into native Kubernetes Secrets. We render
# exactly the enabled platforms' entries (Requirement 11.7) into a single CR.
# ---------------------------------------------------------------------------

# Custom-resource group/version and kind for the Secrets Store CSI Driver's
# SecretProviderClass, and the provider that fetches the objects. ``aws`` selects
# the ASCP provider installed by ClusterStack (design DD-5 / DD-7).
SECRET_PROVIDER_CLASS_API_VERSION = "secrets-store.csi.x-k8s.io/v1"  # nosec B105 - K8s apiVersion string, not a secret value  # pragma: allowlist secret
SECRET_PROVIDER_CLASS_KIND = "SecretProviderClass"  # nosec B105 - K8s kind string, not a secret value  # pragma: allowlist secret
SECRET_PROVIDER = "aws"  # nosec B105 - CSI provider name, not a secret value  # pragma: allowlist secret

# Metadata name of the single SecretProviderClass (design data model). Lives in
# the same namespace as the Agent_Service_Account / Deployment so the workload
# (task 9) can reference it.
SECRET_PROVIDER_CLASS_NAME = "agent-observability-secrets"  # nosec B105 - K8s resource name, not a secret value  # pragma: allowlist secret

# ``objectType`` for AWS Secrets Manager entries in the CSI ``objects`` list.
SECRETSMANAGER_OBJECT_TYPE = "secretsmanager"  # nosec B105 - CSI objectType literal, not a secret value  # pragma: allowlist secret

# Object aliases used *inside* the SecretProviderClass to name the values the
# CSI driver extracts (via jmesPath) from each Secrets Manager JSON secret. These
# aliases are then mapped into the synced Kubernetes Secret keys by
# ``secretObjects``. They are internal to this CR (not seen by the pod), so they
# are namespaced per-platform to stay unambiguous when both platforms are enabled.
LANGSMITH_API_KEY_OBJECT_ALIAS = "langsmith-api-key"  # nosec B105 - CSI object alias, not a secret value  # pragma: allowlist secret
LANGFUSE_PUBLIC_KEY_OBJECT_ALIAS = "langfuse-public-key"  # nosec B105 - CSI object alias, not a secret value  # pragma: allowlist secret
LANGFUSE_SECRET_KEY_OBJECT_ALIAS = "langfuse-secret-key"  # nosec B105 - CSI object alias, not a secret value  # pragma: allowlist secret

# ---------------------------------------------------------------------------
# Deployment / Service constants (task 9, Requirements 8.1, 8.2, 8.9, 8.10).
#
# These mirror the shape of the old ``chart/values.yaml`` / templates so the
# running pod sees the same ports and probes it always has — the only thing that
# changes is that the manifest is now generated declaratively by CDK.
# ---------------------------------------------------------------------------

# Kubernetes API groups/kinds for the Deployment and Service manifests.
DEPLOYMENT_API_VERSION = "apps/v1"
DEPLOYMENT_KIND = "Deployment"
SERVICE_API_VERSION = "v1"
SERVICE_KIND = "Service"

# 1 replica by default (Requirement 8.1), matching the old chart's
# ``replicaCount: 1``.
DEPLOYMENT_REPLICAS = 1

# Container port the agent's HTTP server listens on, and the named port used by
# the probes and the Service ``targetPort`` (Requirement 8.1). Matches the old
# chart's ``service.targetPort: 8000``.
CONTAINER_PORT = 8000
CONTAINER_PORT_NAME = "http"

# ClusterIP Service exposing port 80 -> container port 8000 (Requirement 8.1),
# matching the old chart's ``service.type: ClusterIP`` / ``service.port: 80``.
SERVICE_TYPE = "ClusterIP"
SERVICE_PORT = 80

# Health-check endpoint and probe timings (Requirement 8.2). These are the exact
# values from the old chart's ``livenessProbe`` / ``readinessProbe`` blocks.
HEALTH_CHECK_PATH = "/health"
LIVENESS_INITIAL_DELAY_SECONDS = 30
LIVENESS_PERIOD_SECONDS = 60
READINESS_INITIAL_DELAY_SECONDS = 10
READINESS_PERIOD_SECONDS = 30

# Base runtime (non-observability) env vars injected into the container (design
# "Deployment env var shape"). ``AWS_REGION``/``MODEL_ID`` are derived from
# AppConfig; the rest are the fixed runtime knobs the old chart carried. The
# three observability blocks (agentcore/langsmith/langfuse) are concatenated
# after these by :meth:`WorkloadStack._container_env`.
BASE_ENV_MAX_TOKENS = "2000"
BASE_ENV_PORT = str(CONTAINER_PORT)
# Note: TEMPERATURE is intentionally not injected. Newer Claude models
# (e.g. Sonnet 5) reject a `temperature` parameter with a ValidationException;
# the app only sends one when the TEMPERATURE env var is explicitly set, which
# lets operators opt in for older models without breaking the default model.

# Secrets Store CSI Driver volume that mounts the SecretProviderClass into the
# pod. Mounting the CSI volume is what triggers the driver to fetch the AWS
# Secrets Manager objects and sync the ``secretKeyRef``-backed Kubernetes Secrets
# (design DD-5). Only added when at least one platform is enabled (i.e.
# ``self.secret_provider_class`` is not ``None``).
CSI_VOLUME_NAME = "secrets-store"
CSI_DRIVER_NAME = "secrets-store.csi.k8s.io"
CSI_MOUNT_PATH = "/mnt/secrets-store"  # noqa: S108 - in-pod mount, not host tmp

# Standard Kubernetes recommended labels used consistently across the Deployment
# selector, the pod template, and the Service selector (Requirement 11.5). Both
# are keyed to the agent name so every workload resource is attributable to it.
LABEL_NAME_KEY = "app.kubernetes.io/name"
LABEL_INSTANCE_KEY = "app.kubernetes.io/instance"

# Bedrock cross-region inference-profile geo prefixes. A model id such as
# ``us.anthropic.claude-sonnet-5`` is a *cross-region inference
# profile* whose leading ``us.`` is a geo-routing prefix, not part of the
# underlying foundation-model id. Invoking an inference profile requires
# permission on BOTH the profile ARN and the foundation-model ARNs the profile
# routes to, so we strip a recognized geo prefix to recover the base
# foundation-model id (see :meth:`WorkloadStack._bedrock_resource_arns`).
#
# Only these *known* geo prefixes are stripped — model providers (``anthropic.``,
# ``amazon.``, ``meta.`` …) are longer, dot-delimited vendor tokens and are left
# intact. If a plain foundation-model id (no geo prefix) is supplied, nothing is
# stripped and the id is used as-is. This list mirrors the Bedrock cross-region
# inference geo codes; extend it if AWS adds new geographies.
BEDROCK_CROSS_REGION_PREFIXES: tuple[str, ...] = (
    "us-gov.",
    "us.",
    "eu.",
    "apac.",
    "jp.",
    "au.",
    "ca.",
    "sa.",
    "me.",
    "af.",
    "il.",
    "in.",
    "kr.",
    "sg.",
)


class WorkloadStack(Stack):
    """Agent workload: CDK-owned secrets today; IAM/SA, CSI, and Deployment later.

    Task 8.1 scope (implemented here): for each enabled observability platform,
    create a CDK-owned, empty ``secretsmanager.Secret`` with a deterministic name
    and ``RETAIN`` removal policy (design DD-5, Requirements 6.1, 6.2, 6.3, 6.5,
    6.6, 9.6).

    Attributes:
        langsmith_secret: the CDK-owned LangSmith :class:`~aws_cdk.aws_secretsmanager.Secret`
            (named ``config.langsmith_secret_name``), or ``None`` when LangSmith is
            disabled. Exposed so task 8.2 can scope the agent role's
            ``secretsmanager:GetSecretValue`` grant to its ARN and task 8.3 can map
            its ``api-key`` into the synced ``langsmith-secret`` Kubernetes Secret.
        langfuse_secret: the CDK-owned Langfuse
            :class:`~aws_cdk.aws_secretsmanager.Secret` (named
            ``config.langfuse_secret_name``), or ``None`` when Langfuse is disabled.
            Exposed for the same reasons (its ``public-key`` / ``secret-key``).
        secrets_encryption_key: the customer-managed :class:`~aws_cdk.aws_kms.Key`
            (CMK) that encrypts the platform secrets at rest (Checkov CKV_AWS_149),
            shared by both secrets and created only when at least one platform is
            enabled; ``None`` when every platform is disabled. The agent role is
            granted ``kms:Decrypt`` on it (task 8.2) so Secrets Manager can decrypt
            on the pod's behalf.
        service_account: the IRSA :class:`~aws_cdk.aws_eks.ServiceAccount`
            (``<agent>-sa`` in the ``default`` namespace) whose backing
            Agent_IAM_Role carries the scoped inline permissions (task 8.2), or
            ``None`` when synthesized in isolation without a cluster. Exposed so
            task 8.3 (``SecretProviderClass``) and task 9 (``Deployment``) can
            reference the service-account name/role. ``app.py`` (task 10) always
            passes a real cluster, so it is non-``None`` in a wired deploy.
        secret_provider_class: the ``SecretProviderClass`` Kubernetes manifest
            (:class:`~aws_cdk.aws_eks.KubernetesManifest`) that maps the enabled
            platforms' Secrets Manager JSON keys into synced Kubernetes Secrets
            (``langsmith-secret`` / ``langfuse-secret``) for the pod's
            ``secretKeyRef``s (task 8.3). ``None`` when synthesized without a
            cluster, or when **both** platforms are disabled (nothing to sync).
            Exposed so task 9 (``Deployment``) can order itself after the secrets
            are synced.
        deployment: the Kubernetes ``Deployment`` manifest
            (:class:`~aws_cdk.aws_eks.KubernetesManifest`) that runs the
            Agent_Workload (task 9) — 1 replica, container port 8000, the
            digest-pinned image, the IRSA service account, the base + observability
            env vars, the ``/health`` probes, and (when a platform is enabled) the
            Secrets Store CSI volume. ``None`` when synthesized without a cluster
            or image URI (isolated synth).
        service: the ``ClusterIP`` Kubernetes ``Service`` manifest
            (:class:`~aws_cdk.aws_eks.KubernetesManifest`) mapping port 80 to the
            container's port 8000 (task 9). ``None`` under the same isolated-synth
            guard as :attr:`deployment`.
    """

    langsmith_secret: Optional[secretsmanager.Secret]
    langfuse_secret: Optional[secretsmanager.Secret]
    secrets_encryption_key: Optional[kms.Key]
    service_account: Optional[Any]
    secret_provider_class: Optional[Any]
    agent_image: Optional[AgentImage]
    deployment: Optional[Any]
    service: Optional[Any]

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        cluster: Any = None,
        image_uri: Optional[str] = None,
        cluster_stack: Any = None,
        observability_stack: Any = None,
        **kwargs: Any,
    ) -> None:
        """Create the CDK-owned Secrets Manager secrets for enabled platforms.

        The constructor signature is deliberately the *full* signature the
        workload stack needs across tasks 8.2, 8.3, and 9, so those tasks can
        extend the body without changing how ``app.py`` (task 10) instantiates
        this stack. For task 8.1 only ``config`` is used; the remaining
        parameters are accepted now (defaulting to ``None``) and stored for the
        later tasks, keeping the stack importable and synthesizable in isolation
        until the cluster/image are wired in.

        Args:
            scope: the parent construct (typically the CDK ``App``).
            construct_id: this stack's logical id within the app.
            config: the resolved :class:`AppConfig`. Task 8.1 reads the per-platform
                ``*_enabled`` flags and the deterministic ``*_secret_name`` values
                from it; it was already validated fail-fast in ``AppConfig``, so
                this stack can trust it.
            cluster: the EKS cluster from ``ClusterStack`` (``eks.ICluster``).
                **Unused in task 8.1**; consumed by task 8.2 (IRSA service account)
                and task 9 (Deployment/Service). Stored as ``self._cluster``.
            image_uri: an optional pre-published, digest-pinned image URI. When
                supplied it is used verbatim. When ``None`` (the ``app.py`` wiring),
                this stack adopts the image built by ``ClusterStack``
                (``cluster_stack.agent_image``) — the image is built there so it is
                co-located with the Deployment/Service manifests that materialize
                in the cluster-owning stack, avoiding a cross-stack export of the
                changing image tag. Stored as ``self._image_uri``. See
                :meth:`_resolve_agent_image`.
            cluster_stack: the ``ClusterStack`` instance, passed by reference so
                this stack can (a) adopt the image it builds
                (``cluster_stack.agent_image``) and (b) order the workload after
                the cluster's prerequisite Helm charts (cert-manager, CSI driver,
                ASCP provider) via ``node.add_dependency`` (design DD-7). Stored as
                ``self._cluster_stack``.
            observability_stack: the ``ObservabilityStack`` instance, passed by
                reference so task 8.2 can scope the agent role's Logs permissions to
                ``observability_stack.log_group_arn`` (Requirement 4.7). **Unused in
                task 8.1**; stored as ``self._observability_stack``.
            **kwargs: forwarded to :class:`aws_cdk.Stack` — notably ``env`` so the
                secrets are created in the Operator's resolved account/region.
        """
        super().__init__(scope, construct_id, **kwargs)

        self._config = config
        # Stored for later tasks (8.2 / 8.3 / 9). Unused in task 8.1, but held
        # here so the constructor signature is stable across the sub-tasks.
        self._cluster = cluster
        self._image_uri = image_uri
        self._cluster_stack = cluster_stack
        self._observability_stack = observability_stack

        # The AgentImage this stack references. Normally adopted from
        # ``ClusterStack`` (where the image is built, co-located with the
        # Deployment manifest). Stays ``None`` when a pre-published image URI was
        # supplied or when synthesized in isolation without a cluster.
        self.agent_image = None

        # Task 8.1 — CDK-owned, empty Secrets Manager secrets (design DD-5).
        self._create_platform_secrets()

        # Task 8.2 — scoped Agent_IAM_Role + IRSA service account. Must run after
        # ``_create_platform_secrets`` so the Secrets Manager grant can reference
        # the (enabled) secrets created above.
        self._create_agent_role_and_service_account()

        # Task 8.3 — SecretProviderClass (enabled platforms only). Maps the
        # Secrets Manager JSON keys of the secrets created above into synced
        # Kubernetes Secrets (``langsmith-secret`` / ``langfuse-secret``) the
        # Deployment (task 9) consumes via ``secretKeyRef``.
        self._create_secret_provider_class()

        # Resolve the image URI the Deployment references — adopted from
        # ClusterStack (where it is built, co-located with the Deployment
        # manifest) unless a pre-published URI was supplied. Must run before
        # ``_create_deployment_and_service`` so the Deployment has a concrete URI.
        self._resolve_agent_image()

        # Task 9 — Deployment + Service. References ``self._image_uri``, the
        # IRSA service account (task 8.2), the CSI volume backed by the
        # SecretProviderClass (task 8.3), and the concatenated enabled
        # observability env blocks from ``observability_config`` (task 3). Adds
        # the port-forward/test CfnOutputs. Ordered after the service account,
        # the SecretProviderClass, and the cluster's prerequisite Helm charts.
        self._create_deployment_and_service()

        # Surface the created secret names for the Operator (Requirements 6.6,
        # 9.6) — supports the README ``put-secret-value`` step (task 12).
        self._emit_outputs()

    # ------------------------------------------------------------------
    # Task 8.1 — CDK-owned Secrets Manager secrets (empty).
    # ------------------------------------------------------------------

    def _create_platform_secrets(self) -> None:
        """Create an empty, CDK-owned Secrets Manager secret per enabled platform.

        Realizes Requirements 6.1, 6.2, 6.3, 6.5, 6.6 and design DD-5. For each
        enabled platform a ``secretsmanager.Secret`` is created with:

        * a **deterministic name** equal to the platform's ``*_secret_name`` from
          :class:`AppConfig` (default ``agent-observability/<agent>/<platform>``),
          so task 8.2's IAM grant, task 8.3's ``SecretProviderClass``, and the
          Operator's ``put-secret-value`` all reference the same name;
        * an **empty/placeholder JSON body** with the expected keys and empty
          values, produced via :meth:`_placeholder_generator` so no credential
          material enters CDK/CFN/git (Requirements 6.2, 6.3, 6.5);
        * ``RemovalPolicy.RETAIN`` so an accidental ``cdk destroy`` does not shred
          a populated credential (Requirements 6.x, 9.6).

        A disabled platform gets **no** secret and its attribute is ``None``
        (Requirements 6.6, 11.7).
        """
        # Default all attributes to None so a disabled platform is unambiguously
        # "no secret" for tasks 8.2 / 8.3 to branch on.
        self.langsmith_secret = None
        self.langfuse_secret = None
        self.secrets_encryption_key = None

        # Customer-managed KMS key (CMK) encrypting the platform secrets at rest.
        # Secrets Manager's default is the AWS-managed ``aws/secretsmanager`` key;
        # a dedicated CMK gives an explicit, auditable, rotatable key and resolves
        # Checkov CKV_AWS_149 (and CKV_AWS_7 via rotation). Created once and shared
        # by both secrets, and only when at least one platform is enabled (a fully
        # disabled config adds no key). ``grant_read`` on each secret (task 8.2)
        # automatically extends the agent role with ``kms:Decrypt`` on this key, so
        # the Secrets Store CSI driver can still sync the Kubernetes Secret. RETAIN
        # matches the secrets' retention so a retained (populated) secret stays
        # decryptable after an accidental ``cdk destroy`` — documented alongside
        # the retained secrets (Requirement 9.6).
        if self._config.langsmith_enabled or self._config.langfuse_enabled:
            self.secrets_encryption_key = kms.Key(
                self,
                "SecretsEncryptionKey",
                description=(
                    "CMK encrypting the agent observability platform secrets "
                    "(LangSmith / Langfuse) at rest."
                ),
                enable_key_rotation=True,
                removal_policy=RemovalPolicy.RETAIN,
            )

        # LangSmith — single ``api-key`` (created only when enabled).
        if self._config.langsmith_enabled:
            self.langsmith_secret = secretsmanager.Secret(
                self,
                "LangSmithSecret",
                # Deterministic name from AppConfig (Requirement 6.6, DD-5).
                secret_name=self._config.langsmith_secret_name,
                description=(
                    "LangSmith API key for the agent (CDK-created empty; the "
                    "Operator populates it via 'aws secretsmanager "
                    "put-secret-value')."
                ),
                # Empty placeholder body with the expected 'api-key' key.
                generate_secret_string=self._placeholder_generator(
                    LANGSMITH_SECRET_TEMPLATE
                ),
                # Encrypt at rest with the dedicated CMK (Checkov CKV_AWS_149).
                encryption_key=self.secrets_encryption_key,
                # Retain on destroy so credentials are not shredded accidentally
                # (Requirement 9.6); documented as a retained resource.
                removal_policy=RemovalPolicy.RETAIN,
            )

        # Langfuse — ``public-key`` + ``secret-key`` pair (created only when enabled).
        if self._config.langfuse_enabled:
            self.langfuse_secret = secretsmanager.Secret(
                self,
                "LangfuseSecret",
                secret_name=self._config.langfuse_secret_name,
                description=(
                    "Langfuse public/secret key pair for the agent (CDK-created "
                    "empty; the Operator populates it via 'aws secretsmanager "
                    "put-secret-value')."
                ),
                generate_secret_string=self._placeholder_generator(
                    LANGFUSE_SECRET_TEMPLATE
                ),
                # Encrypt at rest with the dedicated CMK (Checkov CKV_AWS_149).
                encryption_key=self.secrets_encryption_key,
                # Retain on destroy so credentials are not shredded accidentally
                # (Requirement 9.6); documented as a retained resource.
                removal_policy=RemovalPolicy.RETAIN,
            )

    @staticmethod
    def _placeholder_generator(
        template: dict[str, str],
    ) -> secretsmanager.SecretStringGenerator:
        """Build a ``SecretStringGenerator`` that stamps an empty placeholder body.

        The generator is configured with ``secret_string_template`` set to the
        JSON of ``template`` (the expected keys, all empty strings) and
        ``generate_string_key`` set to the throwaway
        :data:`PLACEHOLDER_GENERATE_KEY`. Secrets Manager fills only the throwaway
        key with a random value server-side, so the resulting secret has the
        expected key shape with empty values plus one ignored ``_unused`` key —
        and no credential-shaped literal ever appears in CDK source, the template,
        a change set, or deploy output (Requirements 6.2, 6.3, 6.5; design
        Correctness Property 1).

        Args:
            template: the expected-key placeholder mapping (all values empty),
                e.g. ``{"api-key": ""}`` or
                ``{"public-key": "", "secret-key": ""}``.

        Returns:
            A :class:`~aws_cdk.aws_secretsmanager.SecretStringGenerator` that
            creates the empty placeholder body.
        """
        return secretsmanager.SecretStringGenerator(
            # The expected keys with empty values — this is the placeholder body.
            secret_string_template=json.dumps(template),
            # Throwaway key Secrets Manager fills with a random value server-side;
            # required whenever a template is supplied. Ignored by the
            # SecretProviderClass (task 8.3) and overwritten by put-secret-value.
            generate_string_key=PLACEHOLDER_GENERATE_KEY,
            # Keep the generated throwaway value simple/JSON-safe. It is never
            # read, so its exact contents do not matter.
            exclude_punctuation=True,
        )

    # ------------------------------------------------------------------
    # Task 8.2 — scoped Agent_IAM_Role + IRSA service account.
    # ------------------------------------------------------------------

    def _create_agent_role_and_service_account(self) -> None:
        """Create the IRSA service account and attach scoped inline IAM statements.

        Realizes Requirements 4.1-4.8 and the design's "WorkloadStack IAM role
        (scoped)" table. The Agent_Service_Account is created with
        ``cluster.add_service_account`` (which creates the Agent_IAM_Role and
        wires the IRSA trust relationship via the cluster's OIDC provider,
        Requirement 4.1), and the role is granted **inline, resource-scoped
        statements only** — never one of the four broad managed policies the old
        deployment used (``AmazonBedrockFullAccess``, ``CloudWatchLogsFullAccess``,
        ``AWSXRayDaemonWriteAccess``, ``SecretsManagerReadWrite``), satisfying
        Requirement 4.3.

        The five permission groups (design table):

        #. **Bedrock invoke** — ``bedrock:InvokeModel`` /
           ``bedrock:InvokeModelWithResponseStream`` scoped to the model and judge
           inference-profile ARNs *and* the underlying foundation-model ARNs the
           profiles route to (Requirement 4.2). See
           :meth:`_bedrock_resource_arns`.
        #. **X-Ray** — ``xray:PutTraceSegments`` / ``xray:PutTelemetryRecords`` on
           ``*``; X-Ray write actions do not support resource-level scoping
           (Requirement 4.6, documented per the design note).
        #. **CloudWatch Logs** — ``logs:CreateLogStream`` / ``logs:PutLogEvents`` /
           ``logs:DescribeLogStreams`` scoped to the AgentCore log group ARN and
           its ``:*`` streams; never a wildcard log-group (Requirement 4.7).
        #. **CloudWatch metrics** — ``cloudwatch:PutMetricData`` on ``*`` but
           conditioned on ``cloudwatch:namespace = bedrock-agentcore`` so it is
           scoped to the agent's namespace, not unrestricted (Requirement 4.8).
        #. **Secrets Manager** — ``secretsmanager:GetSecretValue`` scoped to ONLY
           the enabled platforms' secret ARNs via ``grant_read``; no wildcard
           resource, and no statement at all when both platforms are disabled
           (Requirement 4.4).

        Cluster guard: the service account requires the cluster (its OIDC
        provider). ``app.py`` (task 10) always passes a real cluster, so this is
        fully exercised in a wired deploy and by the task 11 tests. When the stack
        is synthesized in isolation *without* a cluster (``self._cluster is
        None``), this method no-ops and leaves ``self.service_account`` as
        ``None`` — the module stays importable/synthesizable on its own.
        """
        # Default so a cluster-less isolated synth is unambiguously "no SA" for
        # tasks 8.3 / 9 to branch on.
        self.service_account = None

        # No cluster => cannot create an IRSA service account. app.py always
        # passes the cluster; this branch only matters for isolated synth.
        if self._cluster is None:
            return

        cfg = self._config

        # IRSA service account + backing Agent_IAM_Role (Requirement 4.1). The
        # deterministic name (``<agent>-sa`` in ``default``) matches the old
        # chart's ``langgraph-shopping-agent-sa`` so downstream references are
        # stable (task 8.3 / 9).
        self.service_account = self._cluster.add_service_account(
            "AgentServiceAccount",
            name=f"{cfg.agent_name}{AGENT_SERVICE_ACCOUNT_SUFFIX}",
            namespace=AGENT_SERVICE_ACCOUNT_NAMESPACE,
        )

        # (1) Bedrock invoke — scoped to the specific model/judge inference
        # profiles and the foundation models they route to (Requirement 4.2).
        self.service_account.add_to_principal_policy(
            iam.PolicyStatement(
                sid="BedrockInvokeScoped",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=self._bedrock_invoke_resource_arns(),
            )
        )

        # (2) X-Ray write — cannot be ARN-scoped by AWS (Requirement 4.6).
        self.service_account.add_to_principal_policy(
            iam.PolicyStatement(
                sid="XRayWrite",
                effect=iam.Effect.ALLOW,
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            )
        )

        # (3) CloudWatch Logs — scoped to the AgentCore log group + its streams
        # (Requirement 4.7). Never a wildcard log-group resource.
        log_group_arn = self._agentcore_log_group_arn()
        self.service_account.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AgentCoreLogsScoped",
                effect=iam.Effect.ALLOW,
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                # The log group ARN itself plus its ``:*`` log streams.
                resources=[log_group_arn, f"{log_group_arn}:*"],
            )
        )

        # (4) CloudWatch metrics — PutMetricData is not ARN-scopable, so scope it
        # with a namespace condition instead of an unrestricted namespace
        # (Requirement 4.8).
        self.service_account.add_to_principal_policy(
            iam.PolicyStatement(
                sid="CloudWatchMetricsNamespaceScoped",
                effect=iam.Effect.ALLOW,
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": AGENTCORE_METRIC_NAMESPACE
                    }
                },
            )
        )

        # (5) Secrets Manager — read only the enabled platforms' secrets
        # (Requirement 4.4), plus ``kms:Decrypt`` on the CMK that encrypts them so
        # Secrets Manager can decrypt on the caller's behalf.
        #
        # These are attached as **identity-based** statements on the agent role
        # (like the four groups above) rather than via ``secret.grant_read`` on
        # purpose. ``grant_read`` on a CMK-encrypted secret also calls
        # ``key.grant_decrypt(role)``, which — because the CMK lives in this
        # WorkloadStack while the role lives in ClusterStack (created by the
        # cluster) — adds the role to the *key's resource policy*, creating a
        # ``WorkloadStack -> ClusterStack`` edge. ClusterStack already depends on
        # WorkloadStack (its SecretProviderClass manifest references the secret),
        # so that reverse edge is a stack dependency cycle. Granting decrypt on the
        # role's own policy (referencing the key ARN) keeps every edge pointing
        # ``ClusterStack -> WorkloadStack`` and relies on the CMK's default
        # account-identity key policy — no key resource-policy mutation, no cycle.
        #
        # Scoping is unchanged: exact secret ARNs (no wildcard) and the single CMK
        # ARN. Disabled platforms contribute no secret and thus no statement; if
        # BOTH are disabled there is no Secrets Manager / KMS statement at all.
        secret_arns = [
            secret.secret_arn
            for secret in (self.langsmith_secret, self.langfuse_secret)
            if secret is not None
        ]
        if secret_arns:
            self.service_account.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="SecretsManagerReadScoped",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                    ],
                    resources=secret_arns,
                )
            )
            # Decrypt on the CMK (task 8.1) so GetSecretValue can decrypt the
            # secret payload. Identity-based to avoid the cross-stack cycle above.
            if self.secrets_encryption_key is not None:
                self.service_account.add_to_principal_policy(
                    iam.PolicyStatement(
                        sid="SecretsKmsDecryptScoped",
                        effect=iam.Effect.ALLOW,
                        actions=["kms:Decrypt"],
                        resources=[self.secrets_encryption_key.key_arn],
                    )
                )

    def _bedrock_invoke_resource_arns(self) -> list[str]:
        """Build the scoped Bedrock resource ARN list for the model and judge.

        For each of the model id and judge model id, this produces two ARNs
        (Requirement 4.2):

        * the **inference-profile** ARN
          ``arn:<partition>:bedrock:<region>:<account>:inference-profile/<modelId>``,
          which is the resource the agent actually invokes; and
        * the **foundation-model** ARN
          ``arn:<partition>:bedrock:*::foundation-model/<baseModelId>`` where
          ``<baseModelId>`` is the model id with its cross-region geo prefix (e.g.
          ``us.``) stripped.

        Both are required because invoking a cross-region inference profile also
        requires ``bedrock:InvokeModel`` permission on the foundation models the
        profile routes to. Using the ``*`` region on the foundation-model ARN (and
        the empty account segment foundation models always use) keeps the grant
        scoped to *specific model resources* while covering every region the ``us.``
        profile may fan out to — far tighter than ``bedrock:*`` or a ``*`` resource,
        satisfying Requirement 4.2 and keeping the banned ``AmazonBedrockFullAccess``
        off the role (Requirement 4.3).

        Duplicate ARNs (e.g. if the model and judge share a base model) are
        de-duplicated while preserving order.

        Returns:
            The ordered, de-duplicated list of Bedrock resource ARNs.

        Raises:
            ValueError: if a supplied model id is malformed such that an ARN
                cannot be constructed (Requirement 4.5) — fail synthesis rather
                than fall back to unscoped Bedrock access.
        """
        arns: list[str] = []
        for arn in (
            *self._bedrock_resource_arns(self._config.model_id, "modelId"),
            *self._bedrock_resource_arns(self._config.judge_model_id, "judgeModelId"),
        ):
            if arn not in arns:
                arns.append(arn)
        return arns

    def _bedrock_resource_arns(self, model_id: str, input_name: str) -> list[str]:
        """Return the inference-profile and foundation-model ARNs for one model id.

        See :meth:`_bedrock_invoke_resource_arns` for the rationale. This validates
        the model id first (Requirement 4.5): an empty id, an id containing
        whitespace, or one whose base (after stripping a recognized geo prefix) is
        empty cannot form a valid ARN, so synthesis fails with a clear error
        rather than silently widening access.

        Args:
            model_id: the Bedrock model / inference-profile id.
            input_name: the config field name (``modelId`` / ``judgeModelId``) used
                in the error message so the Operator knows which input is bad.

        Returns:
            ``[inference_profile_arn, foundation_model_arn]``.

        Raises:
            ValueError: if ``model_id`` is malformed (Requirement 4.5).
        """
        raw = model_id or ""
        # Guard (Requirement 4.5): reject anything that cannot form an ARN — empty
        # or whitespace-bearing ids would produce a malformed/over-broad resource.
        if raw.strip() == "" or any(ch.isspace() for ch in raw):
            raise ValueError(
                f"Invalid Bedrock model id for '{input_name}': {model_id!r}. "
                "The model id must be a non-empty identifier with no whitespace so "
                "a scoped Bedrock ARN can be constructed (failing synthesis rather "
                "than falling back to unscoped Bedrock access)."
            )

        # Strip a recognized cross-region geo prefix (e.g. ``us.``) to recover the
        # underlying foundation-model id. Unrecognized/absent prefixes leave the id
        # as-is (a plain foundation-model id passed directly).
        base_model_id = raw
        for prefix in BEDROCK_CROSS_REGION_PREFIXES:
            if raw.startswith(prefix):
                base_model_id = raw[len(prefix):]
                break

        if base_model_id.strip() == "":
            raise ValueError(
                f"Invalid Bedrock model id for '{input_name}': {model_id!r}. "
                "Stripping the cross-region prefix left no foundation-model id, so "
                "a scoped Bedrock ARN cannot be constructed."
            )

        # Inference-profile ARN: account/region-scoped, the resource the agent
        # invokes. Built from the stack's partition/region/account tokens.
        inference_profile_arn = (
            f"arn:{self.partition}:bedrock:{self.region}:{self.account}"
            f":inference-profile/{raw}"
        )
        # Foundation-model ARN: foundation models use an empty account segment and
        # a region; use ``*`` region so the grant covers every region a cross-region
        # (``us.``) profile can route to, while still naming the specific model.
        foundation_model_arn = (
            f"arn:{self.partition}:bedrock:*::foundation-model/{base_model_id}"
        )
        return [inference_profile_arn, foundation_model_arn]

    def _agentcore_log_group_arn(self) -> str:
        """Return the AgentCore log group ARN to scope the Logs grant to.

        Prefers the ARN owned by ``ObservabilityStack``
        (``self._observability_stack.log_group_arn``) when that stack was passed
        by reference, so the grant tracks exactly the log group that stack creates
        (Requirement 4.7). When the workload stack is synthesized in isolation
        without an observability stack (app.py wiring is task 10), the same ARN is
        derived from config — ``arn:<partition>:logs:<region>:<account>:log-group:
        /aws/bedrock-agentcore/runtimes/<agent>`` — using the identical
        ``COLON_RESOURCE_NAME`` format ``ObservabilityStack`` uses, so both paths
        yield the same value.

        Returns:
            The log group ARN (without a trailing ``:*``); callers append ``:*`` for
            the log streams.
        """
        if self._observability_stack is not None:
            return self._observability_stack.log_group_arn

        # Derive the same ARN from config when synthesized in isolation.
        log_group_name = f"/aws/bedrock-agentcore/runtimes/{self._config.agent_name}"
        return self.format_arn(
            service="logs",
            resource="log-group",
            resource_name=log_group_name,
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

    # ------------------------------------------------------------------
    # Task 8.3 — SecretProviderClass (synced Kubernetes Secret).
    # ------------------------------------------------------------------

    def _create_secret_provider_class(self) -> None:
        """Apply the ``SecretProviderClass`` that syncs enabled secrets into K8s.

        Realizes Requirements 6.2, 8.4, 8.6, 11.7 and the design's
        "SecretProviderClass (synced Kubernetes Secret)" data model. A single
        ``SecretProviderClass`` custom resource
        (``secrets-store.csi.x-k8s.io/v1``) is applied to the cluster via
        ``cluster.add_manifest``. It tells the AWS Secrets Store CSI Driver
        (provider ``aws`` / ASCP) to:

        #. fetch each *enabled* platform's AWS Secrets Manager secret (by the
           deterministic name CDK created it under — ``config.*_secret_name``),
           extracting individual JSON keys via ``jmesPath`` into named object
           aliases (``parameters.objects``); and
        #. sync those aliases into native Kubernetes Secrets (``secretObjects``)
           — ``langsmith-secret`` (key ``api-key``) and ``langfuse-secret`` (keys
           ``public-key`` / ``secret-key``) — which are exactly the Secret
           names/keys the pod consumes via ``secretKeyRef`` (see
           ``observability_config``), so ``docker/app/app.py`` needs no change.

        Enabled-platform consistency (Requirement 11.7): only enabled platforms'
        entries are rendered. A disabled platform contributes **no** ``objects``
        entry and **no** ``secretObjects`` entry. If **both** platforms are
        disabled there is nothing to sync, so **no** ``SecretProviderClass`` is
        created at all and :attr:`secret_provider_class` stays ``None``.

        ``parameters.objects`` is, per the CSI spec, a YAML-formatted **string**
        (not a nested structure). It is built here as a list of Python dicts and
        serialized with :func:`json.dumps` — JSON is a valid YAML subset the ASCP
        provider parses, so no YAML dependency is needed and no ambiguity is
        introduced.

        Ordering (design DD-7): the ``SecretProviderClass`` must be applied only
        after the CSI driver and ASCP provider Helm charts are installed. When
        ``self._cluster_stack`` was passed by reference, explicit
        ``node.add_dependency`` edges are added against
        ``cluster_stack.csi_driver_chart`` and ``cluster_stack.ascp_provider_chart``
        so CDK orders the manifest after both add-ons.

        Cluster guard: applying a manifest requires the cluster. ``app.py``
        (task 10) always passes a real cluster, so this is fully exercised in a
        wired deploy. When synthesized in isolation *without* a cluster
        (``self._cluster is None``), this method no-ops and leaves
        :attr:`secret_provider_class` as ``None`` — the module stays
        importable/synthesizable on its own.
        """
        # Default so callers (task 9) can unambiguously branch on "no SPC".
        self.secret_provider_class = None

        # No cluster => cannot apply a manifest. app.py always passes the
        # cluster; this branch only matters for isolated synth.
        if self._cluster is None:
            return

        # Build the enabled platforms' ``objects`` and ``secretObjects`` entries.
        # Only enabled platforms contribute (Requirement 11.7).
        objects: list[dict[str, Any]] = []
        secret_objects: list[dict[str, Any]] = []

        if self.langsmith_secret is not None:
            # Extract the single ``api-key`` from the LangSmith Secrets Manager
            # secret into the ``langsmith-api-key`` alias...
            objects.append(
                {
                    "objectName": self._config.langsmith_secret_name,
                    "objectType": SECRETSMANAGER_OBJECT_TYPE,
                    "jmesPath": [
                        {
                            # JMESPath treats '-' as subtraction, so hyphenated
                            # JSON keys MUST be double-quoted (e.g. "api-key"),
                            # otherwise the ASCP provider rejects it with
                            # "Invalid JMES Path".
                            "path": f'"{LANGSMITH_API_KEY_SECRET_KEY}"',
                            "objectAlias": LANGSMITH_API_KEY_OBJECT_ALIAS,
                        }
                    ],
                }
            )
            # ...and sync that alias into the ``langsmith-secret`` K8s Secret
            # under the ``api-key`` key the pod's secretKeyRef expects.
            secret_objects.append(
                {
                    "secretName": LANGSMITH_K8S_SECRET_NAME,
                    "type": "Opaque",
                    "data": [
                        {
                            "objectName": LANGSMITH_API_KEY_OBJECT_ALIAS,
                            "key": LANGSMITH_API_KEY_SECRET_KEY,
                        }
                    ],
                }
            )

        if self.langfuse_secret is not None:
            # Extract the ``public-key`` and ``secret-key`` from the Langfuse
            # Secrets Manager secret into their respective aliases...
            objects.append(
                {
                    "objectName": self._config.langfuse_secret_name,
                    "objectType": SECRETSMANAGER_OBJECT_TYPE,
                    "jmesPath": [
                        {
                            # Hyphenated keys must be double-quoted for JMESPath.
                            "path": f'"{LANGFUSE_PUBLIC_KEY_SECRET_KEY}"',
                            "objectAlias": LANGFUSE_PUBLIC_KEY_OBJECT_ALIAS,
                        },
                        {
                            "path": f'"{LANGFUSE_SECRET_KEY_SECRET_KEY}"',
                            "objectAlias": LANGFUSE_SECRET_KEY_OBJECT_ALIAS,
                        },
                    ],
                }
            )
            # ...and sync both aliases into the ``langfuse-secret`` K8s Secret
            # under the ``public-key`` / ``secret-key`` keys the pod expects.
            secret_objects.append(
                {
                    "secretName": LANGFUSE_K8S_SECRET_NAME,
                    "type": "Opaque",
                    "data": [
                        {
                            "objectName": LANGFUSE_PUBLIC_KEY_OBJECT_ALIAS,
                            "key": LANGFUSE_PUBLIC_KEY_SECRET_KEY,
                        },
                        {
                            "objectName": LANGFUSE_SECRET_KEY_OBJECT_ALIAS,
                            "key": LANGFUSE_SECRET_KEY_SECRET_KEY,
                        },
                    ],
                }
            )

        # Both platforms disabled => nothing to sync => create no CR at all
        # (Requirement 11.7). Leaves ``secret_provider_class`` as ``None``.
        if not objects:
            return

        # The CSI ``parameters.objects`` field is a YAML-formatted STRING; JSON is
        # a valid YAML subset the ASCP provider parses (design data model note).
        manifest = {
            "apiVersion": SECRET_PROVIDER_CLASS_API_VERSION,
            "kind": SECRET_PROVIDER_CLASS_KIND,
            "metadata": {
                "name": SECRET_PROVIDER_CLASS_NAME,
                # Same namespace as the Agent_Service_Account / Deployment so the
                # workload (task 9) can mount/consume the synced Secrets.
                "namespace": AGENT_SERVICE_ACCOUNT_NAMESPACE,
            },
            "spec": {
                "provider": SECRET_PROVIDER,
                "parameters": {
                    "objects": json.dumps(objects),
                },
                "secretObjects": secret_objects,
            },
        }

        self.secret_provider_class = self._cluster.add_manifest(
            "AgentSecretProviderClass", manifest
        )

        # Order the manifest after the CSI driver + ASCP provider are installed
        # (design DD-7). Only possible when the ClusterStack was passed by
        # reference; guarded with getattr so isolated synth (no cluster_stack, or
        # a stand-in without the chart attributes) still works.
        if self._cluster_stack is not None:
            csi_driver_chart = getattr(
                self._cluster_stack, "csi_driver_chart", None
            )
            ascp_provider_chart = getattr(
                self._cluster_stack, "ascp_provider_chart", None
            )
            if csi_driver_chart is not None:
                self.secret_provider_class.node.add_dependency(csi_driver_chart)
            if ascp_provider_chart is not None:
                self.secret_provider_class.node.add_dependency(ascp_provider_chart)

    # ------------------------------------------------------------------
    # Task 10 — build the agent image inside this stack when none was passed.
    # ------------------------------------------------------------------

    def _resolve_agent_image(self) -> None:
        """Resolve the agent image URI the Deployment references.

        The container image is built by ``ClusterStack`` (see that stack's
        docstring for why). It is built there — not here — because the workload's
        ``Deployment``/``Service`` manifests are attached to the cluster via
        ``cluster.add_manifest(...)`` and therefore materialize in the
        cluster-owning stack. Co-locating the image build with those manifests
        keeps the Deployment's image reference intra-stack; building it here would
        make the Deployment consume the content-derived image tag through a
        cross-stack CloudFormation export, which CloudFormation refuses to update
        while it is in use — deadlocking any redeploy that changes ``docker/``
        ("Cannot update export ... as it is in use").

        Behavior by case:

        * **``image_uri`` supplied** (a pre-published URI) — used as-is; no image
          is referenced from ``ClusterStack``. Keeps the stack usable with an
          externally-built image.
        * **``image_uri`` is ``None`` and a ``cluster_stack`` with an
          ``agent_image`` is present** (the wired deploy from ``app.py``) — adopt
          ``cluster_stack.agent_image`` and its :attr:`~AgentImage.image_uri`, and
          expose it as :attr:`agent_image` so
          :meth:`_order_workload_dependencies` orders the Deployment after the
          image build (both live in ``ClusterStack``, so this is an intra-stack
          dependency).
        * **``image_uri`` is ``None`` and no cluster** (isolated synth) — nothing
          runs, so no image is resolved and
          :meth:`_create_deployment_and_service` no-ops on the ``None`` URI.
        * **Fallback: cluster present but no ``cluster_stack.agent_image``**
          (standalone use/tests) — build the image in this stack's scope so the
          stack stays synthesizable on its own.
        """
        # A pre-published URI was supplied by app.py / a test — use it verbatim.
        if self._image_uri is not None:
            return
        # Isolated cluster-less synth: no workload to run, so no image to resolve.
        if self._cluster is None:
            return
        # Preferred path: consume the image built by ClusterStack so the
        # Deployment's image reference stays intra-stack (no cross-stack export
        # of the changing image tag). ``app.py`` always passes ``cluster_stack``.
        cluster_image = getattr(self._cluster_stack, "agent_image", None)
        if cluster_image is not None:
            self.agent_image = cluster_image
            self._image_uri = cluster_image.image_uri
            return
        # Fallback for standalone use without a ClusterStack (e.g. isolated
        # tests): build the image in this stack's scope.
        self.agent_image = AgentImage(self, "AgentImage", config=self._config)
        self._image_uri = self.agent_image.image_uri

    # ------------------------------------------------------------------
    # Task 9 — Kubernetes Deployment + Service.
    # ------------------------------------------------------------------

    def _create_deployment_and_service(self) -> None:
        """Apply the Agent_Workload ``Deployment`` and ``Service`` to the cluster.

        Realizes Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.9, and 8.10 (design
        "Deployment env var shape" and DD-8). Two Kubernetes manifests are applied
        via ``cluster.add_manifest`` — a ``Deployment`` and a ``ClusterIP``
        ``Service`` — reproducing the exact runtime shape of the old Helm chart
        (``chart/templates/deployment.yaml`` / ``service.yaml``) but generated
        declaratively from :class:`AppConfig` and the published image.

        **Deployment** (Requirements 8.1, 8.2, 8.10):

        * **1 replica** exposing **container port 8000** (named ``http``).
        * **Image** = :attr:`_image_uri`, the digest-pinned URI published by the
          ``AgentImage`` construct (task 7).
        * **No ``command``/``args`` override and no ConfigMap volume** — the image
          runs its built ``CMD`` (``opentelemetry-instrument python app.py``) and
          ships ``app.py`` inside itself, deleting the two workarounds the old
          chart carried (Requirement 8.10, design DD-8).
        * **serviceAccountName** = the IRSA service account created in task 8.2
          (``<agent>-sa`` in the ``default`` namespace, Requirement 8.3), so the
          pod assumes the scoped Agent_IAM_Role.
        * **env** = the base runtime vars (Requirement 8.4, design "Deployment env
          var shape") concatenated with the enabled observability blocks from
          ``observability_config`` — including the ``secretKeyRef``s that source
          LangSmith/Langfuse credentials from the synced Kubernetes Secrets
          (Requirement 8.5). See :meth:`_container_env`.
        * **livenessProbe** ``GET /health:8000`` (delay 30s / period 60s) and
          **readinessProbe** ``GET /health:8000`` (delay 10s / period 30s) — the
          exact timings from the old chart (Requirement 8.2).
        * **Secrets Store CSI volume** mounted read-only at ``/mnt/secrets-store``
          referencing the :attr:`secret_provider_class` (task 8.3). Mounting the
          volume is what triggers the CSI driver to fetch the Secrets Manager
          objects and materialize the ``secretKeyRef``-backed Kubernetes Secrets
          (design DD-5). The volume/mount is added **only** when at least one
          platform is enabled (``self.secret_provider_class is not None``); when
          both platforms are disabled it is omitted entirely.

        **Service** (Requirement 8.1): a ``ClusterIP`` Service named
        ``config.agent_name`` mapping port **80** to the container's port
        **8000**, selecting the pods by the shared recommended labels.

        **Ordering / dependencies:** the Deployment is ordered after (a) the IRSA
        service account (the pod references it), (b) the ``SecretProviderClass``
        (its synced Secrets back the ``secretKeyRef``s and its CSI volume), and
        (c) the cluster's prerequisite Helm charts (cert-manager, the CSI driver,
        and the ASCP provider) when ``self._cluster_stack`` was passed by
        reference (design DD-7, Requirements 8.7/8.8), and (d) — when this stack
        built the image itself (:meth:`_resolve_agent_image`) — the image's
        ``ECRDeployment`` so the image is published to the named ECR repo before
        the Deployment (which references the URI as a string) applies.

        **Guards (isolated synth):** applying a manifest requires the cluster, and
        the Deployment needs the published image URI. ``app.py`` (task 10) always
        passes both, so this is fully exercised in a wired deploy and by the task
        11 tests. When synthesized in isolation without a cluster
        (``self._cluster is None``) or without an image
        (``self._image_uri is None``), this method no-ops and leaves
        :attr:`deployment` / :attr:`service` as ``None`` — the module stays
        importable/synthesizable on its own.
        """
        # Default so callers can unambiguously branch on "no workload".
        self.deployment = None
        self.service = None

        # No cluster => cannot apply a manifest; no image => nothing to run.
        # app.py (task 10) always passes both; these branches only matter for
        # isolated synth.
        if self._cluster is None or self._image_uri is None:
            return

        name = self._config.agent_name

        # Recommended labels shared by the Deployment selector, the pod template,
        # and the Service selector so the three stay consistent (Requirement
        # 11.5). Both keys are the agent name.
        labels = {
            LABEL_NAME_KEY: name,
            LABEL_INSTANCE_KEY: name,
        }

        # Service-account name from the IRSA service account (task 8.2). It is
        # non-None whenever the cluster is present (both are created together),
        # but fall back to the deterministic name defensively.
        if self.service_account is not None:
            service_account_name = self.service_account.service_account_name
        else:  # pragma: no cover - cluster present implies SA present
            service_account_name = (
                f"{name}{AGENT_SERVICE_ACCOUNT_SUFFIX}"
            )

        # Build the single container spec (image, port, env, probes, and the CSI
        # mount when a platform is enabled).
        container = self._container_spec(name)

        # Assemble the pod spec, adding the CSI volume only when there is a
        # SecretProviderClass to mount (Requirement 11.7 — no volume when both
        # platforms are disabled).
        pod_spec: dict[str, Any] = {
            "serviceAccountName": service_account_name,
            "containers": [container],
        }
        if self.secret_provider_class is not None:
            pod_spec["volumes"] = [self._csi_volume()]

        deployment_manifest = {
            "apiVersion": DEPLOYMENT_API_VERSION,
            "kind": DEPLOYMENT_KIND,
            "metadata": {
                "name": name,
                "namespace": AGENT_SERVICE_ACCOUNT_NAMESPACE,
                "labels": labels,
            },
            "spec": {
                "replicas": DEPLOYMENT_REPLICAS,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": pod_spec,
                },
            },
        }

        service_manifest = {
            "apiVersion": SERVICE_API_VERSION,
            "kind": SERVICE_KIND,
            "metadata": {
                "name": name,
                "namespace": AGENT_SERVICE_ACCOUNT_NAMESPACE,
                "labels": labels,
            },
            "spec": {
                "type": SERVICE_TYPE,
                "ports": [
                    {
                        "port": SERVICE_PORT,
                        "targetPort": CONTAINER_PORT,
                        "protocol": "TCP",
                        "name": CONTAINER_PORT_NAME,
                    }
                ],
                "selector": labels,
            },
        }

        self.deployment = self._cluster.add_manifest(
            "AgentDeployment", deployment_manifest
        )
        self.service = self._cluster.add_manifest("AgentService", service_manifest)

        # Order the workload after everything it depends on.
        self._order_workload_dependencies()

        # Port-forward / test command outputs (Requirement 8.9).
        self._emit_workload_outputs()

    def _container_spec(self, name: str) -> dict[str, Any]:
        """Build the single agent container spec for the Deployment.

        Assembles the image, the named container port, the concatenated env
        (:meth:`_container_env`), the ``/health`` liveness/readiness probes with
        the exact timings from the old chart (Requirement 8.2), and — when a
        platform is enabled — the read-only Secrets Store CSI volume mount. There
        is deliberately **no** ``command``/``args`` key and **no** ConfigMap
        volume mount, so the image runs its built ``CMD`` unchanged
        (Requirement 8.10, design DD-8).

        Args:
            name: the agent name, used as the container name (matches the old
                chart, whose container name was the chart name).

        Returns:
            The container spec dict for the pod template.
        """
        container: dict[str, Any] = {
            "name": name,
            "image": self._image_uri,
            "ports": [
                {
                    "name": CONTAINER_PORT_NAME,
                    "containerPort": CONTAINER_PORT,
                    "protocol": "TCP",
                }
            ],
            "env": self._container_env(),
            # HTTP GET /health probes with the exact old-chart timings
            # (Requirement 8.2).
            "livenessProbe": {
                "httpGet": {"path": HEALTH_CHECK_PATH, "port": CONTAINER_PORT},
                "initialDelaySeconds": LIVENESS_INITIAL_DELAY_SECONDS,
                "periodSeconds": LIVENESS_PERIOD_SECONDS,
            },
            "readinessProbe": {
                "httpGet": {"path": HEALTH_CHECK_PATH, "port": CONTAINER_PORT},
                "initialDelaySeconds": READINESS_INITIAL_DELAY_SECONDS,
                "periodSeconds": READINESS_PERIOD_SECONDS,
            },
        }

        # Mount the CSI volume only when a SecretProviderClass exists (i.e. at
        # least one platform is enabled). Mounting it triggers the driver to sync
        # the secretKeyRef-backed Kubernetes Secrets (design DD-5). Omitted
        # entirely when both platforms are disabled (Requirement 11.7).
        if self.secret_provider_class is not None:
            container["volumeMounts"] = [
                {
                    "name": CSI_VOLUME_NAME,
                    "mountPath": CSI_MOUNT_PATH,
                    "readOnly": True,
                }
            ]

        return container

    def _container_env(self) -> list[dict[str, Any]]:
        """Build the container ``env`` list: base runtime vars + observability.

        Realizes Requirements 8.4 and 8.5 and the design's "Deployment env var
        shape". The list is the concatenation of:

        #. the **base runtime vars** — ``AWS_REGION`` (from ``config.region``),
           ``MODEL_ID`` (from ``config.model_id``), and the fixed ``MAX_TOKENS`` /
           ``PORT`` runtime knobs; then
        #. the enabled **observability blocks** from ``observability_config``, in
           order — ``agentcore_env`` + ``langsmith_env`` + ``langfuse_env``. Each
           function returns ``[]`` when its platform is disabled (Requirement
           11.7), so only enabled platforms contribute env vars.

        The observability dicts are already in Kubernetes container ``env`` shape
        (``{"name","value"}`` literals or ``{"name","valueFrom":{"secretKeyRef":
        ...}}`` references), so they slot directly into the container ``env``. The
        ``secretKeyRef`` entries source the LangSmith/Langfuse credentials from the
        synced Kubernetes Secrets rather than as literal values (Requirement 8.5;
        design Correctness Property 1 — no plaintext).

        Returns:
            The ordered list of container ``env`` entries.
        """
        cfg = self._config

        # Base runtime vars (design "Deployment env var shape"). AWS_REGION /
        # MODEL_ID are input-derived; the rest are fixed runtime knobs.
        base_env: list[dict[str, Any]] = [
            {"name": "AWS_REGION", "value": cfg.region},
            {"name": "MODEL_ID", "value": cfg.model_id},
            {"name": "MAX_TOKENS", "value": BASE_ENV_MAX_TOKENS},
            {"name": "PORT", "value": BASE_ENV_PORT},
        ]

        # Concatenate only the enabled observability blocks (each returns [] when
        # its platform is disabled — Requirement 11.7).
        return [
            *base_env,
            *agentcore_env(cfg),
            *langsmith_env(cfg),
            *langfuse_env(cfg),
        ]

    def _csi_volume(self) -> dict[str, Any]:
        """Build the Secrets Store CSI pod volume backed by the SecretProviderClass.

        The volume uses the ``secrets-store.csi.k8s.io`` CSI driver in read-only
        mode and references the task-8.3 ``SecretProviderClass`` (by its fixed
        name :data:`SECRET_PROVIDER_CLASS_NAME`) via ``volumeAttributes``. Mounting
        this volume into the container is what makes the CSI driver fetch the AWS
        Secrets Manager objects and sync the ``secretKeyRef``-backed Kubernetes
        Secrets the pod's env consumes (design DD-5). Only called when
        ``self.secret_provider_class is not None``.

        Returns:
            The CSI volume spec dict for the pod ``volumes`` list.
        """
        return {
            "name": CSI_VOLUME_NAME,
            "csi": {
                "driver": CSI_DRIVER_NAME,
                "readOnly": True,
                "volumeAttributes": {
                    "secretProviderClass": SECRET_PROVIDER_CLASS_NAME,
                },
            },
        }

    def _order_workload_dependencies(self) -> None:
        """Order the Deployment after the SA, SecretProviderClass, and add-ons.

        The Deployment must apply only after everything it relies on exists
        (design DD-7):

        * the IRSA **service account** (task 8.2) — the pod references it by name
          and needs the Agent_IAM_Role/OIDC trust in place;
        * the **SecretProviderClass** (task 8.3, when present) — its CSI volume
          and the synced Secrets it produces back the pod's ``secretKeyRef``s;
        * the cluster's prerequisite **Helm charts** — cert-manager, the Secrets
          Store CSI driver, and the ASCP provider (Requirements 8.7/8.8), when the
          ``ClusterStack`` was passed by reference.

        The Service selects the same pods but has no such ordering needs beyond
        the Deployment, so only the Deployment's edges are added. All edges are
        guarded so isolated synth (or a stand-in cluster stack without the chart
        attributes) still works.
        """
        assert self.deployment is not None  # set by the caller before this runs

        # After the IRSA service account (task 8.2).
        if self.service_account is not None:
            self.deployment.node.add_dependency(self.service_account)

        # After the SecretProviderClass (task 8.3), when present.
        if self.secret_provider_class is not None:
            self.deployment.node.add_dependency(self.secret_provider_class)

        # After the agent image is built and pushed to the named ECR repo, so the
        # Deployment references an image that already exists. The image is built
        # in ClusterStack (adopted here as ``self.agent_image``) and the
        # Deployment manifest also materializes in ClusterStack, so this is an
        # intra-stack ordering edge. The Deployment consumes the URI as a string,
        # so this explicit edge is what orders the image push before the workload.
        if self.agent_image is not None:
            self.deployment.node.add_dependency(self.agent_image.deployment)

        # After the cluster prerequisite Helm charts (design DD-7). Guarded with
        # getattr so a stand-in cluster stack without these attributes still works.
        if self._cluster_stack is not None:
            for attr in (
                "cert_manager_chart",
                "csi_driver_chart",
                "ascp_provider_chart",
            ):
                chart = getattr(self._cluster_stack, attr, None)
                if chart is not None:
                    self.deployment.node.add_dependency(chart)

    def _emit_workload_outputs(self) -> None:
        """Emit the port-forward and sample ``/chat`` test commands (Requirement 8.9).

        Surfaces the two commands an Operator runs to reach the deployed agent
        once the workload is up: a ``kubectl port-forward`` against the ClusterIP
        Service, and a sample ``curl`` ``POST /chat`` request. Both are keyed to
        the resolved agent name and the Service's port (80 -> container 8000).
        """
        name = self._config.agent_name

        CfnOutput(
            self,
            "PortForwardCommand",
            value=(
                f"kubectl port-forward svc/{name} {CONTAINER_PORT}:{SERVICE_PORT}"
            ),
            description=(
                "Forward a local port to the agent Service, then send requests to "
                f"http://localhost:{CONTAINER_PORT}."
            ),
        )
        CfnOutput(
            self,
            "TestCommand",
            value=(
                f"curl -X POST http://localhost:{CONTAINER_PORT}/chat "
                "-H 'Content-Type: application/json' "
                "-d '{\"prompt\":\"Show me laptops\",\"session_id\":\"test-001\"}'"
            ),
            description=(
                "Sample request to the agent once port-forwarding is active "
                "(run the PortForwardCommand first)."
            ),
        )

    # ------------------------------------------------------------------
    # Stack outputs (Requirements 6.6, 9.6).
    # ------------------------------------------------------------------

    def _emit_outputs(self) -> None:
        """Emit the created secret names for the Operator.

        Surfaces the deterministic Secrets Manager name of each *enabled*
        platform's secret so the Operator can copy it straight into the
        ``aws secretsmanager put-secret-value --secret-id <name>`` step the README
        documents (Requirements 6.6, 9.6). Disabled platforms emit no output,
        matching "no secret is created" (Requirement 6.6, 11.7).
        """
        if self.langsmith_secret is not None:
            CfnOutput(
                self,
                "LangSmithSecretName",
                value=self._config.langsmith_secret_name,
                description=(
                    "Secrets Manager name of the (empty) LangSmith secret. Populate "
                    "it with: aws secretsmanager put-secret-value --secret-id "
                    "<this> --secret-string '{\"api-key\":\"...\"}'."
                ),
            )
        if self.langfuse_secret is not None:
            CfnOutput(
                self,
                "LangfuseSecretName",
                value=self._config.langfuse_secret_name,
                description=(
                    "Secrets Manager name of the (empty) Langfuse secret. Populate "
                    "it with: aws secretsmanager put-secret-value --secret-id "
                    "<this> --secret-string "
                    "'{\"public-key\":\"...\",\"secret-key\":\"...\"}'."
                ),
            )
