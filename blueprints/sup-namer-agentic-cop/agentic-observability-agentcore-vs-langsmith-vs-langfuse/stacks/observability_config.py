"""Per-platform observability environment configuration (the teaching artifact).

This module is the heart of the sample's teaching value. It shows, in one
place, *exactly* what environment each of the three observability platforms
requires so that a reader can understand per-platform instrumentation without
cross-referencing any other file (Requirements 10.2, 10.3).

It exposes three pure, framework-agnostic functions:

* :func:`agentcore_env` — AgentCore Observability (OTEL → X-Ray + CloudWatch)
* :func:`langsmith_env` — LangSmith (LangChain SDK → LangSmith cloud)
* :func:`langfuse_env`  — Langfuse (Langfuse SDK → Langfuse cloud/self-hosted)

Design intent
-------------
* **Pure functions, plain data.** Each function takes an :class:`AppConfig` and
  returns a ``list[dict]`` — nothing more. There are no CDK, Kubernetes, or AWS
  SDK objects here on purpose: keeping the output as plain Python dictionaries
  makes this module trivially unit-testable (no AWS credentials, no synth) and
  keeps the per-platform requirements readable as data. ``WorkloadStack``
  (task 9) is responsible for translating these dicts into the actual
  Kubernetes container ``env`` entries in the Deployment manifest.

* **Representation.** Each env var is one dict, in one of two shapes that mirror
  the Kubernetes container ``env`` schema (and the old ``chart/values.yaml``):

    - Literal value::

          {"name": "OTEL_SERVICE_NAME", "value": "langgraph-shopping-agent"}

    - Secret reference (value pulled from a synced Kubernetes Secret)::

          {"name": "LANGCHAIN_API_KEY",
           "valueFrom": {"secretKeyRef": {"name": "langsmith-secret",
                                          "key": "api-key"}}}

  The secret ``name`` values (``langsmith-secret`` / ``langfuse-secret``) are the
  *synced Kubernetes Secret* names produced by the Secrets Store CSI Driver
  (design DD-5 / the SecretProviderClass), matching the keys the Deployment has
  always consumed — so ``docker/app/app.py`` needs no change.

* **Enable/disable per platform.** Each function returns ``[]`` (an empty list)
  when its platform is disabled in the :class:`AppConfig`. ``WorkloadStack``
  concatenates only the non-empty blocks, so a disabled platform contributes no
  env vars at all (Requirement 11.7 — enabled-platform consistency).

* **Nothing hard-coded.** Every region- and agent-dependent value is derived
  from ``cfg.region`` and ``cfg.agent_name``. There is no literal ``us-east-1``
  and no hard-coded agent name anywhere in this file (Requirements 11.1, 11.5 —
  name/region propagation is total).

Scope note
----------
The *base runtime* env vars (``AWS_REGION``, ``MODEL_ID``, ``MAX_TOKENS``,
``TEMPERATURE``, ``PORT``) are intentionally **not** produced here. They are not
platform-specific observability config; ``WorkloadStack`` composes them
separately (per design's "Deployment env var shape"). This module is strictly
the three observability blocks.
"""

from __future__ import annotations

from typing import Any

from .app_config import AppConfig

# ---------------------------------------------------------------------------
# Synced Kubernetes Secret names + keys.
#
# These are the names of the Kubernetes Secrets that the AWS Secrets Store CSI
# Driver syncs from AWS Secrets Manager (design DD-5). The Deployment references
# these via ``secretKeyRef`` exactly as the old Helm chart did, so the plaintext
# credential values never appear here, in the CDK template, or in git
# (Requirements 6.2, 6.3, 6.5). The AWS Secrets Manager source names live on the
# AppConfig (``langsmith_secret_name`` / ``langfuse_secret_name``); the *pod*
# only ever sees these synced K8s Secret names.
# ---------------------------------------------------------------------------
# These are Kubernetes Secret *names* and JSON *key names* (not credential
# values), so the ``# nosec B105`` annotations suppress Bandit's
# hardcoded-password false positives.
LANGSMITH_K8S_SECRET_NAME = "langsmith-secret"  # nosec B105 - K8s Secret name, not a secret value  # pragma: allowlist secret
LANGSMITH_API_KEY_SECRET_KEY = "api-key"  # nosec B105 - JSON key name, not a secret value  # pragma: allowlist secret

LANGFUSE_K8S_SECRET_NAME = "langfuse-secret"  # nosec B105 - K8s Secret name, not a secret value  # pragma: allowlist secret
LANGFUSE_PUBLIC_KEY_SECRET_KEY = "public-key"  # nosec B105 - JSON key name, not a secret value  # pragma: allowlist secret
LANGFUSE_SECRET_KEY_SECRET_KEY = "secret-key"  # nosec B105 - JSON key name, not a secret value  # pragma: allowlist secret

# The AgentCore CloudWatch Logs stream and metric namespace are fixed conventions
# expected by the GenAI Observability console (they are not derived from inputs).
AGENTCORE_LOG_STREAM = "runtime-logs"
AGENTCORE_METRIC_NAMESPACE = "bedrock-agentcore"


def _literal(name: str, value: str) -> dict[str, Any]:
    """Return one env var as a literal ``{"name", "value"}`` dict.

    Small helper so each block below reads as a flat list of declarations.
    """
    return {"name": name, "value": value}


def _secret_ref(name: str, secret_name: str, secret_key: str) -> dict[str, Any]:
    """Return one env var as a Kubernetes ``secretKeyRef`` dict.

    The value is not supplied here — the pod resolves it at runtime from the
    named (synced) Kubernetes Secret, so no plaintext credential is ever placed
    in this data structure (Requirements 6.2, 6.3, 6.5).
    """
    return {
        "name": name,
        "valueFrom": {"secretKeyRef": {"name": secret_name, "key": secret_key}},
    }


def _agentcore_log_group(agent_name: str) -> str:
    """Derive the AgentCore CloudWatch log group name from the agent name.

    Matches Requirement 7.1's ``/aws/bedrock-agentcore/runtimes/<agent>`` pattern
    and the ObservabilityStack log group (task 6.1), so telemetry lands in the
    log group the rest of the app creates.
    """
    return f"/aws/bedrock-agentcore/runtimes/{agent_name}"


def agentcore_env(cfg: AppConfig) -> list[dict[str, Any]]:
    """Build the AgentCore Observability env-var block.

    AgentCore Observability uses the **collector-less OTLP path** (design DD-6,
    Requirement 7.6): the aws-opentelemetry-distro exports traces directly to the
    AWS X-Ray OTLP endpoint and logs to the AWS CloudWatch Logs OTLP endpoint,
    with SigV4 auth applied automatically from the pod's IRSA credentials — there
    is no self-hosted ADOT collector and no API key.

    Everything region- or agent-specific is derived here from ``cfg.region`` and
    ``cfg.agent_name`` (Requirements 7.4, 11.1, 11.5): the two OTLP endpoints are
    built from the region, and the resource attributes, log-group headers, and
    service name are built from the agent name.

    Returns:
        The list of AgentCore env-var dicts, or ``[]`` when AgentCore is disabled
        (Requirement 11.7).
    """
    # Disabled ⇒ contribute nothing (Requirement 11.7).
    if not cfg.agentcore_enabled:
        return []

    agent = cfg.agent_name
    region = cfg.region
    log_group = _agentcore_log_group(agent)

    # OTLP endpoints are per-region AWS service endpoints (Requirement 7.4).
    traces_endpoint = f"https://xray.{region}.amazonaws.com/v1/traces"
    logs_endpoint = f"https://logs.{region}.amazonaws.com/v1/logs"

    # OTEL resource attributes. PlatformType=AWS::BedrockAgentCore and
    # aws.service.type=gen_ai_agent are REQUIRED for sessions to appear in the
    # GenAI Observability console even though this agent runs on EKS rather than
    # the AgentCore managed runtime (Requirement 7.4). service.name,
    # aws.log.group.names, and cloud.resource_id are all keyed to the agent name.
    resource_attributes = (
        f"service.name={agent},"
        f"aws.log.group.names={log_group},"
        f"cloud.resource_id={agent},"
        "PlatformType=AWS::BedrockAgentCore,"
        "aws.service.type=gen_ai_agent"
    )

    # OTLP logs headers route log records to the AgentCore log group/stream and
    # tag the metric namespace the console expects (Requirement 7.4).
    logs_headers = (
        f"x-aws-log-group={log_group},"
        f"x-aws-log-stream={AGENTCORE_LOG_STREAM},"
        f"x-aws-metric-namespace={AGENTCORE_METRIC_NAMESPACE}"
    )

    return [
        # Master switch read by the agent + the AWS distro.
        _literal("AGENT_OBSERVABILITY_ENABLED", "true"),
        # Select the AWS OpenTelemetry distro + configurator (SigV4, AWS OTLP).
        _literal("OTEL_PYTHON_DISTRO", "aws_distro"),
        _literal("OTEL_PYTHON_CONFIGURATOR", "aws_configurator"),
        # Identity/routing metadata for the GenAI Observability console.
        _literal("OTEL_RESOURCE_ATTRIBUTES", resource_attributes),
        _literal("OTEL_EXPORTER_OTLP_LOGS_HEADERS", logs_headers),
        # Direct-to-AWS OTLP endpoints (region-derived).
        _literal("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", traces_endpoint),
        _literal("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", logs_endpoint),
        # Service name (agent-derived).
        _literal("OTEL_SERVICE_NAME", agent),
        # Protobuf over HTTP for every signal.
        _literal("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
        _literal("OTEL_TRACES_EXPORTER", "otlp"),
        _literal("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf"),
        _literal("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "http/protobuf"),
        _literal("OTEL_LOGS_EXPORTER", "otlp"),
        # No managed OTLP metrics endpoint exists; Application Signals derives
        # session/trace metrics from the X-Ray spans instead.
        _literal("OTEL_METRICS_EXPORTER", "none"),
        # Legacy convenience vars referenced by older AWS examples / the agent.
        _literal("CLOUDWATCH_LOG_GROUP", log_group),
        _literal("CLOUDWATCH_LOG_STREAM", AGENTCORE_LOG_STREAM),
        _literal("CLOUDWATCH_NAMESPACE", AGENTCORE_METRIC_NAMESPACE),
    ]


def langsmith_env(cfg: AppConfig) -> list[dict[str, Any]]:
    """Build the LangSmith env-var block.

    LangSmith tracing is enabled purely through the LangChain SDK's environment
    contract: ``LANGCHAIN_TRACING_V2`` turns tracing on, ``LANGSMITH_PROJECT``
    selects the destination project, ``LANGCHAIN_ENDPOINT`` is the LangSmith API
    base URL, and ``LANGCHAIN_API_KEY`` authenticates. The API key is the only
    secret and is injected by reference to the synced ``langsmith-secret``
    Kubernetes Secret — never as a literal value (Requirements 8.5, 6.2, 6.3).

    The project name is operator-configurable via ``cfg.langsmith_project``; the
    endpoint is the fixed LangSmith SaaS URL.

    Returns:
        The list of LangSmith env-var dicts, or ``[]`` when LangSmith is disabled
        (Requirement 11.7).
    """
    # Disabled ⇒ contribute nothing (Requirement 11.7).
    if not cfg.langsmith_enabled:
        return []

    return [
        # Turn on LangChain V2 tracing (the LangChain SDK reads this).
        _literal("LANGCHAIN_TRACING_V2", "true"),
        # Destination project (operator-configurable input).
        _literal("LANGSMITH_PROJECT", cfg.langsmith_project),
        # LangSmith SaaS API base URL.
        _literal("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com"),
        # API key sourced by reference from the synced K8s Secret — no plaintext.
        _secret_ref(
            "LANGCHAIN_API_KEY",
            LANGSMITH_K8S_SECRET_NAME,
            LANGSMITH_API_KEY_SECRET_KEY,
        ),
    ]


def langfuse_env(cfg: AppConfig) -> list[dict[str, Any]]:
    """Build the Langfuse env-var block.

    Langfuse needs a host URL plus a public/secret key pair. ``LANGFUSE_HOST`` is
    operator-configurable via ``cfg.langfuse_host`` (US cloud, EU cloud, or a
    self-hosted URL). Both keys are secrets, injected by reference to the synced
    ``langfuse-secret`` Kubernetes Secret rather than as literal values
    (Requirements 8.5, 6.2, 6.3).

    Returns:
        The list of Langfuse env-var dicts, or ``[]`` when Langfuse is disabled
        (Requirement 11.7).
    """
    # Disabled ⇒ contribute nothing (Requirement 11.7).
    if not cfg.langfuse_enabled:
        return []

    return [
        # Langfuse host (operator-configurable input: cloud region or self-host).
        _literal("LANGFUSE_HOST", cfg.langfuse_host),
        # Public + secret keys sourced by reference from the synced K8s Secret.
        _secret_ref(
            "LANGFUSE_PUBLIC_KEY",
            LANGFUSE_K8S_SECRET_NAME,
            LANGFUSE_PUBLIC_KEY_SECRET_KEY,
        ),
        _secret_ref(
            "LANGFUSE_SECRET_KEY",
            LANGFUSE_K8S_SECRET_NAME,
            LANGFUSE_SECRET_KEY_SECRET_KEY,
        ),
    ]
