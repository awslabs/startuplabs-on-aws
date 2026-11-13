"""CDK entry point. `just rollout` drives it; `just synth` runs it with cdk-nag."""

from __future__ import annotations

import os

from aws_cdk import App, Environment, Tags, Validations
from cdk_nag import AwsSolutionsChecks

from infra.settings import LAMBDA_BUILD, Deployment
from infra.stacks.api import ApiStack
from infra.stacks.platform import PlatformStack


def main() -> None:
    app = App()
    dep = Deployment(
        project=str(app.node.try_get_context("project") or "sup"),
        stage=str(app.node.try_get_context("stage") or "dev"),
    )
    if not (LAMBDA_BUILD / "app").is_dir():
        raise SystemExit("Lambda bundle missing: run `just build-lambda` (rollout does it)")
    env = Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=os.environ.get("CDK_DEFAULT_REGION")
    )
    platform = PlatformStack(app, dep.platform_stack, dep=dep, env=env)
    api = ApiStack(
        app,
        dep.api_stack,
        dep=dep,
        platform=platform,
        alarm_email=app.node.try_get_context("alarm_email"),
        env=env,
    )
    api.add_stack_dependency(platform)

    for key, value in {
        "Project": dep.project,
        "Environment": dep.stage,
        "ManagedBy": "CDK",
        "Repository": "modular-monolith-aws-emulator",
    }.items():
        Tags.of(app).add(key, value)
    Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))
    app.synth()


if __name__ == "__main__":
    main()
