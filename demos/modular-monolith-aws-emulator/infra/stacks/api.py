"""Stateless API: the FastAPI app on Lambda behind an HTTP API with a Cognito JWT gate."""

from __future__ import annotations

import jsii
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from infra.nag import ack
from infra.settings import LAMBDA_BUILD, Deployment
from infra.stacks.platform import LOG_RETENTION, PlatformStack

# cdk-nag renders cross-stack ARNs as "<producer stack>:<export logical id>". The
# logical ids are derived from construct paths in PlatformStack, so they are stable.
EXPORTS = {
    "AssetsBucket": "ExportsOutputFnGetAttAssetsBucket5CB76180ArnBF60D959",
    "WordCountFunction": "ExportsOutputFnGetAttWordCountFunction0D76BCB1ArnD097C5E2",
}

# Routes reachable without a token: an end user has to be able to sign up and in.
PUBLIC_ROUTES = [
    ("/api/v1/auth/register", apigw.HttpMethod.POST),
    ("/api/v1/auth/confirm", apigw.HttpMethod.POST),
    ("/api/v1/auth/login", apigw.HttpMethod.POST),
    ("/api/v1/auth/refresh", apigw.HttpMethod.POST),
    ("/healthz", apigw.HttpMethod.GET),
]


@jsii.implements(apigw.IAccessLogSettings)
class _AccessLogSettings:
    """HTTP API access logs (the L2 takes an interface, not a struct)."""

    def __init__(self, destination: apigw.IAccessLogDestination, fmt: str) -> None:
        self._destination = destination
        self._format = apigateway.AccessLogFormat.custom(fmt)

    @property
    def destination(self) -> apigw.IAccessLogDestination:
        return self._destination

    @property
    def format(self) -> apigateway.AccessLogFormat:
        return self._format


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        dep: Deployment,
        platform: PlatformStack,
        alarm_email: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, cid, **kwargs)
        removal = RemovalPolicy.RETAIN if dep.is_prod else RemovalPolicy.DESTROY
        assert platform.cluster.secret is not None

        env = {
            "APP_PLATFORM_TARGET": "aws",
            "APP_AWS_REGION": self.region,
            "APP_RESOURCE_PREFIX": dep.prefix,
            "APP_STRANGLER_CONFIG": "/var/task/strangler.yaml",
            "APP_COGNITO_USER_POOL_ID": platform.user_pool.user_pool_id,
            "APP_COGNITO_CLIENT_ID": platform.user_pool_client.user_pool_client_id,
            "APP_DB_CLUSTER_ARN": platform.cluster.cluster_arn,
            "APP_DB_SECRET_ARN": platform.cluster.secret.secret_arn,
            "APP_DB_NAME": "app",
            "APP_PROFILES_TABLE": platform.profiles.table_name,
            "APP_ASSETS_BUCKET": platform.assets.bucket_name,
            "APP_WORD_COUNT_FUNCTION": platform.word_count.function_name,
        }
        fn = lambda_.Function(
            self,
            "ApiFunction",
            function_name=f"{dep.prefix}-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="app.lambda_handler.handler",
            code=lambda_.Code.from_asset(str(LAMBDA_BUILD)),
            memory_size=512,
            timeout=Duration.seconds(29),
            environment=env,
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self, "ApiLogs", retention=LOG_RETENTION, removal_policy=removal
            ),
        )

        # Least privilege, through grant*() wherever a grant exists.
        platform.profiles.grant_read_write_data(fn)
        # Exact object actions on the avatars/ prefix. bucket.grant_read_write would
        # add GetObject*, List*, Abort* wildcards that the app never needs.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[platform.assets.arn_for_objects("avatars/*")],
            )
        )
        platform.cluster.grant_data_api_access(fn)
        platform.word_count.grant_invoke(fn)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["servicediscovery:DiscoverInstances"],
                resources=[platform.namespace.namespace_arn],
            )
        )

        api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name=f"{dep.prefix}-api",
            create_default_stage=False,
            default_integration=integrations.HttpLambdaIntegration("App", fn),
            # Every route requires a valid access token unless listed in PUBLIC_ROUTES.
            default_authorizer=authorizers.HttpJwtAuthorizer(
                "Cognito",
                jwt_issuer=(
                    f"https://cognito-idp.{self.region}.amazonaws.com/"
                    f"{platform.user_pool.user_pool_id}"
                ),
                # HTTP API checks `aud` or, for Cognito access tokens, `client_id`.
                jwt_audience=[platform.user_pool_client.user_pool_client_id],
            ),
        )
        public_integration = integrations.HttpLambdaIntegration("AppPublic", fn)
        for path, method in PUBLIC_ROUTES:
            api.add_routes(
                path=path,
                methods=[method],
                integration=public_integration,
                authorizer=apigw.HttpNoneAuthorizer(),
            )

        access_logs = logs.LogGroup(
            self, "ApiAccessLogs", retention=LOG_RETENTION, removal_policy=removal
        )
        stage = apigw.HttpStage(
            self,
            "DefaultStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=20, burst_limit=40),
            access_log_settings=_AccessLogSettings(
                apigw.LogGroupLogDestination(access_logs),
                '{"requestId":"$context.requestId","ip":"$context.identity.sourceIp",'
                '"method":"$context.httpMethod","path":"$context.path",'
                '"status":"$context.status","latencyMs":"$context.responseLatency",'
                '"authError":"$context.authorizer.error"}',
            ),
        )

        self._alarms(dep, api, stage, fn, alarm_email)

        url = f"https://{api.api_id}.execute-api.{self.region}.amazonaws.com"
        CfnOutput(self, "ApiUrl", value=url)
        CfnOutput(self, "ApiFunctionName", value=fn.function_name)
        self._producer = dep.platform_stack
        self._suppressions(api, fn)

    def _alarms(
        self,
        dep: Deployment,
        api: apigw.HttpApi,
        stage: apigw.HttpStage,
        fn: lambda_.Function,
        email: str | None,
    ) -> None:
        topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=f"{dep.prefix}-alarms",
            master_key=kms.Alias.from_alias_name(self, "SnsKey", "alias/aws/sns"),
            enforce_ssl=True,
        )
        if email:
            topic.add_subscription(subscriptions.EmailSubscription(email))
        action = cw_actions.SnsAction(topic)
        dims = {"ApiId": api.api_id, "Stage": "$default"}

        def metric(name: str, stat: str) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="AWS/ApiGateway",
                metric_name=name,
                dimensions_map=dims,
                statistic=stat,
                period=Duration.minutes(1),
            )

        five_xx_rate = cloudwatch.MathExpression(
            expression="IF(count > 0, 100 * errors / count, 0)",
            using_metrics={"errors": metric("5xx", "Sum"), "count": metric("Count", "Sum")},
            period=Duration.minutes(1),
            label="5xx %",
        )
        alarms = [
            five_xx_rate.create_alarm(
                self,
                "Api5xxRate",
                alarm_name=f"{dep.prefix}-api-5xx-rate",
                alarm_description="Sev2: 5xx rate above 0.5% for 3 minutes",
                threshold=0.5,
                evaluation_periods=3,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ),
            metric("Latency", "p99").create_alarm(
                self,
                "ApiLatencyP99",
                alarm_name=f"{dep.prefix}-api-latency-p99",
                alarm_description="Sev3: p99 latency above 3 s for 5 minutes",
                threshold=3000,
                evaluation_periods=5,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ),
            fn.metric_errors(period=Duration.minutes(1)).create_alarm(
                self,
                "ApiFunctionErrors",
                alarm_name=f"{dep.prefix}-api-function-errors",
                alarm_description="Sev2: unhandled Lambda errors for 3 minutes",
                threshold=1,
                evaluation_periods=3,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ),
            fn.metric_throttles(period=Duration.minutes(1)).create_alarm(
                self,
                "ApiFunctionThrottles",
                alarm_name=f"{dep.prefix}-api-function-throttles",
                alarm_description="Sev3: Lambda throttling",
                threshold=1,
                evaluation_periods=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ),
        ]
        for alarm in alarms:
            alarm.add_alarm_action(action)
        self.alarm_names = [a.alarm_name for a in alarms]

    def _suppressions(self, api: apigw.HttpApi, fn: lambda_.Function) -> None:
        ack(
            fn,
            {
                "AwsSolutions::AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/"
                "service-role/AWSLambdaBasicExecutionRole]": "Grants only CreateLogStream/"
                "PutLogEvents for its own logs.",
                "AwsSolutions::AwsSolutions-IAM5[Resource::*]": "X-Ray PutTraceSegments/"
                "PutTelemetryRecords do not support resource-level permissions.",
                "AwsSolutions::AwsSolutions-L1": "python3.12 is the runtime validated against "
                "the AWS Emulator.",
            },
        )
        ack(
            fn,
            {
                f"AwsSolutions::AwsSolutions-IAM5[Resource::{self._export('AssetsBucket')}"
                "/avatars/*]": "Object keys under the avatars/ prefix only.",
                f"AwsSolutions::AwsSolutions-IAM5[Resource::{self._export('WordCountFunction')}"
                ":*]": "grant_invoke adds <function-arn>:* for versions/aliases of one function.",
            },
        )
        for path, method in PUBLIC_ROUTES:
            route_id = f"{method.value}{path.replace('/', '--')}"
            ack(
                api.node.find_child(route_id),
                {
                    "AwsSolutions::AwsSolutions-APIG4": "Public by design: sign-up, sign-in, "
                    "token refresh and health need no prior identity (see PUBLIC_ROUTES).",
                },
            )

    def _export(self, logical: str) -> str:
        return f"{self._producer}:{EXPORTS[logical]}"
