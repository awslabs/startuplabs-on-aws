"""Stateful platform: network, identity, SQL, documents, storage, functions, discovery."""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_servicediscovery as servicediscovery
from constructs import Construct

from infra.nag import ack
from infra.settings import ROOT, Deployment, output_id

LOG_RETENTION = logs.RetentionDays.ONE_MONTH


class PlatformStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, dep: Deployment, **kwargs: object) -> None:
        super().__init__(scope, cid, **kwargs)
        removal = RemovalPolicy.RETAIN if dep.is_prod else RemovalPolicy.DESTROY

        # Network: isolated subnets only (no NAT, no internet route). Aurora lives
        # here; the API Lambda reaches it through the RDS Data API (HTTPS + IAM).
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"{dep.prefix}-vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                )
            ],
        )
        flow_logs = logs.LogGroup(
            self, "VpcFlowLogs", retention=LOG_RETENTION, removal_policy=removal
        )
        self.vpc.add_flow_log(
            "FlowLog", destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_logs)
        )
        db_sg = ec2.SecurityGroup(
            self,
            "DbSecurityGroup",
            vpc=self.vpc,
            description="Aurora: no ingress, accessed only through the RDS Data API",
            allow_all_outbound=False,
        )

        # Identity
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{dep.prefix}-users",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            sign_in_case_sensitive=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                fullname=cognito.StandardAttribute(required=False, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            deletion_protection=dep.is_prod,
            removal_policy=removal,
        )
        self.user_pool_client = self.user_pool.add_client(
            "ApiClient",
            user_pool_client_name="api",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True),
            prevent_user_existence_errors=True,
            access_token_validity=Duration.minutes(60),
            id_token_validity=Duration.minutes(60),
            refresh_token_validity=Duration.days(30),
            enable_token_revocation=True,
        )
        cognito.UserPoolGroup(
            self,
            "AdminGroup",
            user_pool=self.user_pool,
            group_name="admin",
            description="app admins",
        )

        # SQL: Aurora PostgreSQL Serverless v2, scales to 0 ACU when idle.
        # Log group created here (not via cloudwatch_logs_retention) so no
        # LogRetention custom-resource Lambda is needed.
        db_logs = logs.LogGroup(
            self,
            "DatabaseLogs",
            log_group_name=f"/aws/rds/cluster/{dep.prefix}-db/postgresql",
            retention=LOG_RETENTION,
            removal_policy=removal,
        )
        self.cluster = rds.DatabaseCluster(
            self,
            "Database",
            cluster_identifier=f"{dep.prefix}-db",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_13
            ),
            writer=rds.ClusterInstance.serverless_v2("writer"),
            serverless_v2_min_capacity=0,
            serverless_v2_max_capacity=2,
            serverless_v2_auto_pause_duration=Duration.minutes(10),
            credentials=rds.Credentials.from_generated_secret("appadmin"),
            default_database_name="app",
            enable_data_api=True,
            iam_authentication=True,
            storage_encrypted=True,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[db_sg],
            backup=rds.BackupProps(retention=Duration.days(7 if dep.is_prod else 1)),
            cloudwatch_logs_exports=["postgresql"],
            deletion_protection=dep.is_prod,
            removal_policy=RemovalPolicy.SNAPSHOT if dep.is_prod else RemovalPolicy.DESTROY,
        )
        self.cluster.node.add_dependency(db_logs)
        assert self.cluster.secret is not None

        # Documents
        self.profiles = dynamodb.Table(
            self,
            "ProfilesTable",
            table_name=f"{dep.prefix}-profiles",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=removal,
        )

        # Storage
        access_logs = s3.Bucket(
            self,
            "AccessLogsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=removal,
            auto_delete_objects=not dep.is_prod,
        )
        self.assets = s3.Bucket(
            self,
            "AssetsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=dep.is_prod,
            server_access_logs_bucket=access_logs,
            server_access_logs_prefix="assets/",
            removal_policy=removal,
            auto_delete_objects=not dep.is_prod,
        )

        # Functions
        self.word_count = lambda_.Function(
            self,
            "WordCountFunction",
            function_name=f"{dep.prefix}-word-count",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(ROOT / "functions" / "word_count")),
            timeout=Duration.seconds(10),
            memory_size=128,
            log_group=logs.LogGroup(
                self, "WordCountLogs", retention=LOG_RETENTION, removal_policy=removal
            ),
        )

        # Discovery
        self.namespace = servicediscovery.PrivateDnsNamespace(
            self, "Namespace", name=f"{dep.prefix}.local", vpc=self.vpc
        )
        self.namespace.create_service(
            "ApiService", name="api", dns_record_type=servicediscovery.DnsRecordType.A
        )

        outputs = {
            "cognito_user_pool_id": self.user_pool.user_pool_id,
            "cognito_client_id": self.user_pool_client.user_pool_client_id,
            "db_cluster_arn": self.cluster.cluster_arn,
            "db_secret_arn": self.cluster.secret.secret_arn,
            "profiles_table": self.profiles.table_name,
            "assets_bucket": self.assets.bucket_name,
            "word_count_function": self.word_count.function_name,
            "service_namespace": self.namespace.namespace_name,
            "vpc_id": self.vpc.vpc_id,
        }
        for key, value in outputs.items():
            CfnOutput(self, output_id(key), value=value)
        self.outputs = outputs

        self._suppressions(dep)

    def _suppressions(self, dep: Deployment) -> None:
        basic_exec = (
            "AwsSolutions::AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/"
            "service-role/AWSLambdaBasicExecutionRole]"
        )
        ack(
            self.user_pool,
            {
                "AwsSolutions::AwsSolutions-COG2": "MFA is OPTIONAL (TOTP); require it per "
                "product decision.",
                "AwsSolutions::AwsSolutions-COG8": "Plus feature plan (threat protection) has a "
                "per-MAU cost; ESSENTIALS is the boilerplate default.",
            },
        )
        ack(
            self.cluster.node.find_child("Secret"),  # the DatabaseSecret, not the attachment
            {
                "AwsSolutions::AwsSolutions-SMG4": "Rotation needs a rotation Lambda in the "
                "isolated VPC plus a Secrets Manager endpoint; the app only uses the Data API.",
            },
        )
        if not dep.is_prod:
            ack(
                self.cluster,
                {
                    "AwsSolutions::AwsSolutions-RDS10": "Non-prod stage: torn down by "
                    "`just destroy`. Prod stages enable deletion protection.",
                },
            )
        ack(
            self.word_count,
            {
                basic_exec: "Grants only CreateLogStream/PutLogEvents for its own logs.",
                "AwsSolutions::AwsSolutions-L1": "python3.12 is the runtime validated "
                "against the AWS Emulator.",
            },
        )
