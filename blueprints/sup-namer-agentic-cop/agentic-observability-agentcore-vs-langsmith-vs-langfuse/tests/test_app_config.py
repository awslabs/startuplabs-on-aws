"""Table-driven unit tests for ``AppConfig`` validation (task 2.3).

These tests exercise the fail-fast validation in ``stacks/app_config.py`` so
that invalid or missing Deployment_Inputs raise *before* synth — no AWS
credentials and no network access are required (Requirements 2.2, 5.6, 5.7,
11.3, 11.6; design Correctness Property 5).

The approach:

* ``make_config`` builds a fully-valid :class:`AppConfig` dataclass instance;
  each negative case overrides exactly one field so a single validation rule is
  exercised in isolation.
* A parametrized "invalid" table asserts that each bad input raises
  ``ValueError`` and that the message names the offending input.
* A parametrized "valid" table asserts fully-valid configs pass ``validate()``.
* A small set of ``from_context`` tests confirm validation is invoked before an
  ``AppConfig`` is returned (the fail-fast contract), using a stub context so no
  real CDK ``App`` / AWS environment is needed.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from stacks.app_config import (
    AGENT_NAME_MAX_LENGTH,
    NODES_MAX_LIMIT,
    AppConfig,
)


def make_config(**overrides: Any) -> AppConfig:
    """Return a fully-valid :class:`AppConfig`, applying any field overrides.

    The baseline mirrors the documented defaults and passes ``validate()``
    unchanged; tests override a single field to isolate one validation rule.
    """
    base: dict[str, Any] = dict(
        account="123456789012",
        region="us-east-1",
        agent_name="langgraph-shopping-agent",
        model_id="us.anthropic.claude-sonnet-5",
        judge_model_id="us.anthropic.claude-3-5-haiku-20241022-v1:0",
        node_type="t3.medium",
        nodes_desired=2,
        nodes_min=1,
        nodes_max=3,
        existing_cluster_name="none",
        cluster_admin_role_arn="",
        langsmith_enabled=True,
        langsmith_project="langgraph-shopping-agent",
        langsmith_secret_name="agent-observability/langgraph-shopping-agent/langsmith",  # nosec B106 - Secrets Manager name, not a secret value  # pragma: allowlist secret
        langfuse_enabled=True,
        langfuse_host="https://us.cloud.langfuse.com",
        langfuse_secret_name="agent-observability/langgraph-shopping-agent/langfuse",  # pragma: allowlist secret
        agentcore_enabled=True,
    )
    base.update(overrides)
    return AppConfig(**base)


# ---------------------------------------------------------------------------
# Negative cases: each must raise ValueError before synth.
#
# Columns: (test id, field overrides, expected substring in the error message).
# ---------------------------------------------------------------------------
INVALID_CASES: list[tuple[str, dict[str, Any], str]] = [
    # --- Account ID (Requirement 5.7): must be exactly 12 digits. ---
    ("account_too_short", {"account": "12345"}, "Invalid AWS account ID"),
    ("account_non_digit", {"account": "12345678901X"}, "Invalid AWS account ID"),
    ("account_too_long", {"account": "1234567890123"}, "Invalid AWS account ID"),
    ("account_missing", {"account": None}, "Missing required Deployment_Input"),
    ("account_blank", {"account": "   "}, "Missing required Deployment_Input"),
    # --- Region (Requirements 5.7, 11.3): AWS region identifier pattern. ---
    ("region_bad_pattern", {"region": "not-a-region"}, "Invalid AWS region"),
    ("region_uppercase", {"region": "US-EAST-1"}, "Invalid AWS region"),
    ("region_missing", {"region": None}, "Missing required Deployment_Input"),
    # --- Agent name (Requirement 11.6): charset / length / boundaries. ---
    ("agent_leading_hyphen", {"agent_name": "-agent"}, "Invalid agent/service name"),
    ("agent_trailing_hyphen", {"agent_name": "agent-"}, "Invalid agent/service name"),
    ("agent_uppercase", {"agent_name": "MyAgent"}, "Invalid agent/service name"),
    ("agent_underscore", {"agent_name": "my_agent"}, "Invalid agent/service name"),
    ("agent_empty", {"agent_name": ""}, "Invalid agent/service name"),
    (
        "agent_too_long",
        {"agent_name": "a" * (AGENT_NAME_MAX_LENGTH + 1)},
        "at most",
    ),
    # --- Node sizing (Requirement 2.2). ---
    (
        "nodes_min_gt_desired",
        {"nodes_min": 5, "nodes_desired": 2, "nodes_max": 10},
        "nodesMin <= nodesDesired <= nodesMax",
    ),
    (
        "nodes_desired_gt_max",
        {"nodes_min": 1, "nodes_desired": 5, "nodes_max": 3},
        "nodesMin <= nodesDesired <= nodesMax",
    ),
    (
        "nodes_negative_min",
        {"nodes_min": -1, "nodes_desired": 2, "nodes_max": 3},
        "non-negative",
    ),
    (
        "nodes_negative_desired",
        {"nodes_min": 0, "nodes_desired": -2, "nodes_max": 3},
        "non-negative",
    ),
    (
        "nodes_max_over_limit",
        {"nodes_min": 1, "nodes_desired": 2, "nodes_max": NODES_MAX_LIMIT + 1},
        "must not exceed",
    ),
    (
        "nodes_bool_rejected",
        {"nodes_min": True, "nodes_desired": 2, "nodes_max": 3},
        "expected an integer",
    ),
    # --- Missing enabled-platform values (Requirements 5.6, 11.6/11.7). ---
    (
        "langsmith_enabled_missing_project",
        {"langsmith_enabled": True, "langsmith_project": ""},
        "langsmithProject",
    ),
    (
        "langsmith_enabled_missing_secret",
        {"langsmith_enabled": True, "langsmith_secret_name": "  "},
        "langsmithSecretName",
    ),
    (
        "langfuse_enabled_missing_host",
        {"langfuse_enabled": True, "langfuse_host": ""},
        "langfuseHost",
    ),
    (
        "langfuse_enabled_missing_secret",
        {"langfuse_enabled": True, "langfuse_secret_name": None},
        "langfuseSecretName",
    ),
    # --- clusterAdminRoleArn: optional, but when set must be an IAM ARN. ---
    (
        "cluster_admin_sts_session_arn_rejected",
        {
            "cluster_admin_role_arn": (
                "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Admin_abc/reinaman"
            )
        },
        "STS assumed-role session ARN",
    ),
    (
        "cluster_admin_bogus_arn_rejected",
        {"cluster_admin_role_arn": "not-an-arn"},
        "expected an IAM role or user ARN",
    ),
    (
        "cluster_admin_wrong_service_rejected",
        {"cluster_admin_role_arn": "arn:aws:s3:::my-bucket"},
        "expected an IAM role or user ARN",
    ),
]


@pytest.mark.parametrize(
    "overrides, expected_substring",
    [pytest.param(ov, msg, id=tid) for tid, ov, msg in INVALID_CASES],
)
def test_validate_rejects_invalid_input(
    overrides: dict[str, Any], expected_substring: str
) -> None:
    """Each malformed / missing input raises ValueError naming the problem."""
    config = make_config(**overrides)
    with pytest.raises(ValueError) as exc_info:
        config.validate()
    assert expected_substring in str(exc_info.value)


# ---------------------------------------------------------------------------
# Positive cases: fully-valid configs must pass validate() without raising.
# ---------------------------------------------------------------------------
VALID_CASES: list[tuple[str, dict[str, Any]]] = [
    ("all_defaults_valid", {}),
    (
        "single_node_min_equals_desired_equals_max",
        {"nodes_min": 1, "nodes_desired": 1, "nodes_max": 1},
    ),
    (
        "zero_min_is_allowed",
        {"nodes_min": 0, "nodes_desired": 0, "nodes_max": 2},
    ),
    (
        "max_at_service_limit",
        {"nodes_min": 1, "nodes_desired": 2, "nodes_max": NODES_MAX_LIMIT},
    ),
    (
        "short_agent_name",
        {
            "agent_name": "a",
            "langsmith_secret_name": "agent-observability/a/langsmith",  # pragma: allowlist secret
            "langfuse_secret_name": "agent-observability/a/langfuse",  # pragma: allowlist secret
        },
    ),
    (
        "agent_name_with_digits_and_hyphens",
        {"agent_name": "agent-01-v2"},
    ),
    (
        "other_valid_region",
        {"region": "ap-southeast-2"},
    ),
    # clusterAdminRoleArn is optional (empty) and accepts IAM role/user ARNs.
    ("cluster_admin_empty_allowed", {"cluster_admin_role_arn": ""}),
    (
        "cluster_admin_iam_role_arn_allowed",
        {"cluster_admin_role_arn": "arn:aws:iam::123456789012:role/EksOperator"},
    ),
    (
        "cluster_admin_iam_user_arn_allowed",
        {"cluster_admin_role_arn": "arn:aws:iam::123456789012:user/alice"},
    ),
    (
        "cluster_admin_sso_role_arn_allowed",
        {
            "cluster_admin_role_arn": (
                "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/"
                "AWSReservedSSO_Admin_0123456789abcdef"  # pragma: allowlist secret
            )
        },
    ),
    # Disabled platforms require nothing — empty per-platform values are fine
    # when the platform is off (Requirement 11.7).
    (
        "langsmith_disabled_allows_empty_values",
        {"langsmith_enabled": False, "langsmith_project": "", "langsmith_secret_name": ""},
    ),
    (
        "langfuse_disabled_allows_empty_values",
        {"langfuse_enabled": False, "langfuse_host": "", "langfuse_secret_name": ""},
    ),
    (
        "all_optional_platforms_disabled",
        {
            "langsmith_enabled": False,
            "langsmith_project": "",
            "langsmith_secret_name": "",
            "langfuse_enabled": False,
            "langfuse_host": "",
            "langfuse_secret_name": "",
            "agentcore_enabled": False,
        },
    ),
]


@pytest.mark.parametrize(
    "overrides",
    [pytest.param(ov, id=tid) for tid, ov in VALID_CASES],
)
def test_validate_accepts_valid_input(overrides: dict[str, Any]) -> None:
    """A fully-valid config passes validation with no exception."""
    config = make_config(**overrides)
    # Should not raise.
    config.validate()


# ---------------------------------------------------------------------------
# from_context: validation is invoked before the config is returned, so the
# app fails fast before synth. A stub context keeps this hermetic — no real
# CDK App and no AWS environment are required.
# ---------------------------------------------------------------------------


class _StubNode:
    """Minimal stand-in for ``app.node`` exposing ``try_get_context``."""

    def __init__(self, context: dict[str, Any]) -> None:
        self._context = context

    def try_get_context(self, key: str) -> Optional[Any]:
        return self._context.get(key)


class _StubApp:
    """Minimal stand-in for a CDK ``App`` exposing ``.node``."""

    def __init__(self, context: dict[str, Any]) -> None:
        self.node = _StubNode(context)


@pytest.fixture(autouse=True)
def _clear_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove AWS/CDK env vars so resolution is deterministic in from_context."""
    for var in ("CDK_DEFAULT_ACCOUNT", "CDK_DEFAULT_REGION", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)


def test_from_context_returns_valid_config(tmp_path) -> None:
    """A valid stub context yields a validated AppConfig (no raise)."""
    app = _StubApp({"account": "123456789012", "region": "us-east-1"})
    # Point config_path at a non-existent file so no config.json is picked up.
    config = AppConfig.from_context(app, config_path=tmp_path / "absent.json")
    assert config.account == "123456789012"
    assert config.region == "us-east-1"
    # Defaults were applied and are valid.
    assert config.agent_name  # non-empty
    assert config.nodes_min <= config.nodes_desired <= config.nodes_max


def test_from_context_fails_fast_on_bad_account(tmp_path) -> None:
    """from_context raises before returning when an input is invalid."""
    app = _StubApp({"account": "bad", "region": "us-east-1"})
    with pytest.raises(ValueError) as exc_info:
        AppConfig.from_context(app, config_path=tmp_path / "absent.json")
    assert "Invalid AWS account ID" in str(exc_info.value)


def test_from_context_fails_fast_on_missing_account(tmp_path) -> None:
    """With no account from any source, from_context reports it missing."""
    app = _StubApp({"region": "us-east-1"})
    with pytest.raises(ValueError) as exc_info:
        AppConfig.from_context(app, config_path=tmp_path / "absent.json")
    assert "Missing required Deployment_Input" in str(exc_info.value)
    assert "account" in str(exc_info.value)
