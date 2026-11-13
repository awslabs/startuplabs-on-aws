"""Each adapter against the emulator: proves the port contract holds on 'real' AWS APIs."""

from __future__ import annotations

import ssl
import uuid

import httpx
import pytest

from app.config import Settings
from app.platform.adapters.aws.cognito_identity import CognitoIdentityProvider
from app.platform.adapters.aws.compute import (
    Ec2MachineFleet,
    EcsContainerRuntime,
    LambdaFunctionRuntime,
)
from app.platform.adapters.aws.dynamodb_store import DynamoDbDocumentStore
from app.platform.adapters.aws.jwt_verifier import CognitoJwtVerifier
from app.platform.adapters.aws.network import (
    CloudMapServiceRegistry,
    Ec2NetworkProvisioner,
    Elbv2LoadBalancers,
    Route53Dns,
)
from app.platform.adapters.aws.rds_data_sql import RdsDataSqlDatabase
from app.platform.adapters.aws.s3_storage import S3ObjectStorage
from app.platform.adapters.aws.session import AwsClientFactory
from app.shared import errors
from tests.credentials import PASSWORD, WRONG_PASSWORD


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def test_cognito_and_jwks(emu_settings: Settings, clients: AwsClientFactory) -> None:
    pool, client = emu_settings.cognito_user_pool_id, emu_settings.cognito_client_id
    assert pool and client
    idp = CognitoIdentityProvider(clients, pool, client, auto_confirm=True)
    email = f"it-{_uid()}@example.com"
    assert idp.sign_up(email, PASSWORD, "IT").confirmed
    with pytest.raises(errors.Conflict):
        idp.sign_up(email, PASSWORD, "IT")
    with pytest.raises(errors.Unauthorized):
        idp.authenticate(email, WRONG_PASSWORD)
    tokens = idp.authenticate(email, PASSWORD)
    verifier = CognitoJwtVerifier(clients.cognito_issuer(pool), client, clients.ca_bundle)
    principal = verifier.verify(tokens.access_token)
    with pytest.raises(errors.Unauthorized):
        verifier.verify(tokens.id_token)  # id token is not an access token
    idp.add_to_group(email, "admin")
    assert verifier.verify(idp.authenticate(email, PASSWORD).access_token).in_group("admin")
    assert tokens.refresh_token
    assert idp.refresh(tokens.refresh_token).access_token
    assert principal.subject


def test_rds_data_sql(emu_settings: Settings, clients: AwsClientFactory) -> None:
    db = RdsDataSqlDatabase(
        clients,
        emu_settings.require("db_cluster_arn"),
        emu_settings.require("db_secret_arn"),
        emu_settings.db_name,
    )
    db.execute("DROP TABLE IF EXISTS it_adapter_probe")
    db.execute("CREATE TABLE it_adapter_probe (id TEXT PRIMARY KEY, n INTEGER, ok BOOLEAN)")
    db.execute(
        "INSERT INTO it_adapter_probe (id, n, ok) VALUES (:id, :n, :ok)",
        {"id": "a", "n": 1, "ok": True},
    )
    with pytest.raises(errors.Conflict):
        db.execute("INSERT INTO it_adapter_probe (id, n) VALUES (:id, :n)", {"id": "a", "n": 2})
    with pytest.raises(RuntimeError), db.transaction() as tx:
        tx.execute("INSERT INTO it_adapter_probe (id, n) VALUES ('b', 2)")
        raise RuntimeError("rollback")
    with db.transaction() as tx:
        tx.execute("INSERT INTO it_adapter_probe (id, n) VALUES ('c', 3)")
    rows = db.execute("SELECT id, n FROM it_adapter_probe ORDER BY id")
    assert rows == [{"id": "a", "n": 1}, {"id": "c", "n": 3}]
    db.execute("DROP TABLE it_adapter_probe")


def test_dynamodb(emu_settings: Settings, clients: AwsClientFactory) -> None:
    store, table = DynamoDbDocumentStore(clients), emu_settings.require("profiles_table")
    key = f"it-{_uid()}"
    store.put(table, {"pk": key, "n": 1, "f": 1.5, "tags": ["a"]}, if_absent="pk")
    with pytest.raises(errors.Conflict):
        store.put(table, {"pk": key}, if_absent="pk")
    assert store.get(table, {"pk": key}) == {"pk": key, "n": 1, "f": 1.5, "tags": ["a"]}
    store.delete(table, {"pk": key})
    assert store.get(table, {"pk": key}) is None


def test_s3_and_presigned_urls(emu_settings: Settings, clients: AwsClientFactory) -> None:
    s3, bucket = S3ObjectStorage(clients), emu_settings.require("assets_bucket")
    key = f"it/{_uid()}.txt"
    s3.put(bucket, key, b"hello", "text/plain")
    assert s3.get(bucket, key).body == b"hello"  # type: ignore[union-attr]
    assert key in s3.list_keys(bucket, "it/")
    up = s3.presign_put(bucket, f"{key}.up", "text/plain")
    assert (
        httpx.put(
            up,
            content=b"up",
            headers={"content-type": "text/plain"},
            verify=ssl.create_default_context(cafile=clients.ca_bundle),
        ).status_code
        == 200
    )
    down = s3.presign_get(bucket, f"{key}.up")
    assert (
        httpx.get(down, verify=ssl.create_default_context(cafile=clients.ca_bundle)).content
        == b"up"
    )
    s3.delete(bucket, key)
    assert s3.get(bucket, key) is None
    public = clients.client("s3").get_public_access_block(Bucket=bucket)
    assert all(public["PublicAccessBlockConfiguration"].values())


def test_lambda_runs_python(emu_settings: Settings, clients: AwsClientFactory) -> None:
    result = LambdaFunctionRuntime(clients).invoke(
        emu_settings.require("word_count_function"), {"text": "x y x"}
    )
    assert result.status_code == 200
    assert result.payload["words"] == 3


def test_network_compute_and_discovery(emu_settings: Settings, clients: AwsClientFactory) -> None:
    net = Ec2NetworkProvisioner(clients)
    name = f"it-{_uid()}"
    first = net.ensure_private_network(name, "10.99.0.0/16", zones=2)
    again = net.ensure_private_network(name, "10.99.0.0/16", zones=2)
    assert first == again and len(first.subnet_ids) == 2
    sg_app = net.ensure_security_group(first.vpc_id, "app", "app")
    sg_db = net.ensure_security_group(first.vpc_id, "db", "db")
    net.allow_from_group(sg_db, sg_app, 5432)
    net.allow_from_group(sg_db, sg_app, 5432)  # idempotent
    rules = clients.client("ec2").describe_security_groups(GroupIds=[sg_db])["SecurityGroups"][0]
    assert all(not r.get("IpRanges") for r in rules["IpPermissions"])  # no CIDR ingress

    dns = Route53Dns(clients)
    zone = dns.ensure_private_zone(f"{name}.internal", first.vpc_id)
    assert dns.ensure_private_zone(f"{name}.internal", first.vpc_id) == zone
    dns.upsert_a_record(zone, f"db.{name}.internal", "10.99.1.10")

    lb = Elbv2LoadBalancers(clients).ensure_internal_alb(name, list(first.subnet_ids), sg_app)
    assert lb

    registry = CloudMapServiceRegistry(clients)
    endpoints = registry.discover(emu_settings.resource_prefix + ".local", "api")
    assert endpoints and endpoints[0].port == emu_settings.port

    ecs = clients.client("ecs")
    ecs.create_cluster(clusterName=name)
    ecs.register_task_definition(
        family=name,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="256",
        memory="512",
        containerDefinitions=[
            {"name": "w", "image": "public.ecr.aws/docker/library/busybox", "essential": True}
        ],
    )
    containers = EcsContainerRuntime(clients)
    task = containers.run_task(name, name, [first.subnet_ids[0]], [sg_app])
    assert containers.describe_task(name, task.task_arn).task_arn == task.task_arn

    ami = clients.client("ssm").get_parameter(
        Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
    )["Parameter"]["Value"]
    fleet = Ec2MachineFleet(clients)
    machine = fleet.launch(ami, "t4g.micro", first.subnet_ids[0], {"Name": name})
    assert fleet.describe(machine.instance_id).tags["Name"] == name
    fleet.terminate(machine.instance_id)
