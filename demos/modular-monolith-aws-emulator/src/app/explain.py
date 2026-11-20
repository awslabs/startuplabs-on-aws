"""`just explain`: prints how this codebase is wired, computed from the code itself."""

from __future__ import annotations

import ssl

import httpx

from app import __version__
from app.config import get_settings
from app.container import build_memory_platform, load_modules
from app.main import create_app
from app.platform.adapters.aws.session import EmulatorEndpoint
from app.strangler import Owner

LAYERS = """
  HTTP  ->  StranglerMiddleware  --legacy-->  APP_LEGACY_BASE_URL (system being replaced)
                 | modern
                 v
  modules/<name>/api.py          (FastAPI router, pydantic DTOs)
                 v
  modules/<name>/application.py  (use cases: orchestrate facades + repositories + events)
                 v
  modules/<name>/domain.py       (entities and rules, no IO)
                 v
  platform/facades/*             (simple interface: identity, database, storage, compute, network)
                 v
  platform/ports/*               (Protocols: the contract of each service)
                 v
  platform/adapters/aws/*        (boto3 -> AWS Emulator on 127.0.0.1:9297, or real AWS)
  platform/adapters/memory/*     (same ports, no network: unit tests)
"""

ADAPTERS = [
    ("identity", "IdentityProvider + TokenVerifier", "Cognito user pool + JWKS (JWT)"),
    ("database", "SqlDatabase", "Aurora PostgreSQL via RDS Data API"),
    ("database", "DocumentStore", "DynamoDB"),
    ("storage", "ObjectStorage", "S3 (presigned URLs)"),
    ("compute", "FunctionRuntime", "Lambda (python3.12)"),
    ("compute", "ContainerRuntime", "ECS Fargate"),
    ("compute", "MachineFleet", "EC2 (IMDSv2 required)"),
    ("network", "NetworkProvisioner", "VPC, private subnets, security groups"),
    ("network", "ServiceRegistry", "Cloud Map"),
    ("network", "Dns", "Route 53 private zone"),
    ("network", "LoadBalancers", "internal ALB (ELBv2)"),
]

RULES = [
    "a module imports another module only via <module>.contract (events + FastAPI deps)",
    "a module never imports platform.adapters, boto3 or botocore",
    "facades depend on ports, never on adapters",
    "app.container is the only place that picks adapters (swap = strangler at adapter level)",
    "enforced by `lint-imports` in `just test`",
]


def _emulator_status(ep: EmulatorEndpoint) -> str:
    if not ep.ca_bundle.exists():
        return "stopped (run `just emulator-up`)"
    try:
        ctx = ssl.create_default_context(cafile=str(ep.ca_bundle))
        r = httpx.get(f"{ep.url}/__admin/health", verify=ctx, timeout=2.0)
        return "running" if r.status_code == 200 else f"HTTP {r.status_code}"
    except httpx.HTTPError:
        return "stopped (run `just emulator-up`)"


def main() -> None:
    settings = get_settings()
    modules = load_modules()
    app = create_app(settings, build_memory_platform(settings, modules))
    table = app.state.routes
    ep = EmulatorEndpoint(settings.emulator_host, settings.emulator_port, settings.emulator_home)

    target = settings.platform_target.value
    print(f"modular-monolith-aws-emulator v{__version__}  |  target: {target}")
    print(f"emulator {ep.url}: {_emulator_status(ep)}")
    print("\n== Layers (modular monolith + ports/adapters + facades) ==")
    print(LAYERS)

    print("== Modules ==")
    # app.openapi() is the public view of every mounted route (works with /docs off).
    operations = [
        (method.upper(), path, op.get("tags", [""])[0])
        for path, item in app.openapi()["paths"].items()
        for method, op in item.items()
    ]
    for m in modules:
        print(f"  {m.name:<9} {m.summary}")
        for method, path, tag in operations:
            if tag == m.name:
                print(f"      {method:<6} {path}")

    print("\n== Strangler fig (strangler.yaml) ==")
    legacy = settings.legacy_base_url or "(APP_LEGACY_BASE_URL not set)"
    width = max([len(r.prefix) for r in table.routes] + [len("(everything else)")]) + 2
    for r in table.routes:
        dest = "monolith" if r.owner is Owner.MODERN else f"legacy -> {legacy}"
        print(f"  {r.prefix:<{width}} {dest}")
    rest = "monolith" if table.default is Owner.MODERN else f"legacy -> {legacy}"
    print(f"  {'(everything else)':<{width}} {rest}")

    print("\n== Adapters per facade ==")
    for facade, port, impl in ADAPTERS:
        print(f"  {facade:<9} {port:<34} {impl}")

    print("\n== Dependency rules ==")
    for rule in RULES:
        print(f"  - {rule}")

    print("\n== Commands ==")
    print("  just install   dependencies + emulator image")
    print("  just dev       emulator + bootstrap + legacy stub + API with reload (127.0.0.1:8080)")
    print("  just test      lint, types, architecture, unit and integration tests on the emulator")
    print("  just explain   this summary")
    print("  just rollout   CDK on the active AWS account, live per-service dashboard")
    print("  just status    live health of the services on AWS (r refresh, q quit)")
    print("  just destroy   delete the AWS stacks")


if __name__ == "__main__":
    main()
