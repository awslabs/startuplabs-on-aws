"""AgentCore Observability account/region setup (log group + Transaction Search).

``ObservabilityStack`` owns the account/region-level AgentCore Observability
plumbing that must exist before the Agent_Workload emits telemetry (design DD-6,
"AgentCore Observability — log group + Transaction Search custom resource"). It
is a separate stack so this account-level setup has its own failure blast radius
and teardown ordering, distinct from the network, cluster, and workload stacks
(design DD-2, "Stack decomposition").

Scope of this module today (task 6.1):

* **AgentCore log group** — the CloudWatch log group
  ``/aws/bedrock-agentcore/runtimes/<agent>`` that AgentCore Observability writes
  to, derived from the agent name Deployment_Input (Requirement 7.1). It is
  created **idempotently** so that a log group which already exists in the target
  region does not fail the deploy (Requirement 7.2). See
  :meth:`_create_log_group`.

* **CloudWatch Transaction Search (task 6.2)** — idempotent
  :class:`~aws_cdk.custom_resources.AwsCustomResource` calls that enable
  span-to-log ingestion so X-Ray spans are indexed for the GenAI Observability
  console (Requirements 7.3, 7.5, 7.6, 7.7). See
  :meth:`_enable_transaction_search`. Because
  ``cloudwatch put-transaction-search-configuration`` (used by
  ``setup_agentcore_observability.sh``) is not present in the SDK bundled with a
  custom-resource Lambda, this uses the equivalent, currently-supported X-Ray +
  CloudWatch Logs API sequence AWS documents for enabling Transaction Search via
  an API: ``logs:PutResourcePolicy`` (let X-Ray write spans to logs) →
  ``xray:UpdateTraceSegmentDestination CloudWatchLogs`` (the
  ``--ingest-spans-as-logs`` equivalent) → ``xray:UpdateIndexingRule`` (index
  percentage). The rationale is documented in full at the Transaction Search
  constants block below.

Idempotency approach (Requirement 7.2)
--------------------------------------
CDK's ``logs.LogGroup`` L2 (and the ``logs.CfnLogGroup`` L1) fail with
``ResourceAlreadyExistsException`` when a log group of the same name already
exists, and existence cannot be known at synth time without a lookup. To
genuinely satisfy "use the existing log group and do not fail the deployment",
this stack creates the log group with an :class:`~aws_cdk.custom_resources.AwsCustomResource`
that calls the CloudWatch Logs ``CreateLogGroup`` API and treats
``ResourceAlreadyExistsException`` as success (via ``ignore_error_codes_matching``).
This is the idempotent "reference-if-exists" pattern recommended by the design:
a pre-existing log group is silently reused, and a fresh deploy creates it.

A second, dependent ``AwsCustomResource`` sets a one-month retention on the log
group (``PutRetentionPolicy``), giving the created log group sensible retention
hygiene without the L2's create-time existence failure.

Deletion behavior (documented per Requirements 9.1, 9.6)
--------------------------------------------------------
On ``cdk destroy`` the log-group custom resource calls ``DeleteLogGroup`` and
ignores ``ResourceNotFoundException`` (so an already-absent log group is treated
as removed — Requirement 9.3). Requirement 9.1 explicitly lists the
AgentCore_Log_Group among the resources destroy removes, and the log-group name
is specific to *this* sample's agent (``/aws/bedrock-agentcore/runtimes/<agent>``),
so removing it on teardown is the intended clean-teardown behavior. Note the
consequence of the reference-if-exists semantics: if the log group happened to
pre-exist before the first deploy, teardown still removes it, because the delete
path cannot distinguish "created by this app" from "pre-existing". This is a
deliberate, documented trade-off in favor of the Requirement 9.1 clean-teardown
guarantee.

The created log group's name and ARN are exposed as the public attributes
:attr:`ObservabilityStack.log_group_name` and
:attr:`ObservabilityStack.log_group_arn` so ``WorkloadStack`` (task 8.2) can
scope the Agent_IAM_Role's ``logs:CreateLogStream`` / ``logs:PutLogEvents``
permissions to exactly this log group's ARN (Requirement 4.7).

This module is intentionally *not* instantiated in ``app.py`` yet; the stacks are
composed together in task 10. It is written to be importable and synthesizable on
its own so it can be exercised with ``cdk synth`` in isolation.
"""

from __future__ import annotations

import json
from typing import Any

from aws_cdk import ArnFormat, CfnOutput, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import custom_resources as cr
from constructs import Construct

from .app_config import AppConfig

# ---------------------------------------------------------------------------
# Log group constants.
#
# Kept as module-level names so the naming convention and retention decision are
# self-documenting and easy to audit against the design (DD-6, Requirement 7.1).
# ---------------------------------------------------------------------------

# The AgentCore log-group name prefix. The full name is this prefix plus the
# agent name, producing ``/aws/bedrock-agentcore/runtimes/<agent>`` — the pattern
# AgentCore Observability expects and that ``observability_config.py`` derives the
# matching OTEL log-group headers from (Requirement 7.1).
LOG_GROUP_NAME_PREFIX = "/aws/bedrock-agentcore/runtimes/"

# Retention for the created log group, in days. 30 days == logs.RetentionDays
# .ONE_MONTH — a sensible default for a sample that keeps recent telemetry
# available without unbounded storage cost.
LOG_RETENTION_DAYS = 30

# CloudWatch Logs API error codes the idempotent custom resource tolerates:
# * CreateLogGroup on an existing log group returns ResourceAlreadyExistsException
#   — treated as success so a pre-existing log group does not fail the deploy
#   (Requirement 7.2).
# * DeleteLogGroup on an absent log group returns ResourceNotFoundException —
#   treated as success so destroy is idempotent (Requirement 9.3).
_ERR_ALREADY_EXISTS = "ResourceAlreadyExistsException"
_ERR_NOT_FOUND = "ResourceNotFoundException"

# The CloudWatch Logs service name used by the AwsCustomResource SDK calls.
_LOGS_SERVICE = "CloudWatchLogs"

# ---------------------------------------------------------------------------
# CloudWatch Transaction Search constants (task 6.2).
#
# WHY THE X-RAY APIs (and not `cloudwatch put-transaction-search-configuration`):
# ``setup_agentcore_observability.sh`` enables Transaction Search with the
# CloudWatch convenience commands ``describe-transaction-search-configuration``
# and ``put-transaction-search-configuration --enabled --ingest-spans-as-logs``.
# Those commands are NOT present in the AWS SDK/CLI bundled with (or installable
# into) an ``AwsCustomResource`` Lambda, so calling them from a custom resource
# would fail at deploy time. AWS documents the API-level way to achieve exactly
# the same end state — "enable Transaction Search with span-to-log ingestion" —
# as a short X-Ray sequence (see the CloudWatch "Enable Transaction Search using
# an API" guide and the Bedrock AgentCore observability guide):
#
#   1. ``logs:PutResourcePolicy`` — allow ``xray.amazonaws.com`` to
#      ``logs:PutLogEvents`` into the span log groups, so X-Ray may write spans
#      as logs at all.
#   2. ``xray:UpdateTraceSegmentDestination --destination CloudWatchLogs`` — the
#      API equivalent of ``--ingest-spans-as-logs``: it switches span ingestion
#      to CloudWatch Logs (Requirement 7.3).
#   3. ``xray:UpdateIndexingRule`` — sets the percentage of ingested spans
#      indexed as trace summaries for the GenAI Observability console.
#
# All three are idempotent "update/put" calls, so re-running them against an
# already-enabled account succeeds without error (Requirement 7.5) — which is
# also why this uses ``AwsCustomResource`` rather than the L1
# ``CfnTransactionSearchConfig`` (the L1 fails if Transaction Search is already
# enabled).
# ---------------------------------------------------------------------------

# The X-Ray service name used by the AwsCustomResource SDK calls.
_XRAY_SERVICE = "XRay"

# X-Ray raises this error code from ``updateTraceSegmentDestination`` when the
# account-level trace-segment destination is already set to the requested value
# (message: "The destination is already set to CloudWatchLogs"). Because the
# Transaction Search destination is a RETAINED account setting (not reversed on
# teardown — Requirements 7.6, 9.6), every redeploy re-hits this. We ignore this
# specific error so re-enablement is idempotent (Requirement 7.5) without masking
# genuine failures — the destination value is a hardcoded constant, so an
# "already set" InvalidRequestException is the only expected case for this call.
_ERR_TS_DESTINATION_ALREADY_SET = "InvalidRequestException"

# X-Ray trace-segment destination that turns on span-to-log ingestion. Setting
# the destination to CloudWatch Logs is the current AWS API equivalent of the
# script's ``--ingest-spans-as-logs`` flag (Requirement 7.3). Valid values are
# ``XRay`` and ``CloudWatchLogs``.
_TRACE_SEGMENT_DESTINATION = "CloudWatchLogs"

# The built-in X-Ray indexing rule whose sampling percentage governs how many
# ingested spans are indexed as trace summaries. ``Default`` is the rule the
# AWS documentation configures.
_INDEXING_RULE_NAME = "Default"

# Percentage of ingested spans indexed as trace summaries. 1% is the
# AWS-recommended free-tier default; full span visibility still comes from the
# 100% log ingestion enabled in step (2).
_INDEXING_SAMPLING_PERCENTAGE = 1

# The CloudWatch Logs log groups Transaction Search writes spans into. X-Ray
# must be granted ``logs:PutLogEvents`` on both before it can ingest spans as
# logs (the resource policy in step (1)).
_SPANS_LOG_GROUP = "aws/spans"
_APPLICATION_SIGNALS_LOG_GROUP = "/aws/application-signals/data"

# Sid for the X-Ray -> CloudWatch Logs resource-policy statement.
_TS_RESOURCE_POLICY_SID = "TransactionSearchXRayAccess"


def _agentcore_log_group_name(agent_name: str) -> str:
    """Derive the AgentCore log-group name from the agent name (Requirement 7.1).

    Produces ``/aws/bedrock-agentcore/runtimes/<agent>``, matching the pattern in
    ``observability_config._agentcore_log_group`` so the log group this stack
    creates is exactly the one the agent's OTEL config writes to.
    """
    return f"{LOG_GROUP_NAME_PREFIX}{agent_name}"


class ObservabilityStack(Stack):
    """AgentCore Observability setup: log group (task 6.1) + Transaction Search (task 6.2).

    Creates the ``/aws/bedrock-agentcore/runtimes/<agent>`` log group idempotently
    so an existing log group does not fail the deploy (Requirement 7.2), sets a
    one-month retention on it, and enables CloudWatch Transaction Search with
    span-to-log ingestion via idempotent custom resources (task 6.2,
    :meth:`_enable_transaction_search`).

    Attributes:
        log_group_name: the resolved AgentCore log-group name
            (``/aws/bedrock-agentcore/runtimes/<agent>``). Exposed so downstream
            stacks and config derive the same name.
        log_group_arn: the CloudWatch Logs ARN of the log group (without a
            trailing ``:*``). ``WorkloadStack`` (task 8.2) scopes the agent role's
            ``logs:CreateLogStream`` / ``logs:PutLogEvents`` grant to this ARN plus
            its ``:*`` log-stream children (Requirement 4.7).
    """

    log_group_name: str
    log_group_arn: str

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        **kwargs: Any,
    ) -> None:
        """Create the AgentCore log group.

        Args:
            scope: the parent construct (typically the CDK ``App``).
            construct_id: this stack's logical id within the app.
            config: the resolved :class:`AppConfig`. The agent name is read from
                it to derive the log-group name (Requirement 7.1); it was already
                validated fail-fast in ``AppConfig``, so this stack can trust it.
            **kwargs: forwarded to :class:`aws_cdk.Stack` — notably ``env`` so the
                log group is created in the Operator's resolved account/region and
                the derived ARN targets the right region.
        """
        super().__init__(scope, construct_id, **kwargs)

        self._config = config

        # Resolve the log-group name once and expose it (Requirements 7.1, 11.5).
        self.log_group_name = _agentcore_log_group_name(config.agent_name)

        # Build the log group's ARN from the stack's account/region tokens. Using
        # ``format_arn`` (rather than a hand-built string) keeps the ARN correct
        # across partitions/regions and resolves the account/region from ``env``.
        # COLON_RESOURCE_NAME yields ``arn:...:logs:<region>:<account>:log-group:
        # /aws/bedrock-agentcore/runtimes/<agent>`` (no trailing ``:*``); downstream
        # IAM scoping appends ``:*`` for the log streams (Requirement 4.7).
        self.log_group_arn = self.format_arn(
            service="logs",
            resource="log-group",
            resource_name=self.log_group_name,
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

        # Idempotently create the log group + set retention (task 6.1).
        self._create_log_group()

        # Task 6.2 extension point — CloudWatch Transaction Search. Wired here so
        # task 6.2 only needs to fill in the method body (currently a no-op).
        self._enable_transaction_search()

        # Surface the log-group name for the Operator's convenience.
        self._emit_outputs()

    # ------------------------------------------------------------------
    # Log group creation (task 6.1) — idempotent "reference-if-exists".
    # ------------------------------------------------------------------

    def _create_log_group(self) -> None:
        """Idempotently create the AgentCore log group and set its retention.

        Realizes Requirements 7.1 and 7.2. CDK's ``logs.LogGroup`` L2 fails when a
        log group of the same name already exists, which would violate
        Requirement 7.2. Instead, this uses an
        :class:`~aws_cdk.custom_resources.AwsCustomResource` that calls the
        CloudWatch Logs ``CreateLogGroup`` API directly and treats
        ``ResourceAlreadyExistsException`` as success — so an existing log group
        is silently reused and a fresh deploy creates one.

        Two resources are used:

        #. ``AgentCoreLogGroup`` — creates the log group on create/update
           (ignoring "already exists"), and deletes it on stack delete (ignoring
           "not found" so destroy is idempotent — Requirements 9.1, 9.3). Its
           physical id is the log-group name, so CloudFormation treats a rename as
           a replacement.
        #. ``AgentCoreLogGroupRetention`` — sets a one-month retention on the log
           group via ``PutRetentionPolicy`` (idempotent) once it exists. It
           depends on the create resource so it runs after the group is present.

        The custom resources' IAM is derived from the SDK calls and scoped to the
        log group's ARN (and its ``:*`` children), never a wildcard log-group
        resource.
        """
        # IAM scope for the custom-resource Lambda: exactly this log group and its
        # ``:*`` children. ``from_sdk_calls`` maps each SDK action (CreateLogGroup,
        # DeleteLogGroup, PutRetentionPolicy) to its IAM action against these ARNs.
        log_group_resources = [self.log_group_arn, f"{self.log_group_arn}:*"]

        # (1) Idempotent create + idempotent delete of the log group.
        self._log_group_resource = cr.AwsCustomResource(
            self,
            "AgentCoreLogGroup",
            # Create on first deploy; re-assert on update. ``CreateLogGroup`` on an
            # existing group raises ResourceAlreadyExistsException, which we ignore
            # so a pre-existing log group does not fail the deploy (Requirement 7.2).
            on_create=cr.AwsSdkCall(
                service=_LOGS_SERVICE,
                action="createLogGroup",
                parameters={"logGroupName": self.log_group_name},
                ignore_error_codes_matching=_ERR_ALREADY_EXISTS,
                physical_resource_id=cr.PhysicalResourceId.of(self.log_group_name),
            ),
            on_update=cr.AwsSdkCall(
                service=_LOGS_SERVICE,
                action="createLogGroup",
                parameters={"logGroupName": self.log_group_name},
                ignore_error_codes_matching=_ERR_ALREADY_EXISTS,
                physical_resource_id=cr.PhysicalResourceId.of(self.log_group_name),
            ),
            # On teardown, remove the log group (Requirement 9.1). Ignoring
            # ResourceNotFoundException keeps destroy idempotent (Requirement 9.3).
            on_delete=cr.AwsSdkCall(
                service=_LOGS_SERVICE,
                action="deleteLogGroup",
                parameters={"logGroupName": self.log_group_name},
                ignore_error_codes_matching=_ERR_NOT_FOUND,
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=log_group_resources,
            ),
            # Use the Lambda runtime's bundled AWS SDK; do not fetch the latest at
            # deploy time (faster, reproducible, no network dependency).
            install_latest_aws_sdk=False,
        )

        # (2) Set retention on the (now-existing) log group. PutRetentionPolicy is
        # idempotent and does not fail when re-applied. Retention is 30 days
        # (logs.RetentionDays.ONE_MONTH). No on_delete: the log group is removed by
        # resource (1), so there is no separate retention policy to clean up.
        self._retention_resource = cr.AwsCustomResource(
            self,
            "AgentCoreLogGroupRetention",
            on_create=cr.AwsSdkCall(
                service=_LOGS_SERVICE,
                action="putRetentionPolicy",
                parameters={
                    "logGroupName": self.log_group_name,
                    "retentionInDays": LOG_RETENTION_DAYS,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{self.log_group_name}-retention"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service=_LOGS_SERVICE,
                action="putRetentionPolicy",
                parameters={
                    "logGroupName": self.log_group_name,
                    "retentionInDays": LOG_RETENTION_DAYS,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{self.log_group_name}-retention"
                ),
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=log_group_resources,
            ),
            install_latest_aws_sdk=False,
        )

        # The retention policy can only be set once the log group exists, so order
        # the retention resource after the create resource.
        self._retention_resource.node.add_dependency(self._log_group_resource)

    # ------------------------------------------------------------------
    # Task 6.2 extension point — CloudWatch Transaction Search.
    # ------------------------------------------------------------------

    def _enable_transaction_search(self) -> None:
        """Enable CloudWatch Transaction Search with span-to-log ingestion (task 6.2).

        Realizes Requirements 7.3, 7.5, 7.6, and 7.7 (design DD-6). Because there
        is no L2 construct for Transaction Search — and the L1
        ``CfnTransactionSearchConfig`` *fails* when Transaction Search is already
        enabled — this uses idempotent :class:`~aws_cdk.custom_resources.AwsCustomResource`
        calls, matching the describe/put-then-idempotent intent of
        ``setup_agentcore_observability.sh`` while using the API surface that is
        actually callable from a custom-resource Lambda (see the module-level
        note): the granular X-Ray + CloudWatch Logs calls, not the
        ``cloudwatch put-transaction-search-configuration`` convenience command
        (which is absent from the bundled SDK).

        Three ordered, idempotent custom resources are created on create/update:

        #. ``TransactionSearchLogsResourcePolicy`` — ``logs:PutResourcePolicy``
           granting ``xray.amazonaws.com`` ``logs:PutLogEvents`` on the span log
           groups (``aws/spans``, ``/aws/application-signals/data``), scoped with
           ``aws:SourceAccount`` / ``aws:SourceArn`` conditions. Without this
           X-Ray cannot write spans as logs, so Transaction Search would be
           enabled but non-functional.
        #. ``TransactionSearchTraceDestination`` —
           ``xray:UpdateTraceSegmentDestination`` with ``Destination=CloudWatchLogs``.
           This is the API equivalent of the script's ``--ingest-spans-as-logs``:
           it turns on span-to-log ingestion (Requirement 7.3). X-Ray raises
           ``InvalidRequestException`` when the destination is already
           ``CloudWatchLogs`` (the retained-account-setting case on redeploy), so
           this call ignores that specific error to stay idempotent without
           failing the deploy (Requirement 7.5).
        #. ``TransactionSearchIndexingRule`` — ``xray:UpdateIndexingRule`` on the
           built-in ``Default`` rule, setting a 1% trace-summary sampling
           percentage so spans are indexed for the GenAI Observability console.

        The calls are ordered ``policy -> destination -> indexing rule`` via CDK
        dependencies so X-Ray has write permission before ingestion is switched
        on.

        Delete behavior (Requirements 7.6, 9.6): none of the three resources set
        an ``on_delete`` call, so ``cdk destroy`` removes the custom resources
        without reversing the account/region-level Transaction Search setting or
        the span resource policy. This is deliberate — disabling Transaction
        Search would disrupt any other workload in the account that relies on it.
        The retained account-level setting is documented per Requirement 9.6.

        Failure surfacing (Requirement 7.7): the resource-policy and indexing-rule
        calls set no ``ignore_error_codes_matching``, so a genuine API failure
        fails the custom resource and therefore the stack — the deploy does not
        silently proceed with a partial Observability_Config. The one exception is
        the trace-destination call, which ignores the single ``InvalidRequestException``
        "destination already set" case (see below) because the destination is a
        hardcoded constant and that setting is retained across teardown; every
        other error there still fails the stack.
        """
        # Each call targets an account/region-level Transaction Search setting.
        # ``xray:UpdateTraceSegmentDestination`` / ``xray:UpdateIndexingRule`` and
        # ``logs:PutResourcePolicy`` do not support resource-level scoping, so the
        # custom-resource execution role must use ``*`` for these actions. This
        # applies only to the custom resource's own short-lived role, not to the
        # agent workload's role (which stays least-privilege in WorkloadStack).
        #
        # NOTE (deploy fix): ``from_sdk_calls`` only derives the *direct* SDK
        # action for each call (e.g. ``xray:UpdateTraceSegmentDestination``). But
        # enabling Transaction Search is not just that one API call — server-side,
        # ``UpdateTraceSegmentDestination`` also creates and configures the
        # AWS-managed span log groups (``aws/spans`` and
        # ``/aws/application-signals/data``), so the *caller* additionally needs
        # ``logs:CreateLogGroup`` / ``logs:PutRetentionPolicy`` /
        # ``logs:PutResourcePolicy`` on those groups. Without them the call fails
        # with ``AccessDeniedException: not authorized to perform
        # logs:PutRetentionPolicy on aws/spans``. We therefore grant the full
        # documented Transaction Search enablement action set explicitly via
        # ``from_statements`` instead of relying on the auto-derived policy.
        def account_level_policy() -> cr.AwsCustomResourcePolicy:
            # This is the exact permission set AWS documents as required to enable
            # Transaction Search via the API (CloudWatch "Enable Transaction
            # Search" -> Prerequisites). Enabling it is not a single API call:
            # server-side it configures the AWS-managed span log groups
            # (aws/spans, /aws/application-signals/data), creates the Application
            # Signals service-linked role, and creates a CloudTrail
            # service-linked channel. The caller therefore needs X-Ray + Logs +
            # Application Signals + IAM SLR + CloudTrail permissions. These apply
            # only to this short-lived custom-resource role, never the agent
            # workload role (which stays least-privilege in WorkloadStack).
            spans_lg = (
                f"arn:{self.partition}:logs:{self.region}:{self.account}"
                f":log-group:{_SPANS_LOG_GROUP}:*"
            )
            app_signals_lg = (
                f"arn:{self.partition}:logs:{self.region}:{self.account}"
                f":log-group:{_APPLICATION_SIGNALS_LOG_GROUP}:*"
            )
            app_signals_slr = (
                f"arn:{self.partition}:iam::*:role/aws-service-role/"
                "application-signals.cloudwatch.amazonaws.com/"
                "AWSServiceRoleForCloudWatchApplicationSignals"
            )
            cloudtrail_channel = (
                f"arn:{self.partition}:cloudtrail:*:*:channel/"
                "aws-service-channel/application-signals/*"
            )
            return cr.AwsCustomResourcePolicy.from_statements(
                [
                    # X-Ray Transaction Search / indexing controls (no resource
                    # scoping supported).
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=[
                            "xray:GetTraceSegmentDestination",
                            "xray:UpdateTraceSegmentDestination",
                            "xray:GetIndexingRules",
                            "xray:UpdateIndexingRule",
                        ],
                        resources=["*"],
                    ),
                    # Setup of the AWS-managed span log groups.
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=[
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:PutRetentionPolicy",
                        ],
                        resources=[spans_lg, app_signals_lg],
                    ),
                    # The X-Ray -> Logs resource policy (account-level).
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=[
                            "logs:PutResourcePolicy",
                            "logs:DescribeResourcePolicies",
                            "logs:DescribeLogGroups",
                        ],
                        resources=["*"],
                    ),
                    # Application Signals discovery kicked off by enablement.
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["application-signals:StartDiscovery"],
                        resources=["*"],
                    ),
                    # The Application Signals service-linked role the discovery
                    # creates (scoped to that SLR with the service-name condition).
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["iam:CreateServiceLinkedRole"],
                        resources=[app_signals_slr],
                        conditions={
                            "StringLike": {
                                "iam:AWSServiceName": (
                                    "application-signals.cloudwatch.amazonaws.com"
                                )
                            }
                        },
                    ),
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["iam:GetRole"],
                        resources=[app_signals_slr],
                    ),
                    # The CloudTrail service-linked channel enablement creates.
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["cloudtrail:CreateServiceLinkedChannel"],
                        resources=[cloudtrail_channel],
                    ),
                ]
            )

        policy_name = self._transaction_search_policy_name()
        policy_document = self._transaction_search_policy_document()

        # (1) Resource policy: let X-Ray write spans into the span log groups.
        # PutResourcePolicy overwrites the same-named policy on re-deploy, so it
        # is idempotent. No on_delete: the policy is a retained, account-level
        # grant (Requirements 7.6, 9.6).
        self._ts_resource_policy = cr.AwsCustomResource(
            self,
            "TransactionSearchLogsResourcePolicy",
            on_create=cr.AwsSdkCall(
                service=_LOGS_SERVICE,
                action="putResourcePolicy",
                parameters={
                    "policyName": policy_name,
                    "policyDocument": policy_document,
                },
                physical_resource_id=cr.PhysicalResourceId.of(policy_name),
            ),
            on_update=cr.AwsSdkCall(
                service=_LOGS_SERVICE,
                action="putResourcePolicy",
                parameters={
                    "policyName": policy_name,
                    "policyDocument": policy_document,
                },
                physical_resource_id=cr.PhysicalResourceId.of(policy_name),
            ),
            policy=account_level_policy(),
            install_latest_aws_sdk=False,
        )

        # (2) Enable span-to-log ingestion: destination -> CloudWatch Logs. This
        # is the ``--ingest-spans-as-logs`` equivalent (Requirement 7.3).
        #
        # IDEMPOTENCY (Requirement 7.5): X-Ray is NOT silently idempotent here —
        # ``updateTraceSegmentDestination`` raises
        # ``InvalidRequestException: The destination is already set to
        # CloudWatchLogs`` when the account-level destination already equals the
        # target. Because Transaction Search is a RETAINED account setting (it is
        # deliberately not reversed on ``cdk destroy`` — Requirements 7.6, 9.6), a
        # redeploy always re-hits that "already set" case. We treat that specific
        # error as success via ``ignore_error_codes_matching`` so re-enablement
        # does not fail the stack. The destination value is a hardcoded constant
        # (``CloudWatchLogs``), so the only realistic ``InvalidRequestException``
        # for this call is the already-set case — genuine failures on the other
        # two Transaction Search resources (resource policy, indexing rule) are
        # still surfaced (Requirement 7.7).
        self._ts_trace_destination = cr.AwsCustomResource(
            self,
            "TransactionSearchTraceDestination",
            on_create=cr.AwsSdkCall(
                service=_XRAY_SERVICE,
                action="updateTraceSegmentDestination",
                parameters={"Destination": _TRACE_SEGMENT_DESTINATION},
                ignore_error_codes_matching=_ERR_TS_DESTINATION_ALREADY_SET,
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{self.stack_name}-transaction-search-destination"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service=_XRAY_SERVICE,
                action="updateTraceSegmentDestination",
                parameters={"Destination": _TRACE_SEGMENT_DESTINATION},
                ignore_error_codes_matching=_ERR_TS_DESTINATION_ALREADY_SET,
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{self.stack_name}-transaction-search-destination"
                ),
            ),
            policy=account_level_policy(),
            install_latest_aws_sdk=False,
        )
        # X-Ray needs the resource policy before ingestion is switched on.
        self._ts_trace_destination.node.add_dependency(self._ts_resource_policy)

        # (3) Configure the indexing percentage on the built-in Default rule.
        # Idempotent: re-applying the same percentage succeeds (Requirement 7.5).
        self._ts_indexing_rule = cr.AwsCustomResource(
            self,
            "TransactionSearchIndexingRule",
            on_create=cr.AwsSdkCall(
                service=_XRAY_SERVICE,
                action="updateIndexingRule",
                parameters={
                    "Name": _INDEXING_RULE_NAME,
                    "Rule": {
                        "Probabilistic": {
                            "DesiredSamplingPercentage": _INDEXING_SAMPLING_PERCENTAGE,
                        },
                    },
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{self.stack_name}-transaction-search-indexing"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service=_XRAY_SERVICE,
                action="updateIndexingRule",
                parameters={
                    "Name": _INDEXING_RULE_NAME,
                    "Rule": {
                        "Probabilistic": {
                            "DesiredSamplingPercentage": _INDEXING_SAMPLING_PERCENTAGE,
                        },
                    },
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{self.stack_name}-transaction-search-indexing"
                ),
            ),
            policy=account_level_policy(),
            install_latest_aws_sdk=False,
        )
        # Indexing only matters once ingestion is on; order it last.
        self._ts_indexing_rule.node.add_dependency(self._ts_trace_destination)

    def _transaction_search_policy_name(self) -> str:
        """Return the deterministic name for the X-Ray -> Logs resource policy.

        Derived from the agent name so it is attributable to this sample and does
        not clobber an unrelated account resource policy. CloudWatch Logs
        evaluates all resource policies additively, so a per-agent policy name
        coexists with any pre-existing Transaction Search policy in the account.
        """
        return f"{self._config.agent_name}-transaction-search"

    def _transaction_search_policy_document(self) -> str:
        """Build the X-Ray -> CloudWatch Logs resource policy document (JSON string).

        Grants the ``xray.amazonaws.com`` service principal ``logs:PutLogEvents``
        on the two span log groups Transaction Search writes to, constrained by
        ``aws:SourceAccount`` and ``aws:SourceArn`` conditions so only this
        account's X-Ray service can use the grant. This mirrors the resource
        policy AWS documents as step 1 of enabling Transaction Search via the API.

        Returns a JSON *string* because ``logs:PutResourcePolicy`` expects
        ``policyDocument`` as a string. Account/region/partition are taken from
        the stack; any unresolved tokens are resolved by CDK when the custom
        resource's parameters are synthesized.
        """
        spans_arn = (
            f"arn:{self.partition}:logs:{self.region}:{self.account}"
            f":log-group:{_SPANS_LOG_GROUP}:*"
        )
        app_signals_arn = (
            f"arn:{self.partition}:logs:{self.region}:{self.account}"
            f":log-group:{_APPLICATION_SIGNALS_LOG_GROUP}:*"
        )
        source_arn = f"arn:{self.partition}:xray:{self.region}:{self.account}:*"

        document = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": _TS_RESOURCE_POLICY_SID,
                    "Effect": "Allow",
                    "Principal": {"Service": "xray.amazonaws.com"},
                    "Action": "logs:PutLogEvents",
                    "Resource": [spans_arn, app_signals_arn],
                    "Condition": {
                        "ArnLike": {"aws:SourceArn": source_arn},
                        "StringEquals": {"aws:SourceAccount": self.account},
                    },
                }
            ],
        }
        return json.dumps(document)

    # ------------------------------------------------------------------
    # Stack outputs.
    # ------------------------------------------------------------------

    def _emit_outputs(self) -> None:
        """Emit the AgentCore log-group name as a stack output.

        Surfaces the derived log-group name (``/aws/bedrock-agentcore/runtimes/
        <agent>``) so the Operator can confirm where AgentCore Observability
        writes without inspecting the synthesized template.
        """
        CfnOutput(
            self,
            "AgentCoreLogGroupName",
            value=self.log_group_name,
            description=(
                "CloudWatch log group used by AgentCore Observability for the "
                "agent runtime."
            ),
        )
