"""Application configuration for the CDK deployment (input resolution).

``AppConfig`` is the single input contract for the CDK app. It resolves every
Deployment_Input from four sources, in the precedence order mandated by
Requirement 5.2:

    CDK context (``cdk.json`` / ``-c``)  >  environment variable  >
    example inputs file (``config.json``)  >  built-in default

Account and region are resolved from the standard CDK/AWS environment
(``CDK_DEFAULT_ACCOUNT``, ``CDK_DEFAULT_REGION`` / ``AWS_REGION``) when the
Operator does not supply them (Requirement 5.3).

Beyond resolution, the class performs fail-fast validation (12-digit account,
region pattern, agent-name charset/length, node sizing, and required
per-platform values) via the ``validate()`` method, which ``from_context``
invokes before returning so invalid or missing inputs raise before synth
(Requirements 5.6, 5.7, 11.3, 11.6, 2.2).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# --- Built-in defaults (see the AppConfig table in design.md) -------------------
# These mirror the values documented in the design and in cdk.json context so the
# app has sensible behavior when nothing is overridden.
DEFAULT_AGENT_NAME = "langgraph-shopping-agent"
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-5"
DEFAULT_JUDGE_MODEL_ID = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
DEFAULT_NODE_TYPE = "t3.medium"
DEFAULT_NODES_DESIRED = 2
DEFAULT_NODES_MIN = 1
DEFAULT_NODES_MAX = 3
DEFAULT_EXISTING_CLUSTER_NAME = "none"  # sentinel: CDK creates a cluster
DEFAULT_LANGSMITH_ENABLED = True
DEFAULT_LANGSMITH_PROJECT = "langgraph-shopping-agent"
DEFAULT_LANGFUSE_ENABLED = True
DEFAULT_LANGFUSE_HOST = "https://us.cloud.langfuse.com"
DEFAULT_AGENTCORE_ENABLED = True
# Optional IAM role/user ARN to grant Kubernetes cluster-admin via an EKS access
# entry at deploy time. Empty = grant nothing (the Operator adds access manually
# afterward). Lets the person who will run ``kubectl`` reach the cluster without
# the post-deploy "you must be logged in to the server" access-entry dance.
DEFAULT_CLUSTER_ADMIN_ROLE_ARN = ""

# Sentinel used to mean "the Operator did not override this value".
_UNSET = object()

# Name of the optional example inputs file read as the lowest-precedence
# explicit source (above built-in defaults).
CONFIG_FILE_NAME = "config.json"

# --- Validation patterns / limits (task 2.2, fail-fast before synth) ------------
# A 12-digit AWS account ID (Requirement 5.7).
ACCOUNT_ID_PATTERN = re.compile(r"^\d{12}$")
# An AWS region identifier, e.g. ``us-east-1``, ``ap-southeast-2`` (Requirements
# 5.7, 11.3). Two letters, one or more lowercase words, a trailing digit.
REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-\d$")
# Agent/service name: the intersection of ECR repository, CloudWatch log-group,
# and Kubernetes resource naming rules (Requirement 11.6). Lowercase
# alphanumerics with internal hyphens, must start and end alphanumeric.
AGENT_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# Upper bound on the agent name length that stays within all three services'
# limits (Kubernetes label/DNS constraints are the tightest practical cap).
AGENT_NAME_MAX_LENGTH = 40
# Upper bound on the EKS managed node group max size (Requirement 2.2).
NODES_MAX_LIMIT = 100
# A valid EKS access-entry principal ARN: an IAM role or user ARN. EKS access
# entries accept only IAM principals, e.g.
# ``arn:aws:iam::123456789012:role/MyRole`` — not STS assumed-role *session*
# ARNs (``arn:aws:sts::...:assumed-role/...``), which is what
# ``aws sts get-caller-identity`` returns when running under an assumed role
# (SSO, ``sts assume-role``). Partition is left open (aws, aws-cn, aws-us-gov).
CLUSTER_ADMIN_ROLE_ARN_PATTERN = re.compile(
    r"^arn:aws[a-z0-9-]*:iam::\d{12}:(role|user)/.+$"
)


def _has_value(value: Any) -> bool:
    """Return whether a value is present (non-``None`` and non-empty when trimmed).

    Used by validation to treat ``None`` and whitespace-only strings uniformly
    as "missing" (Requirements 5.6, 6.1).
    """
    if value is None:
        return False
    return str(value).strip() != ""


def _derive_secret_name(agent_name: str, platform: str) -> str:
    """Derive the default Secrets Manager name for a platform from the agent name.

    Produces ``agent-observability/<agent>/<platform>`` (design DD-5). Used only
    when the Operator has not supplied an explicit secret name.
    """
    return f"agent-observability/{agent_name}/{platform}"


class _Resolver:
    """Resolves a single input across the four sources in precedence order.

    Precedence (highest first): CDK context, environment variable, config file,
    built-in default. Values coming from environment variables arrive as strings
    and are coerced to the requested type; values from context and the JSON
    config file are already typed.
    """

    def __init__(self, node_context: Any, config_file: dict[str, Any]) -> None:
        self._node_context = node_context
        self._config_file = config_file

    def _from_context(self, key: str) -> Any:
        """Read a value from CDK context (``node.try_get_context``)."""
        if self._node_context is None:
            return None
        try:
            return self._node_context.try_get_context(key)
        except Exception:  # pragma: no cover - defensive, context is optional
            return None

    def resolve(
        self,
        *,
        context_key: str,
        env_var: Optional[str],
        default: Any,
        cast: str = "str",
    ) -> Any:
        """Resolve one input using the documented precedence order.

        Args:
            context_key: the CDK context key (also used to look up the config file).
            env_var: the environment variable name, or ``None`` if the input has no
                environment-variable source.
            default: the built-in default returned when no source supplies a value.
            cast: one of ``"str"``, ``"int"``, ``"bool"`` for type coercion.
        """
        # 1. CDK context (cdk.json / -c) — highest precedence.
        ctx_value = self._from_context(context_key)
        if ctx_value is not None:
            return _coerce(ctx_value, cast)

        # 2. Environment variable.
        if env_var:
            env_value = os.environ.get(env_var)
            if env_value is not None and env_value != "":
                return _coerce(env_value, cast)

        # 3. Example inputs file (config.json).
        if context_key in self._config_file:
            file_value = self._config_file[context_key]
            if file_value is not None:
                return _coerce(file_value, cast)

        # 4. Built-in default — lowest precedence.
        return default


def _coerce(value: Any, cast: str) -> Any:
    """Coerce a resolved value to the requested type.

    Environment variables are always strings; context/config values may already
    be typed. This normalizes both into the type the field expects.
    """
    if cast == "str":
        return str(value)
    if cast == "int":
        if isinstance(value, bool):
            # bool is a subclass of int; treat it as an int value explicitly.
            return int(value)
        if isinstance(value, int):
            return value
        return int(str(value).strip())
    if cast == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    raise ValueError(f"Unknown cast type: {cast}")


def _context_key_to_env_var(context_key: str) -> str:
    """Map a camelCase context key to an UPPER_SNAKE_CASE environment variable.

    Keeps the context key as the single source of truth for input names while
    providing a predictable environment-variable override (e.g. ``agentName`` ->
    ``AGENT_NAME``, ``nodesDesired`` -> ``NODES_DESIRED``).
    """
    out: list[str] = []
    for ch in context_key:
        if ch.isupper():
            out.append("_")
            out.append(ch)
        else:
            out.append(ch.upper())
    return "".join(out)


def _load_config_file(config_path: Optional[Path]) -> dict[str, Any]:
    """Load the optional example inputs file (``config.json``) if present.

    Missing or unreadable files resolve to an empty mapping so the file is a
    purely optional, lowest-precedence explicit source.
    """
    if config_path is None:
        config_path = Path.cwd() / CONFIG_FILE_NAME
    try:
        if config_path.is_file():
            with config_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        # An unreadable or malformed config file is treated as absent here;
        # explicit validation of inputs happens in task 2.2.
        return {}
    return {}


@dataclass
class AppConfig:
    """Resolved Deployment_Inputs for the CDK app.

    Every field is resolved via :meth:`from_context` using the precedence
    ``context > env > config file > default``. Account and region are optional
    at this stage: when not supplied they are resolved from the CDK/AWS
    environment, and may be ``None`` if the environment does not provide them
    (that condition is surfaced by validation in task 2.2).
    """

    # AWS environment (resolved from CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION).
    account: Optional[str]
    region: Optional[str]

    # Core agent / model inputs.
    agent_name: str
    model_id: str
    judge_model_id: str

    # EKS node group sizing.
    node_type: str
    nodes_desired: int
    nodes_min: int
    nodes_max: int

    # Cluster reuse ("none" sentinel means CDK provisions a new cluster).
    existing_cluster_name: str

    # Optional IAM principal ARN granted Kubernetes cluster-admin via an EKS
    # access entry at deploy time ("" = none; Operator grants access manually).
    cluster_admin_role_arn: str

    # LangSmith platform.
    langsmith_enabled: bool
    langsmith_project: str
    langsmith_secret_name: str

    # Langfuse platform.
    langfuse_enabled: bool
    langfuse_host: str
    langfuse_secret_name: str

    # AgentCore platform.
    agentcore_enabled: bool

    @property
    def creates_cluster(self) -> bool:
        """Whether the app should provision a new EKS cluster.

        ``True`` unless the Operator supplied an existing cluster name (any value
        other than the ``"none"`` sentinel, case-insensitive, after trimming).
        """
        return self.existing_cluster_name.strip().lower() in ("", "none")

    @classmethod
    def from_context(
        cls,
        app: Any,
        *,
        config_path: Optional[Path] = None,
    ) -> "AppConfig":
        """Build an :class:`AppConfig` from a CDK ``App``'s context, env, and file.

        Resolution precedence for every input (Requirement 5.2):
        CDK context (``node.try_get_context``) > environment variable >
        ``config.json`` example inputs file > built-in default.

        Args:
            app: a CDK ``App`` (or any object exposing ``node.try_get_context``).
            config_path: optional path to the example inputs file; defaults to
                ``config.json`` in the current working directory.
        """
        node_context = getattr(app, "node", None)
        config_file = _load_config_file(config_path)
        resolver = _Resolver(node_context, config_file)

        def get(context_key: str, default: Any, cast: str = "str") -> Any:
            return resolver.resolve(
                context_key=context_key,
                env_var=_context_key_to_env_var(context_key),
                default=default,
                cast=cast,
            )

        # AWS account/region resolve from the standard CDK/AWS environment
        # (Requirement 5.3). Context still takes precedence if explicitly set.
        account = resolver.resolve(
            context_key="account",
            env_var="CDK_DEFAULT_ACCOUNT",
            default=None,
            cast="str",
        )
        region = resolver.resolve(
            context_key="region",
            env_var="CDK_DEFAULT_REGION",
            default=None,
            cast="str",
        )
        if region is None:
            # Fall back to AWS_REGION when CDK_DEFAULT_REGION is not set.
            aws_region = os.environ.get("AWS_REGION")
            if aws_region:
                region = aws_region

        agent_name = get("agentName", DEFAULT_AGENT_NAME)

        # Secret names default to a value derived from the (resolved) agent name
        # unless the Operator overrides them explicitly (design DD-5).
        langsmith_secret_name = get(
            "langsmithSecretName",
            _derive_secret_name(agent_name, "langsmith"),
        )
        langfuse_secret_name = get(
            "langfuseSecretName",
            _derive_secret_name(agent_name, "langfuse"),
        )

        config = cls(
            account=account,
            region=region,
            agent_name=agent_name,
            model_id=get("modelId", DEFAULT_MODEL_ID),
            judge_model_id=get("judgeModelId", DEFAULT_JUDGE_MODEL_ID),
            node_type=get("nodeType", DEFAULT_NODE_TYPE),
            nodes_desired=get("nodesDesired", DEFAULT_NODES_DESIRED, cast="int"),
            nodes_min=get("nodesMin", DEFAULT_NODES_MIN, cast="int"),
            nodes_max=get("nodesMax", DEFAULT_NODES_MAX, cast="int"),
            existing_cluster_name=get(
                "existingClusterName", DEFAULT_EXISTING_CLUSTER_NAME
            ),
            cluster_admin_role_arn=get(
                "clusterAdminRoleArn", DEFAULT_CLUSTER_ADMIN_ROLE_ARN
            ),
            langsmith_enabled=get(
                "langsmithEnabled", DEFAULT_LANGSMITH_ENABLED, cast="bool"
            ),
            langsmith_project=get("langsmithProject", DEFAULT_LANGSMITH_PROJECT),
            langsmith_secret_name=langsmith_secret_name,
            langfuse_enabled=get(
                "langfuseEnabled", DEFAULT_LANGFUSE_ENABLED, cast="bool"
            ),
            langfuse_host=get("langfuseHost", DEFAULT_LANGFUSE_HOST),
            langfuse_secret_name=langfuse_secret_name,
            agentcore_enabled=get(
                "agentcoreEnabled", DEFAULT_AGENTCORE_ENABLED, cast="bool"
            ),
        )

        # Fail fast before synth: invalid or missing inputs raise here, before
        # any AWS resource is created or modified (Requirements 5.6, 5.7, 11.3,
        # 11.6, 2.2 — Correctness Property 5).
        config.validate()
        return config

    # ------------------------------------------------------------------
    # Fail-fast validation (task 2.2).
    #
    # ``validate()`` is invoked at the end of ``from_context`` so invalid or
    # missing inputs raise before any AWS resource is synthesized or created
    # (Requirements 5.6, 5.7, 11.3, 11.6, 2.2 — design Correctness Property 5).
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Validate the resolved inputs, raising before synth on any problem.

        Two error styles are used, matching the requirements:

        * Missing required inputs are aggregated into a single error that names
          *every* missing input (Requirement 5.6), so the Operator can fix them
          all at once rather than discovering them one deploy at a time.
        * Malformed inputs (account ID, region, agent name, node sizing) raise a
          specific error identifying the offending input (Requirements 5.7,
          11.3, 11.6, 2.2).

        Raises:
            ValueError: if any required input is missing, or any input is
                malformed.
        """
        # 1. Aggregate all missing required inputs (Requirement 5.6).
        missing = self._collect_missing_required()
        if missing:
            raise ValueError(
                "Missing required Deployment_Input(s): "
                + ", ".join(missing)
                + ". Supply each via CDK context (-c / cdk.json), an environment "
                "variable, or config.json."
            )

        # 2. Validate the format of individual inputs (specific errors).
        self._validate_account()
        self._validate_region()
        self._validate_agent_name()
        self._validate_node_sizing()
        self._validate_cluster_admin_role_arn()

    def _collect_missing_required(self) -> list[str]:
        """Return the names of every required input that has no usable value.

        A value is "missing" when it is ``None`` or empty after trimming
        whitespace. Account and region are always required (resolved from the
        CDK/AWS environment per Requirement 5.3); each enabled platform requires
        its project/host and secret-name inputs (Requirement 5.6, design DD-5).
        """
        missing: list[str] = []

        if not _has_value(self.account):
            missing.append("account (AWS account ID)")
        if not _has_value(self.region):
            missing.append("region (AWS region)")

        # Per-platform required values — only for the platforms the Operator
        # enabled (Requirement 11.7: disabled platforms require nothing).
        if self.langsmith_enabled:
            if not _has_value(self.langsmith_project):
                missing.append("langsmithProject (LangSmith enabled)")
            if not _has_value(self.langsmith_secret_name):
                missing.append("langsmithSecretName (LangSmith enabled)")
        if self.langfuse_enabled:
            if not _has_value(self.langfuse_host):
                missing.append("langfuseHost (Langfuse enabled)")
            if not _has_value(self.langfuse_secret_name):
                missing.append("langfuseSecretName (Langfuse enabled)")

        return missing

    def _validate_account(self) -> None:
        """Require a 12-digit AWS account ID (Requirement 5.7)."""
        # Presence is guaranteed by _collect_missing_required; check the format.
        account = (self.account or "").strip()
        if not ACCOUNT_ID_PATTERN.match(account):
            raise ValueError(
                f"Invalid AWS account ID '{self.account}': the account ID must be "
                "exactly 12 digits."
            )

    def _validate_region(self) -> None:
        """Require a valid AWS region identifier (Requirements 5.7, 11.3)."""
        region = (self.region or "").strip()
        if not REGION_PATTERN.match(region):
            raise ValueError(
                f"Invalid AWS region '{self.region}': the region must match an AWS "
                "region identifier such as 'us-east-1'."
            )

    def _validate_agent_name(self) -> None:
        """Validate the agent/service name charset and length (Requirement 11.6).

        The name must satisfy the intersection of ECR, CloudWatch log-group, and
        Kubernetes naming rules so it can be propagated to every resource
        (Requirement 11.5).
        """
        name = self.agent_name
        if not _has_value(name):
            raise ValueError(
                "Invalid agent/service name: the agent name must not be empty."
            )
        if len(name) > AGENT_NAME_MAX_LENGTH:
            raise ValueError(
                f"Invalid agent/service name '{name}': the name must be at most "
                f"{AGENT_NAME_MAX_LENGTH} characters."
            )
        if not AGENT_NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid agent/service name '{name}': the name must contain only "
                "lowercase letters, digits, and hyphens, and must start and end "
                "with an alphanumeric character."
            )

    def _validate_node_sizing(self) -> None:
        """Validate EKS node group sizing (Requirement 2.2).

        Requires integer counts that are all non-negative, satisfy
        ``min <= desired <= max``, and keep ``max`` within the service limit.
        """
        counts = {
            "nodesMin": self.nodes_min,
            "nodesDesired": self.nodes_desired,
            "nodesMax": self.nodes_max,
        }
        for label, value in counts.items():
            # bool is a subclass of int; reject it explicitly so True/False can
            # never masquerade as a valid count.
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    f"Invalid node sizing input '{label}': expected an integer, got "
                    f"{value!r}."
                )
            if value < 0:
                raise ValueError(
                    f"Invalid node sizing input '{label}': counts must be "
                    f"non-negative, got {value}."
                )

        if self.nodes_max > NODES_MAX_LIMIT:
            raise ValueError(
                f"Invalid node sizing: 'nodesMax' ({self.nodes_max}) must not exceed "
                f"{NODES_MAX_LIMIT}."
            )
        if not (self.nodes_min <= self.nodes_desired <= self.nodes_max):
            raise ValueError(
                "Invalid node sizing: the relationship 'nodesMin <= nodesDesired <= "
                f"nodesMax' is violated (min={self.nodes_min}, "
                f"desired={self.nodes_desired}, max={self.nodes_max})."
            )

    def _validate_cluster_admin_role_arn(self) -> None:
        """Validate the optional EKS cluster-admin principal ARN.

        The input is optional (empty means "grant nothing"), but when supplied it
        must be an IAM **role or user** ARN, because EKS access entries accept
        only IAM principals. The common mistake is passing the output of
        ``aws sts get-caller-identity`` while running under an assumed role, which
        is an STS *assumed-role session* ARN
        (``arn:aws:sts::<account>:assumed-role/<role>/<session>``). EKS rejects
        that at deploy time with an opaque "principalArn parameter format is not
        valid" error, so this validator fails fast before synth with actionable
        guidance instead.
        """
        arn = self.cluster_admin_role_arn.strip()
        if not arn:
            # Empty is valid: no access entry is created (Operator grants access
            # manually afterward).
            return

        if arn.startswith("arn:aws") and ":sts:" in arn and ":assumed-role/" in arn:
            # Try to recover the underlying IAM role name to make the fix obvious.
            role_hint = ""
            try:
                role_name = arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
                account = arn.split(":sts::", 1)[1].split(":", 1)[0]
                role_hint = (
                    f" It looks like you passed the session for role "
                    f"'{role_name}'. For an SSO/permission-set role the IAM role "
                    f"ARN is usually "
                    f"'arn:aws:iam::{account}:role/aws-reserved/sso.amazonaws.com/"
                    f"AWSReservedSSO_<permission-set>_<hash>'; find the exact ARN "
                    f"with `aws iam list-roles`."
                )
            except (IndexError, ValueError):
                role_hint = ""
            raise ValueError(
                f"Invalid clusterAdminRoleArn '{self.cluster_admin_role_arn}': EKS "
                "access entries require an IAM role or user ARN "
                "(arn:aws:iam::<account>:role/<name>), not an STS assumed-role "
                "session ARN. `aws sts get-caller-identity` returns a session ARN "
                "when you are running under an assumed role (e.g. SSO)." + role_hint
            )

        if not CLUSTER_ADMIN_ROLE_ARN_PATTERN.match(arn):
            raise ValueError(
                f"Invalid clusterAdminRoleArn '{self.cluster_admin_role_arn}': "
                "expected an IAM role or user ARN of the form "
                "'arn:aws:iam::<12-digit-account>:role/<name>' or "
                "'arn:aws:iam::<12-digit-account>:user/<name>'. Leave it unset to "
                "skip creating an EKS access entry and grant cluster access "
                "manually after deploy."
            )
