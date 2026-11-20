"""Provision every resource the app needs inside the AWS Emulator. Idempotent.

The emulator keeps state in memory, so this runs on every `just dev`. On real AWS
the same resources come from IaC (CDK/CloudFormation) and their identifiers are
injected as APP_* env vars; this script refuses to run there.

Usage:
    app-bootstrap                       # provision + migrate + write outputs
    app-bootstrap --grant-admin EMAIL   # add a user to the `admin` group
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from app.config import PlatformTarget, Settings, get_settings
from app.container import document_tables, load_modules, namespace
from app.platform.adapters.aws.network import CloudMapServiceRegistry, Ec2NetworkProvisioner
from app.platform.adapters.aws.rds_data_sql import RdsDataSqlDatabase
from app.platform.adapters.aws.session import AwsClientFactory
from app.platform.ports.network import Endpoint
from app.repository import migrations

log = logging.getLogger("bootstrap")
ROOT = Path(__file__).resolve().parents[2]
FUNCTIONS = ROOT / "functions"


def _code(err: ClientError) -> str:
    return str(err.response.get("Error", {}).get("Code", ""))


class Bootstrapper:
    def __init__(self, settings: Settings) -> None:
        if settings.platform_target is not PlatformTarget.EMULATOR:
            raise SystemExit("bootstrap only provisions the emulator; use IaC on AWS")
        self.s = settings
        self.c = AwsClientFactory(settings)
        self.prefix = settings.resource_prefix
        self.out: dict[str, str] = {}

    # identity ------------------------------------------------------------
    def identity(self) -> None:
        idp = self.c.client("cognito-idp")
        name = f"{self.prefix}-users"
        pool_id = next(
            (p["Id"] for p in idp.list_user_pools(MaxResults=60)["UserPools"] if p["Name"] == name),
            None,
        )
        if pool_id is None:
            pool_id = idp.create_user_pool(
                PoolName=name,
                UsernameAttributes=["email"],
                AutoVerifiedAttributes=["email"],
                Policies={
                    "PasswordPolicy": {
                        "MinimumLength": 8,
                        "RequireUppercase": True,
                        "RequireLowercase": True,
                        "RequireNumbers": True,
                        "RequireSymbols": True,
                    }
                },
                Schema=[{"Name": "name", "AttributeDataType": "String", "Mutable": True}],
            )["UserPool"]["Id"]
        clients = idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)["UserPoolClients"]
        client_id = next((c["ClientId"] for c in clients if c["ClientName"] == "api"), None)
        if client_id is None:
            client_id = idp.create_user_pool_client(
                UserPoolId=pool_id,
                ClientName="api",
                GenerateSecret=False,
                ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
                PreventUserExistenceErrors="ENABLED",
                AccessTokenValidity=60,
                IdTokenValidity=60,
                RefreshTokenValidity=30,
                TokenValidityUnits={
                    "AccessToken": "minutes",
                    "IdToken": "minutes",
                    "RefreshToken": "days",
                },
            )["UserPoolClient"]["ClientId"]
        try:
            idp.create_group(UserPoolId=pool_id, GroupName="admin", Description="app admins")
        except ClientError as err:
            if _code(err) != "GroupExistsException":
                raise
        self.out |= {"cognito_user_pool_id": pool_id, "cognito_client_id": client_id}

    # database ------------------------------------------------------------
    def database(self) -> None:
        rds = self.c.client("rds")
        ident = f"{self.prefix}-db"
        try:
            cluster = rds.describe_db_clusters(DBClusterIdentifier=ident)["DBClusters"][0]
        except ClientError as err:
            if _code(err) != "DBClusterNotFoundFault":
                raise
            cluster = rds.create_db_cluster(
                DBClusterIdentifier=ident,
                Engine="aurora-postgresql",
                DatabaseName=self.s.db_name,
                MasterUsername="appadmin",
                ManageMasterUserPassword=True,
                EnableHttpEndpoint=True,
                StorageEncrypted=True,
                DeletionProtection=False,
                ServerlessV2ScalingConfiguration={"MinCapacity": 0.5, "MaxCapacity": 2},
            )["DBCluster"]
        self.out |= {
            "db_cluster_arn": cluster["DBClusterArn"],
            "db_secret_arn": cluster["MasterUserSecret"]["SecretArn"],
        }

    def migrate(self, modules: list[Any]) -> list[str]:
        db = RdsDataSqlDatabase(
            self.c, self.out["db_cluster_arn"], self.out["db_secret_arn"], self.s.db_name
        )
        for _ in range(30):  # the managed secret is created asynchronously
            try:
                db.execute("SELECT 1")
                break
            except Exception:
                time.sleep(1)
        return migrations.apply(db, [m.migrations for m in modules if m.migrations])

    # documents -----------------------------------------------------------
    def documents(self, modules: list[Any]) -> None:
        ddb = self.c.client("dynamodb")
        existing = set(ddb.list_tables()["TableNames"])
        for logical, physical in document_tables(self.s, modules).items():
            if physical not in existing:
                ddb.create_table(
                    TableName=physical,
                    AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                    KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                    BillingMode="PAY_PER_REQUEST",
                    SSESpecification={"Enabled": True},
                )
            ddb.get_waiter("table_exists").wait(
                TableName=physical, WaiterConfig={"Delay": 1, "MaxAttempts": 30}
            )
            self.out[f"{logical}_table"] = physical

    # storage -------------------------------------------------------------
    def storage(self) -> None:
        s3 = self.c.client("s3")
        bucket = f"{self.prefix}-assets-{self.c.account_id()}"
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError:
            s3.create_bucket(Bucket=bucket)
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        s3.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
        s3.put_bucket_policy(
            Bucket=bucket,
            Policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "DenyInsecureTransport",
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": "s3:*",
                            "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                        }
                    ],
                }
            ),
        )
        self.out["assets_bucket"] = bucket

    # compute -------------------------------------------------------------
    def functions(self) -> None:
        iam, lam = self.c.client("iam"), self.c.client("lambda")
        role_name = f"{self.prefix}-fn-word-count"
        try:
            role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        except ClientError:
            role_arn = iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "lambda.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                            }
                        ],
                    }
                ),
            )["Role"]["Arn"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(FUNCTIONS / "word_count" / "handler.py", "handler.py")
        name = f"{self.prefix}-word-count"
        try:
            lam.get_function(FunctionName=name)
            lam.update_function_code(FunctionName=name, ZipFile=buf.getvalue())
        except ClientError as err:
            if _code(err) != "ResourceNotFoundException":
                raise
            lam.create_function(
                FunctionName=name,
                Runtime="python3.12",
                Role=role_arn,
                Handler="handler.handler",
                Code={"ZipFile": buf.getvalue()},
                Timeout=10,
                MemorySize=128,
            )
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
        self.out["word_count_function"] = name

    # network -------------------------------------------------------------
    def network(self) -> None:
        net = Ec2NetworkProvisioner(self.c)
        vpc = net.ensure_private_network(f"{self.prefix}-vpc", "10.20.0.0/16", zones=2)
        app_sg = net.ensure_security_group(vpc.vpc_id, f"{self.prefix}-app", "application tier")
        db_sg = net.ensure_security_group(vpc.vpc_id, f"{self.prefix}-db", "database tier")
        net.allow_from_group(db_sg, app_sg, 5432)
        registry = CloudMapServiceRegistry(self.c)
        ns = namespace(self.s)
        registry.ensure_namespace(ns, vpc.vpc_id)
        registry.register(ns, "api", "local-dev", Endpoint(self.s.host, self.s.port))
        self.out |= {
            "vpc_id": vpc.vpc_id,
            "subnet_ids": ",".join(vpc.subnet_ids),
            "app_security_group": app_sg,
            "db_security_group": db_sg,
            "service_namespace": ns,
        }

    def run(self) -> dict[str, str]:
        modules = load_modules()
        steps = [
            ("identity", self.identity),
            ("database", self.database),
            ("migrations", lambda: log.info("applied %s", self.migrate(modules) or "nothing")),
            ("documents", lambda: self.documents(modules)),
            ("storage", self.storage),
            ("compute", self.functions),
            ("network", self.network),
        ]
        for name, step in steps:
            log.info("-> %s", name)
            step()
        self.s.outputs_file.parent.mkdir(parents=True, exist_ok=True)
        self.s.outputs_file.write_text(json.dumps(self.out, indent=2) + "\n")
        return self.out


def grant_admin(settings: Settings, email: str) -> None:
    clients = AwsClientFactory(settings)
    clients.client("cognito-idp").admin_add_user_to_group(
        UserPoolId=settings.require("cognito_user_pool_id"),
        Username=email.lower(),
        GroupName="admin",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app-bootstrap")
    parser.add_argument("--grant-admin", metavar="EMAIL")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    settings = get_settings()
    if args.grant_admin:
        grant_admin(settings, args.grant_admin)
        print(f"{args.grant_admin} added to group admin (log in again to refresh tokens)")
        return 0
    out = Bootstrapper(settings).run()
    print(f"outputs written to {settings.outputs_file}:")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
