"""Container image build (in-cloud, no local Docker) + dedicated ECR repo.

``AgentImage`` is a reusable :class:`~constructs.Construct` that turns the
unchanged ``docker/`` build context into a published, content-tagged image the
Agent_Workload can reference — **without requiring a local Docker daemon on the
machine that runs ``cdk deploy``**.

Why no local Docker (design deviation from the original DD-4)
-------------------------------------------------------------
The original design built the image with ``aws_ecr_assets.DockerImageAsset``,
which shells out to ``docker build`` locally and therefore needs a running
Docker-compatible daemon at deploy time. Operators frequently do not have (or
do not want) a local daemon, so this construct instead builds the image **in AWS
CodeBuild at deploy time** using the ``deploy-time-build`` construct's
``ContainerImageBuild``:

* the ``docker/`` directory is uploaded as an ordinary **S3 file asset** (a zip,
  *not* a Docker image asset), so staging it needs no local Docker;
* a CodeBuild project then runs ``docker build`` + ``docker push`` **in the
  cloud** and pushes the result into a dedicated ECR repository;
* the resulting image URI is handed to the Kubernetes ``Deployment``.

The only local requirement becomes zipping the build context, which the CDK CLI
does natively. The trade-off is that image builds now run in CodeBuild (a couple
of extra minutes on the first deploy and whenever ``docker/`` changes) rather
than on the operator's machine — an acceptable cost for removing the daemon
prerequisite. See ``ContainerImageBuild`` docs:
https://github.com/tmokmss/deploy-time-build

Why a Construct rather than inline in ``WorkloadStack``
------------------------------------------------------
The design's repository layout folds the image into ``WorkloadStack``. This
module factors it into a small, reusable ``Construct`` so tasks 8/9 consume the
published image purely by reference (``agent_image.image_uri``) and so it can be
unit-tested in isolation. ``WorkloadStack`` instantiates one ``AgentImage`` when
``app.py`` does not pass a pre-published image URI.

What this construct creates
---------------------------
#. **A dedicated, human-named ``ecr.Repository``** — named after the agent
   (``config.agent_name``) so a customer browsing ECR sees
   ``langgraph-shopping-agent`` rather than a hash in the shared bootstrap repo
   (Requirement 3.1). ``image_scan_on_push`` is enabled, and
   ``empty_on_delete`` + ``RemovalPolicy.DESTROY`` make ``cdk destroy`` empty and
   remove the repo cleanly (Requirement 9.1).
#. **A ``ContainerImageBuild``** — uploads ``docker/`` to S3, builds + pushes the
   image in CodeBuild into the named repo at a content-derived tag
   (Requirements 3.2, 3.3, 3.5, 3.6, 3.7). CDK's asset hashing means the build
   only re-runs when ``docker/`` contents change.

Digest / tag strategy (Requirement 3.4)
---------------------------------------
``ContainerImageBuild`` tags the pushed image with a hash derived from the build
context, so the tag **changes** when ``docker/`` changes (Requirement 3.5) and is
**reused** unchanged otherwise (Requirement 3.6). :attr:`image_uri` is the
repository URI at that content-derived tag — a deterministic, content-addressed
reference (the task-approved stand-in for a raw ``@sha256`` digest pin, since the
pushed manifest digest is not known at synth time).

This module is intentionally *not* instantiated in ``app.py``; the stacks and
constructs are composed in task 10, and ``WorkloadStack`` (task 8/9) instantiates
this construct.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aws_cdk import RemovalPolicy
from aws_cdk import aws_ecr as ecr
from deploy_time_build import ContainerImageBuild
from constructs import Construct

from .app_config import AppConfig

# ---------------------------------------------------------------------------
# Build-context location.
#
# The ``docker/`` directory is resolved relative to this file (repo root's
# ``docker/``) rather than the process working directory, so the construct builds
# the correct context regardless of where ``cdk`` is invoked from and so unit
# tests that synthesize it do not depend on cwd. ``docker/`` is the unchanged
# build context (Dockerfile, requirements.txt, app/) — see design Correctness
# Property 7; this module never modifies it.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_CONTEXT_DIR = str(_REPO_ROOT / "docker")


class AgentImage(Construct):
    """Builds the agent image in CodeBuild and publishes it to a named ECR repo.

    See the module docstring for the no-local-Docker rationale and the tag
    strategy.

    Attributes:
        repository: the dedicated, human-named :class:`aws_cdk.aws_ecr.Repository`
            (``config.agent_name``) the image is pushed into. Exposed so
            downstream stacks can grant pull permission or reference it directly.
        image_uri: the content-tagged image reference the Kubernetes ``Deployment``
            should reference (Requirement 3.4).
        image_tag: the content-derived tag under which the image is published.
        build: the ``deploy-time-build`` :class:`ContainerImageBuild` construct
            that runs the CodeBuild build+push. Exposed so downstream stacks can
            order themselves after the image is published
            (``node.add_dependency(agent_image.build)``).
        deployment: alias of :attr:`build`, kept so existing call sites that
            depended on ``agent_image.deployment`` for ordering continue to work.
    """

    repository: ecr.Repository
    image_uri: str
    image_tag: str
    build: ContainerImageBuild
    deployment: ContainerImageBuild

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        **kwargs: Any,
    ) -> None:
        """Create the named repo and the in-cloud image build.

        Args:
            scope: the parent construct (the WorkloadStack in task 8/9, or a
                throwaway stack in tests).
            construct_id: this construct's logical id within the scope.
            config: the resolved :class:`AppConfig`. ``config.agent_name`` (already
                validated to the lowercase ECR-safe charset in ``AppConfig``) is
                used verbatim as the ECR repository name (Requirement 11.5).
            **kwargs: forwarded to :class:`~constructs.Construct`.
        """
        super().__init__(scope, construct_id, **kwargs)

        self._config = config

        # (1) Create a dedicated, human-named ECR repository. The agent name is
        # used directly as the repository name (Requirement 3.1, 11.5); it was
        # validated in ``AppConfig`` to the intersection of ECR/log-group/K8s
        # naming rules, so it is already a legal, lowercase ECR repo name.
        self.repository = ecr.Repository(
            self,
            "Repository",
            repository_name=config.agent_name,
            # Scan images for vulnerabilities as they are pushed (Requirement 3.1
            # posture: a cleanly-managed repo for the sample).
            image_scan_on_push=True,
            # Encrypt the repository at rest with an AWS-managed KMS key rather
            # than the default AES-256. Resolves Checkov CKV_AWS_136 and needs no
            # extra key/grant wiring (AWS manages the ECR service key).
            encryption=ecr.RepositoryEncryption.KMS,
            # ``cdk destroy`` must empty and remove the repo cleanly: DESTROY
            # removal policy + auto-delete of the images inside it (Requirement
            # 9.1). ``empty_on_delete`` requires the DESTROY removal policy.
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        # (2) Build the image in AWS CodeBuild (not local Docker) from the unchanged
        # ``docker/`` context and push it into the named repo. ``deploy-time-build``
        # uploads ``docker/`` as an S3 file asset (no local daemon), runs
        # ``docker build`` + push in CodeBuild, and tags the image with a
        # content-derived hash so it only rebuilds when ``docker/`` changes
        # (Requirements 3.2, 3.3, 3.5, 3.6, 3.7). The CodeBuild default build
        # environment is x86_64/amd64, matching the EKS ``t3.medium`` nodes, so no
        # explicit ``platform`` is needed.
        self.build = ContainerImageBuild(
            self,
            "AgentImageBuild",
            directory=DOCKER_CONTEXT_DIR,
            repository=self.repository,
        )
        # Alias kept for existing ordering call sites (WorkloadStack depends on
        # ``agent_image.deployment`` so the image is published before the workload
        # Deployment applies).
        self.deployment = self.build

        # Content-derived tag + the full image reference the Deployment consumes
        # (Requirement 3.4).
        self.image_tag = self.build.image_tag
        self.image_uri = self.build.image_uri
