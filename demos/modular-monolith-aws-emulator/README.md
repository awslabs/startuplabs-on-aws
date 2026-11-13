# Modular monolith on the AWS Emulator

A modular monolith boilerplate in Python (FastAPI) that runs entirely on your machine against the [AWS Emulator](https://github.com/aws/aws-emulator). No AWS account needed. The same code targets real AWS by changing one variable (`APP_PLATFORM_TARGET=aws`), and `just rollout` deploys it with CDK.

What comes ready:

- **Auth and register**: sign-up, confirmation, login, refresh, logout and `/me` on Cognito, with local JWT verification against the user pool JWKS and group-based authorization (`admin`).
- **DB repository**: `SqlRepository` (Aurora PostgreSQL through the RDS Data API) and `DocumentRepository` (DynamoDB), with per-module SQL migrations.
- **Adapters** for compute (Lambda, ECS Fargate, EC2), database (RDS Data API, DynamoDB), storage (S3 with presigned URLs) and network (VPC, security groups, Cloud Map, Route 53, internal ALB).
- **Strangler fig** at the HTTP edge: a route table (`strangler.yaml`) decides, per path prefix, whether the monolith answers or the request goes to the legacy system.
- **Facades** as the only interface business modules see.

## Quick start

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just) (`brew install just`).

```bash
just install   # Python dependencies + AWS Emulator image
just dev       # emulator + provisioning + legacy stub + API on http://127.0.0.1:8080/docs
just demo      # in another terminal: walks through every module over HTTP
just test      # lint, types, architecture, unit and integration tests on the emulator
just explain   # layers, modules, routes, strangler table and adapters, read from the code
```

`just` with no arguments lists every recipe. The main ones:

| Recipe | What it does |
|---|---|
| `just install` | `uv sync` and builds the emulator image (aarch64 or x86_64 RPM, detected) |
| `just dev` | starts the emulator, runs the bootstrap, starts the legacy stub on `:8081` and the API with reload on `:8080` |
| `just test` | `lint` + `test-unit` + `test-integration` |
| `just explain` | architecture summary generated from the modules and `strangler.yaml` |
| `just test-unit` | tests on in-memory adapters, no emulator |
| `just test-integration` | starts the emulator, provisions it and runs the tests against it |
| `just bootstrap` | creates the user pool, Aurora cluster, tables, bucket, Lambda, VPC and namespace in the emulator |
| `just admin EMAIL` | adds a user to the `admin` group |
| `just emulator-up` / `emulator-down` / `emulator-logs` | emulator lifecycle |
| `just rollout` | CDK deploy to the active AWS account, with a live per-service dashboard |
| `just status` | live health of the services on AWS (`r` refreshes, `q` quits, `--once` for one pass) |
| `just destroy` | deletes the AWS stacks |
| `just synth` | Lambda bundle + `cdk synth` with cdk-nag |

## Code layout

```
src/app/
  main.py                FastAPI + StranglerMiddleware + module routers
  container.py           composition root: the only place that picks adapters
  bootstrap.py           provisions the emulator resources (idempotent)
  explain.py             `just explain`
  config.py              APP_* settings (pydantic-settings)
  lambda_handler.py      AWS Lambda entry point (Mangum)
  strangler/             route table + proxy to the legacy system
  modules/
    identity/            auth + register (Cognito + users table in SQL)
    profiles/            profile in DynamoDB, avatar through presigned S3 URLs
    jobs/                work on the compute facade (word-count Lambda)
  platform/
    facades/             identity, database, storage, compute, network
    ports/               one Protocol per service
    adapters/aws/        boto3, pointed at the emulator or at AWS
    adapters/memory/     the same ports without network, for tests
  repository/            SqlRepository, DocumentRepository, migrations
  shared/                domain errors, Principal, EventBus
functions/word_count/    Lambda handler (python3.12) deployed by the bootstrap and by CDK
infra/                   CDK (platform and api stacks), rollout, status and destroy
strangler.yaml           who answers each prefix: modern or legacy
```

Each module is a vertical slice: `api.py` (HTTP) calls `application.py` (use cases), which uses `domain.py` (rules, no IO), repositories and facades. A module talks to another one only through that module's `contract.py` (FastAPI dependencies and events such as `UserRegistered`). Four import-linter contracts enforce this and run in `just test`:

1. modules are independent of each other, except through `contract`;
2. modules do not import `platform.adapters`, `boto3` or `botocore`;
3. the platform does not know the modules;
4. facades and ports do not import adapters.

Details and decisions in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/adr/](docs/adr/). To add a feature, follow [docs/guides/buy-product.md](docs/guides/buy-product.md): sequence diagrams and the code for a `buy_product` feature, from the new module to `just rollout`.

## How the adapters reach the emulator

`AwsClientFactory` (`platform/adapters/aws/session.py`) is the only place that knows where AWS is. With `APP_PLATFORM_TARGET=emulator` (the default) it:

- uses `https://127.0.0.1:9297` and refuses any host that is not loopback;
- reads the keys the emulator generates on every start from `~/.aws/emulator/9297/emulator_credentials` and passes them explicitly, so real credentials in the environment (`AWS_PROFILE`, env vars, IMDS) are never used;
- trusts the emulator's `ca.pem`, forces S3 path-style with SigV4 and turns off host prefix injection.

You do not need to export anything to run the app. To use the AWS CLI against the same emulator: `source scripts/emulator-env.sh`.

## Strangler fig

```yaml
# strangler.yaml
default: modern
routes:
  - prefix: /api/v1/auth
    owner: modern
  - prefix: /api/v1/orders
    owner: legacy
```

`legacy` routes are forwarded to `APP_LEGACY_BASE_URL` (in `just dev`, the stub in `src/app/legacy/app.py`). Only the path and query come from the client: scheme, host and port are fixed by configuration. The `Authorization` header is forwarded unchanged, and the response comes back with `x-strangler-route: legacy`. Migrating a capability means implementing the module and switching the prefix `owner` to `modern`.

The same pattern applies at the adapter level: `container.py` builds each facade with one adapter, and swapping an adapter (say, from an in-memory legacy store to S3) changes neither the module nor the facade.

## Security

See [CONTRIBUTING](../../CONTRIBUTING.md#security-issue-notifications) for more information.

## License

Apache-2.0, the license of this repository. See [LICENSE](../../LICENSE).
