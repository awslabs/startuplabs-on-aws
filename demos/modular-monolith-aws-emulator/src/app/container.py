"""Composition root: the only place that picks concrete adapters.

Strangler fig at the adapter level also happens here: to move one capability
from a legacy implementation to AWS, swap that single adapter below; modules
and facades do not change.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from typing import Any

from app.config import Settings
from app.modules import INSTALLED
from app.modules.base import ModuleSpec
from app.platform.adapters.aws.cognito_identity import CognitoIdentityProvider
from app.platform.adapters.aws.compute import (
    Ec2MachineFleet,
    EcsContainerRuntime,
    LambdaFunctionRuntime,
)
from app.platform.adapters.aws.dynamodb_store import DynamoDbDocumentStore
from app.platform.adapters.aws.jwt_verifier import CognitoJwtVerifier
from app.platform.adapters.aws.network import CloudMapServiceRegistry
from app.platform.adapters.aws.rds_data_sql import RdsDataSqlDatabase
from app.platform.adapters.aws.s3_storage import S3ObjectStorage
from app.platform.adapters.aws.session import AwsClientFactory
from app.platform.adapters.memory.identity import InMemoryIdentity
from app.platform.adapters.memory.stores import (
    InMemoryDocumentStore,
    InMemoryFunctionRuntime,
    InMemoryObjectStorage,
    SqliteSqlDatabase,
)
from app.platform.facades import (
    ComputeFacade,
    DatabaseFacade,
    IdentityFacade,
    NetworkFacade,
    Platform,
    StorageFacade,
)
from app.repository import migrations
from app.shared.events import EventBus


def load_modules() -> list[ModuleSpec]:
    return [importlib.import_module(f"app.modules.{n}.module").MODULE for n in INSTALLED]


def document_tables(settings: Settings, modules: list[ModuleSpec]) -> dict[str, str]:
    return {t: f"{settings.resource_prefix}-{t}" for m in modules for t in m.document_tables}


def namespace(settings: Settings) -> str:
    return f"{settings.resource_prefix}.local"


def build_platform(settings: Settings, modules: list[ModuleSpec]) -> Platform:
    """AWS adapters, pointed at the emulator or at AWS depending on settings."""
    if settings.identity_auto_confirm and settings.platform_target != "emulator":
        raise RuntimeError("APP_IDENTITY_AUTO_CONFIRM is only allowed against the emulator")
    clients = AwsClientFactory(settings)
    pool_id = settings.require("cognito_user_pool_id")
    client_id = settings.require("cognito_client_id")
    return Platform(
        identity=IdentityFacade(
            CognitoIdentityProvider(
                clients, pool_id, client_id, auto_confirm=settings.identity_auto_confirm
            ),
            CognitoJwtVerifier(clients.cognito_issuer(pool_id), client_id, clients.ca_bundle),
        ),
        database=DatabaseFacade(
            sql=RdsDataSqlDatabase(
                clients,
                settings.require("db_cluster_arn"),
                settings.require("db_secret_arn"),
                settings.db_name,
            ),
            documents=DynamoDbDocumentStore(clients),
            tables=document_tables(settings, modules),
        ),
        storage=StorageFacade(S3ObjectStorage(clients), settings.require("assets_bucket")),
        compute=ComputeFacade(
            LambdaFunctionRuntime(clients),
            {"word-count": settings.require("word_count_function")},
            containers=EcsContainerRuntime(clients),
            machines=Ec2MachineFleet(clients),
        ),
        network=NetworkFacade(CloudMapServiceRegistry(clients), namespace(settings)),
        events=EventBus(),
        target=settings.platform_target.value,
    )


def build_memory_platform(
    settings: Settings,
    modules: list[ModuleSpec],
    functions: Mapping[str, Callable[[Any], Any]] | None = None,
) -> Platform:
    """No network at all: unit tests and offline work."""
    functions = dict(functions or {})
    identity = InMemoryIdentity(auto_confirm=True)
    sql = SqliteSqlDatabase()
    migrations.apply(sql, [m.migrations for m in modules if m.migrations])
    return Platform(
        identity=IdentityFacade(identity, identity),
        database=DatabaseFacade(sql, InMemoryDocumentStore(), document_tables(settings, modules)),
        storage=StorageFacade(InMemoryObjectStorage(), "memory-assets"),
        compute=ComputeFacade(InMemoryFunctionRuntime(functions), {n: n for n in functions}),
        network=NetworkFacade(None, namespace(settings)),
        events=EventBus(),
        target="memory",
    )
