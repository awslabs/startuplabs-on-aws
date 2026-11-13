"""Names shared by the CDK app, the rollout dashboard and the app's APP_* settings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAMBDA_BUILD = ROOT / "build" / "lambda"
OUTPUTS_FILE = ROOT / ".platform" / "outputs-aws.json"
CDK_OUTPUTS_FILE = ROOT / ".platform" / "cdk-outputs.json"
CDK_CLI = "aws-cdk@2.1148.1"

# PlatformStack outputs, keyed like the app's APP_* settings.
PLATFORM_OUTPUT_KEYS = (
    "cognito_user_pool_id",
    "cognito_client_id",
    "db_cluster_arn",
    "db_secret_arn",
    "profiles_table",
    "assets_bucket",
    "word_count_function",
    "service_namespace",
    "vpc_id",
)


def output_id(key: str) -> str:
    """CfnOutput construct id for a settings key (e.g. assets_bucket -> OutAssetsBucket)."""
    return "Out" + key.title().replace("_", "")


CDK_OUT = ROOT / "cdk.out"

_STAGE = re.compile(r"^[a-z][a-z0-9]{1,9}$")


@dataclass(frozen=True, slots=True)
class Deployment:
    project: str
    stage: str

    def __post_init__(self) -> None:
        if not _STAGE.match(self.stage) or not _STAGE.match(self.project):
            raise ValueError("project and stage must be 2-10 lowercase alphanumerics")

    @property
    def is_prod(self) -> bool:
        return self.stage in {"prod", "prd", "production"}

    @property
    def prefix(self) -> str:
        return f"{self.project}-{self.stage}"

    @property
    def platform_stack(self) -> str:
        return f"{self.prefix}-platform"

    @property
    def api_stack(self) -> str:
        return f"{self.prefix}-api"

    @property
    def stacks(self) -> tuple[str, str]:
        return (self.platform_stack, self.api_stack)
