"""EKS cluster, managed node group, and OIDC/IRSA for the agent workload.

``ClusterStack`` provisions the Amazon EKS cluster that hosts the
Agent_Workload, matching the behavior of today's ``eksctl``-based deployment
but expressed declaratively (design DD-3, "EKS cluster provisioning —
CDK-native ``eks.Cluster``"). It is created as its own stack so the cluster's
failure blast radius and teardown ordering stay contained relative to the
network and workload stacks (design DD-2, "Stack decomposition").

This module implements two provisioning paths, selected by
``config.creates_cluster``:

* **New-cluster creation path** (task 5.1): the cluster, an explicitly-sized
  managed node group, the OIDC provider needed for IRSA, a matching ``kubectl``
  layer, and the stack outputs an Operator needs to reach the cluster. See
  :meth:`_create_cluster`.
* **Existing-cluster path** (task 5.2): when ``config.existing_cluster_name`` is
  supplied, the stack imports the cluster via
  ``eks.Cluster.from_cluster_attributes(...)`` and skips creation entirely — no
  new cluster and no managed node group are created (Requirement 2.5). See
  :meth:`_import_cluster`.

One follow-up task still extends this stack and is marked with a
clearly-labelled extension point below:

* **Task 5.3 — cluster prerequisites via Helm.** cert-manager and the AWS
  Secrets Store CSI Driver + ASCP provider will be installed with
  ``cluster.add_helm_chart(...)`` and ordered before the workload
  (Requirements 8.7, 8.8). See :meth:`_install_cluster_prerequisites`.

Design decisions realized here (DD-3):

* Kubernetes version **1.30**, matching the current sample.
* ``default_capacity=0`` on the cluster with an **explicit** managed node group
  via ``add_nodegroup_capacity`` so sizing is unambiguous and validated
  (sizes are pre-validated by :class:`AppConfig`, Requirement 2.2).
* Node group sized from :class:`AppConfig` (instance type, desired/min/max),
  defaulting to ``t3.medium`` / 2 / 1 / 3 (Requirement 2.3).
* An **OIDC provider** (enabled automatically by ``eks.Cluster``) so the
  Agent_Service_Account can assume the scoped Agent_IAM_Role via IRSA
  (Requirement 2.4). It is exposed as ``cluster.open_id_connect_provider`` for
  downstream stacks.
* A ``kubectl_layer`` pinned to Kubernetes 1.30
  (``aws_cdk.lambda_layer_kubectl_v30.KubectlV30Layer``) so CDK can apply the
  manifests and Helm charts later tasks add.

The created (or imported) cluster is exposed as the public attribute
:attr:`ClusterStack.cluster` so ``ObservabilityStack`` and ``WorkloadStack`` can
consume it by object reference (cross-stack references managed by CDK).

This module is intentionally *not* instantiated in ``app.py`` yet; the stacks
are composed together in task 10. It is written to be importable and
synthesizable on its own.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks as eks
from aws_cdk.lambda_layer_kubectl_v30 import KubectlV30Layer
from constructs import Construct

from .agent_image import AgentImage
from .app_config import AppConfig

# ---------------------------------------------------------------------------
# Cluster constants.
#
# Kept as module-level names so the version pin and node group logical id are
# self-documenting and easy to audit against the design (DD-3).
# ---------------------------------------------------------------------------

# Kubernetes control-plane version. Pinned to 1.30 to match the current sample
# (Requirement 2.1). The ``kubectl`` layer below is the matching v30 layer.
KUBERNETES_VERSION = eks.KubernetesVersion.V1_30

# Logical id for the explicit managed node group. A stable id keeps the node
# group's identity constant across deploys so re-sizing updates in place rather
# than replacing the group.
NODEGROUP_ID = "AgentNodeGroup"

# ---------------------------------------------------------------------------
# Cluster-prerequisite Helm chart pins (task 5.3, design DD-7).
#
# Versions are pinned so deploys are reproducible and auditable. Each pin is a
# known-good release; the comments note how to bump them safely. Because these
# strings are only handed to the Helm custom resource at deploy time, ``cdk
# synth`` accepts any value — the pins matter for what actually gets installed.
# ---------------------------------------------------------------------------

# cert-manager. Pinned to a v1.13.x release to match the version the sample
# installed previously (design DD-7 says "cert-manager (v1.13.x, matching
# today)"). v1.13 installs its CRDs via the ``installCRDs`` value (the
# ``crds.enabled`` toggle only exists from v1.15+), so the values below use
# ``installCRDs``. Bump within the v1.13.x line as needed.
CERT_MANAGER_CHART = "cert-manager"
CERT_MANAGER_REPOSITORY = "https://charts.jetstack.io"
CERT_MANAGER_NAMESPACE = "cert-manager"
CERT_MANAGER_VERSION = "v1.13.3"

# AWS Secrets Store CSI Driver. Pinned to a known-good ~1.4.x chart release.
# ``syncSecret.enabled=true`` lets the driver sync mounted secrets into native
# Kubernetes Secrets, which the workload (task 9) consumes via ``secretKeyRef``
# (design DD-5). Installed into ``kube-system``. Bump within the 1.4.x line.
CSI_DRIVER_CHART = "secrets-store-csi-driver"
CSI_DRIVER_REPOSITORY = (
    "https://kubernetes-sigs.github.io/secrets-store-csi-driver/charts"
)
CSI_DRIVER_NAMESPACE = "kube-system"
CSI_DRIVER_VERSION = "1.4.6"

# AWS provider for the Secrets Store CSI Driver (ASCP). Pinned to a known-good
# 0.3.x chart release; this is the AWS-maintained provider that lets the CSI
# driver fetch objects from AWS Secrets Manager. Installed into ``kube-system``
# and ordered after the CSI driver (the provider is useless without the driver).
# The AWS provider chart is versioned independently of the driver; bump to a
# newer 0.3.x release as they are published.
ASCP_PROVIDER_CHART = "secrets-store-csi-driver-provider-aws"
ASCP_PROVIDER_REPOSITORY = "https://aws.github.io/secrets-store-csi-driver-provider-aws"
ASCP_PROVIDER_NAMESPACE = "kube-system"
ASCP_PROVIDER_VERSION = "0.3.9"


class ClusterStack(Stack):
    """Amazon EKS cluster (Kubernetes 1.30) with an explicit managed node group.

    The stack creates a cluster with no default capacity and attaches a single
    managed node group sized from :class:`AppConfig`. CDK enables the cluster's
    OIDC provider automatically, which is what makes IRSA available to the
    Agent_Service_Account created later in ``WorkloadStack``.

    Attributes:
        cluster: the EKS cluster hosting the workload, typed as
            :class:`~aws_cdk.aws_eks.ICluster`. It is either a newly-created
            ``eks.Cluster`` (new-cluster path) or an imported cluster returned by
            ``eks.Cluster.from_cluster_attributes`` (existing-cluster path). Both
            satisfy ``ICluster``, so downstream stacks (``ObservabilityStack``,
            ``WorkloadStack``) consume it uniformly by object reference.
        cert_manager_chart: the cert-manager :class:`~aws_cdk.aws_eks.HelmChart`
            installed as a cluster prerequisite (task 5.3, Requirements 8.7/8.8).
        csi_driver_chart: the AWS Secrets Store CSI Driver
            :class:`~aws_cdk.aws_eks.HelmChart`.
        ascp_provider_chart: the AWS provider (ASCP) for the Secrets Store CSI
            Driver, ordered after :attr:`csi_driver_chart`.

    The three prerequisite charts are exposed as public attributes so
    ``WorkloadStack`` (task 9) can add explicit ``node.add_dependency(...)``
    ordering against them, guaranteeing the add-ons are installed before the
    workload that relies on them (design DD-7).

    This stack also builds the agent container image (:attr:`agent_image`). The
    image is built *here* rather than in ``WorkloadStack`` on purpose: the
    workload's Kubernetes ``Deployment``/``Service`` are attached to the cluster
    via ``cluster.add_manifest(...)``, so CDK materializes those manifests in
    *this* (the cluster-owning) stack. Building the image here keeps the
    Deployment's image reference intra-stack. If the image were built in
    ``WorkloadStack``, the Deployment (in this stack) would consume the
    content-derived image tag through a cross-stack CloudFormation export, and
    CloudFormation refuses to update an export's value while another stack still
    imports it — which deadlocks every redeploy that changes ``docker/`` with
    "Cannot update export ... as it is in use." ``WorkloadStack`` consumes the
    published image by reference (``cluster_stack.agent_image.image_uri``).

    Attributes:
        agent_image: the :class:`~stacks.agent_image.AgentImage` construct that
            builds and publishes the agent container image to a dedicated ECR
            repository (in CodeBuild, no local Docker). Exposed so
            ``WorkloadStack`` can reference the published ``image_uri`` and order
            its Deployment after the image build.
    """

    cluster: eks.ICluster
    cert_manager_chart: eks.HelmChart
    csi_driver_chart: eks.HelmChart
    ascp_provider_chart: eks.HelmChart
    agent_image: AgentImage

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        vpc: ec2.IVpc,
        **kwargs: Any,
    ) -> None:
        """Create the EKS cluster and its managed node group.

        Args:
            scope: the parent construct (typically the CDK ``App``).
            construct_id: this stack's logical id within the app.
            config: the resolved :class:`AppConfig`. Node sizing and the agent
                name are read from it; sizing was already validated fail-fast in
                ``AppConfig`` (Requirement 2.2), so this stack can trust it.
            vpc: the VPC to place the cluster in, passed by object reference from
                ``NetworkStack.vpc`` so CDK manages the cross-stack reference.
            **kwargs: forwarded to :class:`aws_cdk.Stack` — notably ``env`` so the
                cluster is created in the Operator's resolved account/region.
        """
        super().__init__(scope, construct_id, **kwargs)

        self._config = config
        self._vpc = vpc

        # Select the provisioning path from the resolved config. When the
        # Operator supplies ``existingClusterName`` (``creates_cluster`` is
        # ``False``), the existing cluster is imported and neither a new cluster
        # nor a managed node group is created (Requirement 2.5). Otherwise a new
        # cluster and node group are provisioned (task 5.1).
        if config.creates_cluster:
            self.cluster = self._create_cluster()
        else:
            self.cluster = self._import_cluster()

        # Cluster prerequisites via Helm (task 5.3, Requirements 8.7/8.8).
        # cert-manager and the Secrets Store CSI Driver + ASCP provider are
        # installed against ``self.cluster``; the created charts are exposed as
        # attributes so the workload stack can depend on them (design DD-7).
        self._install_cluster_prerequisites()

        # Build the agent container image in this stack (see the class docstring
        # for why it lives here and not in WorkloadStack). WorkloadStack consumes
        # the published image via ``cluster_stack.agent_image.image_uri`` and its
        # Deployment — which also materializes in this stack — references the
        # image tag intra-stack, avoiding a cross-stack export deadlock.
        self.agent_image = AgentImage(self, "AgentImage", config=config)

        # Stack outputs an Operator needs to reach the cluster (Requirement 2.6).
        self._emit_outputs()

    # ------------------------------------------------------------------
    # New-cluster creation path (task 5.1).
    # ------------------------------------------------------------------

    def _create_cluster(self) -> eks.Cluster:
        """Create the EKS cluster and attach the explicit managed node group.

        Realizes design decision DD-3: Kubernetes 1.30, ``default_capacity=0``
        plus an explicit node group, an auto-enabled OIDC provider for IRSA, and
        a matching v30 ``kubectl`` layer.

        Returns:
            The created :class:`aws_cdk.aws_eks.Cluster`.
        """
        cluster = eks.Cluster(
            self,
            "AgentCluster",
            version=KUBERNETES_VERSION,
            # Place the cluster in the dedicated VPC from NetworkStack.
            vpc=self._vpc,
            # No default capacity: node capacity is supplied by the explicit
            # managed node group below so sizing is unambiguous and validated
            # (design DD-3, Requirement 2.1).
            default_capacity=0,
            # Use EKS access entries (plus the legacy aws-auth ConfigMap) for
            # authorization. This is what lets ``grant_access`` below wire an
            # operator's IAM principal to Kubernetes RBAC. Without an API-capable
            # auth mode, a human running ``kubectl`` against a CDK-created cluster
            # hits "you must be logged in to the server" because only the CDK
            # deploy role is mapped.
            authentication_mode=eks.AuthenticationMode.API_AND_CONFIG_MAP,
            # The kubectl layer lets CDK apply Kubernetes manifests and Helm
            # charts (used by tasks 5.3, 8, and 9). Pinned to the v30 layer to
            # match the 1.30 control plane.
            kubectl_layer=KubectlV30Layer(self, "KubectlLayer"),
        )

        # Grant an operator's IAM principal Kubernetes cluster-admin via an EKS
        # access entry, if one was supplied (``clusterAdminRoleArn``). This is the
        # declarative fix for the common post-deploy "you must be logged in to the
        # server" 401: the person who will run ``kubectl`` is mapped at deploy
        # time instead of having to add an access entry by hand afterward. When no
        # ARN is supplied, nothing is granted and the README documents the manual
        # ``aws eks create-access-entry`` steps.
        admin_arn = self._config.cluster_admin_role_arn.strip()
        if admin_arn:
            cluster.grant_access(
                "OperatorClusterAdmin",
                admin_arn,
                [
                    eks.AccessPolicy.from_access_policy_name(
                        "AmazonEKSClusterAdminPolicy",
                        access_scope_type=eks.AccessScopeType.CLUSTER,
                    )
                ],
            )

        # Explicit managed node group sized from AppConfig (Requirement 2.1).
        # min <= desired <= max, non-negative, and max <= 100 were all validated
        # in AppConfig before synth (Requirement 2.2), so the values are trusted
        # here. Defaults (t3.medium / 2 / 1 / 3) come from AppConfig
        # (Requirement 2.3).
        cluster.add_nodegroup_capacity(
            NODEGROUP_ID,
            instance_types=[ec2.InstanceType(self._config.node_type)],
            desired_size=self._config.nodes_desired,
            min_size=self._config.nodes_min,
            max_size=self._config.nodes_max,
        )

        # OIDC provider for IRSA (Requirement 2.4). ``eks.Cluster`` enables the
        # OIDC provider automatically; accessing ``open_id_connect_provider``
        # here both documents the dependency and forces the provider to be
        # materialized so downstream IRSA service accounts (WorkloadStack) can
        # rely on it being present.
        assert cluster.open_id_connect_provider is not None

        return cluster

    # ------------------------------------------------------------------
    # Existing-cluster path (task 5.2).
    # ------------------------------------------------------------------

    def _import_cluster(self) -> eks.ICluster:
        """Import a pre-existing EKS cluster instead of creating one.

        Realizes Requirement 2.5: when the Operator supplies
        ``existingClusterName``, the app deploys the Agent_Workload into that
        cluster and does **not** create a new EKS cluster or managed node group.
        The cluster is imported with ``eks.Cluster.from_cluster_attributes(...)``,
        which returns an :class:`~aws_cdk.aws_eks.ICluster` reference (not a fully
        managed ``eks.Cluster``); the reference carries just enough attributes for
        CDK to apply Kubernetes manifests and Helm charts against the cluster in
        later tasks.

        Attributes supplied to the import:

        * ``cluster_name`` — the existing cluster's name from
          ``config.existing_cluster_name``. This is the one attribute CDK
          requires to identify the target cluster.
        * ``kubectl_layer`` — the matching v30 ``kubectl`` layer, so the CDK
          kubectl provider can run ``kubectl``/``helm`` against the cluster when
          tasks 5.3, 8, and 9 apply manifests. Without a ``kubectl_layer`` CDK
          cannot apply Kubernetes resources to an imported cluster.
        * ``vpc`` — the VPC passed into this stack, so downstream constructs that
          need subnet/security-group context resolve against the right network.

        Known limitations of the existing-cluster path (documented rather than
        guessed, because the required inputs are not part of ``AppConfig`` today):

        * **OIDC provider / IRSA.** ``cluster.add_service_account(...)`` (used by
          ``WorkloadStack`` in task 8 to wire the Agent_IAM_Role via IRSA)
          requires the imported cluster's ``open_id_connect_provider``. That
          provider's ARN is not currently a Deployment_Input, so it is not
          supplied here. To use IRSA against an imported cluster, a future change
          should add an ``existingClusterOidcProviderArn`` input and pass
          ``open_id_connect_provider=eks.OpenIdConnectProvider
          .from_open_id_connect_provider_arn(...)`` to this import. Until then,
          the existing-cluster path imports the cluster and can apply manifests,
          but IRSA service-account creation against it is not wired.
        * **kubectl access role.** ``from_cluster_attributes`` also accepts
          ``kubectl_role_arn`` (an IAM role already mapped into the cluster's
          ``aws-auth`` with admin rights) so the CDK kubectl provider can
          authenticate. It is optional and not an ``AppConfig`` input yet; for
          clusters where the default provider role lacks access, a future change
          should thread an ``existingClusterKubectlRoleArn`` input through here.

        Returns:
            The imported :class:`~aws_cdk.aws_eks.ICluster`.
        """
        return eks.Cluster.from_cluster_attributes(
            self,
            "AgentCluster",
            # Identify the pre-existing cluster by name (Requirement 2.5). The
            # value is validated/normalized by AppConfig; ``creates_cluster`` is
            # False only when a concrete name (not the "none" sentinel) was set.
            cluster_name=self._config.existing_cluster_name,
            # Matching v30 kubectl layer so CDK can apply manifests/Helm charts
            # to the imported cluster in later tasks (5.3, 8, 9).
            kubectl_layer=KubectlV30Layer(self, "KubectlLayer"),
            # Resolve subnet/security-group context against the provided VPC.
            vpc=self._vpc,
        )

    # ------------------------------------------------------------------
    # Task 5.3 extension point — cluster prerequisites (Helm).
    # ------------------------------------------------------------------

    def _install_cluster_prerequisites(self) -> None:
        """Install cluster add-ons required before the workload (task 5.3).

        Installs, via ``self.cluster.add_helm_chart(...)``, the two cluster
        prerequisites the sample needs before the Agent_Workload can run
        (design DD-7, Requirements 8.7, 8.8):

        #. **cert-manager** (pinned to :data:`CERT_MANAGER_VERSION`, a v1.13.x
           release) into its own ``cert-manager`` namespace, with its CRDs
           installed via ``installCRDs: true``. This replaces the ``kubectl
           apply`` of cert-manager in the old ``deploy.sh`` and makes the
           prerequisite part of the declarative graph.
        #. **AWS Secrets Store CSI Driver** (:data:`CSI_DRIVER_VERSION`) plus the
           **AWS provider / ASCP** (:data:`ASCP_PROVIDER_VERSION`) into
           ``kube-system``. The driver is configured with
           ``syncSecret.enabled: true`` so it can materialize mounted secrets as
           native Kubernetes Secrets, which the workload consumes via
           ``secretKeyRef`` (design DD-5). The provider is what lets the driver
           read from AWS Secrets Manager.

        Ordering:

        * The ASCP provider is installed **after** the CSI driver — the provider
          registers against the driver and is meaningless without it — via an
          explicit ``node.add_dependency``.
        * cert-manager is independent of the CSI stack, so no ordering is imposed
          between them; both are simply installed before the workload.
        * The workload lives in a separate stack (``WorkloadStack``, task 9) that
          consumes this cluster. Because these charts are exposed as public
          attributes, the workload stack orders itself after them with explicit
          ``node.add_dependency(...)`` calls, satisfying "prerequisites are
          ordered before the workload via CDK dependencies" (design DD-7).

        Both provisioning paths reach this method (``self.cluster`` is an
        :class:`~aws_cdk.aws_eks.ICluster` in either case), so the prerequisites
        are installed whether the cluster was created (task 5.1) or imported
        (task 5.2). ``add_helm_chart`` uses the cluster's ``kubectl`` provider,
        which both paths configure with a v30 ``kubectl`` layer.
        """
        # 1. cert-manager (v1.13.x) in its own namespace, CRDs included.
        #    v1.13 installs CRDs through the ``installCRDs`` value; the
        #    ``crds.enabled`` form only exists in v1.15+ (see CERT_MANAGER_VERSION).
        self.cert_manager_chart = self.cluster.add_helm_chart(
            "CertManager",
            chart=CERT_MANAGER_CHART,
            repository=CERT_MANAGER_REPOSITORY,
            namespace=CERT_MANAGER_NAMESPACE,
            version=CERT_MANAGER_VERSION,
            # cert-manager's namespace is dedicated to it and does not exist by
            # default, so let the chart install create it.
            create_namespace=True,
            values={
                # Install cert-manager's CRDs as part of the chart (v1.13 idiom).
                "installCRDs": True,
            },
        )

        # 2. AWS Secrets Store CSI Driver in kube-system. ``syncSecret.enabled``
        #    lets it sync mounted secrets into native Kubernetes Secrets so the
        #    workload can read them via ``secretKeyRef`` (design DD-5).
        self.csi_driver_chart = self.cluster.add_helm_chart(
            "SecretsStoreCsiDriver",
            chart=CSI_DRIVER_CHART,
            repository=CSI_DRIVER_REPOSITORY,
            namespace=CSI_DRIVER_NAMESPACE,
            version=CSI_DRIVER_VERSION,
            # kube-system already exists; do not attempt to create it.
            create_namespace=False,
            values={
                "syncSecret": {
                    "enabled": True,
                },
            },
        )

        # 3. AWS provider (ASCP) for the CSI driver, also in kube-system.
        self.ascp_provider_chart = self.cluster.add_helm_chart(
            "SecretsStoreCsiProviderAws",
            chart=ASCP_PROVIDER_CHART,
            repository=ASCP_PROVIDER_REPOSITORY,
            namespace=ASCP_PROVIDER_NAMESPACE,
            version=ASCP_PROVIDER_VERSION,
            create_namespace=False,
        )

        # Internal ordering: the AWS provider registers against the CSI driver,
        # so install the driver first. cert-manager is independent and needs no
        # ordering relative to the CSI stack.
        self.ascp_provider_chart.node.add_dependency(self.csi_driver_chart)

    # ------------------------------------------------------------------
    # Stack outputs (Requirement 2.6).
    # ------------------------------------------------------------------

    def _emit_outputs(self) -> None:
        """Emit the cluster name and the ``update-kubeconfig`` command.

        Requirement 2.6: when the cluster is created, the app surfaces the
        cluster name and the exact command the Operator runs to point their
        local kubeconfig at the cluster. The cluster name is a synth-time token
        that CloudFormation resolves to the concrete name on deploy.
        """
        CfnOutput(
            self,
            "ClusterName",
            value=self.cluster.cluster_name,
            description="Name of the EKS cluster hosting the agent workload.",
        )
        CfnOutput(
            self,
            "UpdateKubeconfigCommand",
            value=(
                f"aws eks update-kubeconfig --name {self.cluster.cluster_name} "
                f"--region {self.region}"
            ),
            description=(
                "Run this to point your local kubeconfig at the EKS cluster."
            ),
        )
