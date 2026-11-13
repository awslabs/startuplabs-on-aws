"""Settings. Every value can be overridden with an `APP_` environment variable.

Resource identifiers (pool id, cluster ARN, bucket...) come from, in order:
1. explicit env vars (`APP_COGNITO_USER_POOL_ID`, ...), used on real AWS where
   IaC outputs are injected;
2. `.platform/outputs.json`, written by `just bootstrap` against the emulator.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class PlatformTarget(StrEnum):
    EMULATOR = "emulator"
    AWS = "aws"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    # HTTP server. Loopback by default: exposing it is a deliberate decision.
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "info"

    # Where the adapters point.
    platform_target: PlatformTarget = PlatformTarget.EMULATOR
    aws_region: str = "us-east-1"
    emulator_host: str = "127.0.0.1"
    emulator_port: int = 9297
    emulator_home: Path = Path("~/.aws/emulator").expanduser()

    # Naming prefix for every provisioned resource.
    resource_prefix: str = "sup"

    # OpenAPI UI (/docs, /redoc, /openapi.json). Default: on for the emulator, off on AWS.
    expose_docs: bool | None = None

    # Dev convenience: confirm sign-ups without the e-mail code. Refused on AWS.
    identity_auto_confirm: bool = False

    # Strangler fig.
    strangler_config: Path = Path("strangler.yaml")
    legacy_base_url: str | None = None

    # Resource identifiers (see module docstring).
    outputs_file: Path = Path(".platform/outputs.json")
    cognito_user_pool_id: str | None = None
    cognito_client_id: str | None = None
    db_cluster_arn: str | None = None
    db_secret_arn: str | None = None
    db_name: str = "app"
    profiles_table: str | None = None
    assets_bucket: str | None = None
    word_count_function: str | None = None

    def with_outputs(self) -> Settings:
        """Fill unset identifiers from the bootstrap outputs file, if present."""
        if not self.outputs_file.exists():
            return self
        outputs: dict[str, str] = json.loads(self.outputs_file.read_text())
        updates = {
            key: value
            for key, value in outputs.items()
            if key in type(self).model_fields and getattr(self, key) in (None, "")
        }
        return self.model_copy(update=updates)

    @property
    def docs_enabled(self) -> bool:
        if self.expose_docs is None:
            return self.platform_target is PlatformTarget.EMULATOR
        return self.expose_docs

    def require(self, name: str) -> str:
        value = getattr(self, name)
        if not value:
            raise RuntimeError(
                f"setting '{name}' is not configured: run `just bootstrap` "
                f"or set APP_{name.upper()}"
            )
        return str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings().with_outputs()
