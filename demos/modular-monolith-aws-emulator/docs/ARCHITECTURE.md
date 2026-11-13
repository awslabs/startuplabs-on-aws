# Architecture

## Request flow

```
client
  -> StranglerMiddleware          strangler.yaml: legacy prefix? proxy to APP_LEGACY_BASE_URL
  -> module router (api.py)       validates the pydantic DTO, resolves CurrentPrincipal
  -> use case (application.py)    orchestration, publishes events
  -> domain.py                    entities and invariants, no IO
  -> repository / facade          SqlRepository, DocumentRepository, StorageFacade, ComputeFacade
  -> port (Protocol)
  -> aws adapter                  boto3 -> AWS Emulator (127.0.0.1:9297) or AWS
```

Errors travel up as `DomainError` (`shared/errors.py`). Adapters translate botocore codes and `main.py` maps them to HTTP: 422 validation, 404, 409 conflict, 401 with `WWW-Authenticate: Bearer`, 403, and 503 when the platform fails (generic message to the client, details in the log).

## Modules

| Module | Exposes | Uses | Data |
|---|---|---|---|
| identity | `/api/v1/auth/*`, `contract.CurrentPrincipal`, `require_group`, event `UserRegistered` | IdentityFacade, SqlRepository | Cognito (credentials), `users` table (projection) |
| profiles | `/api/v1/profiles/*` | identity.contract, DatabaseFacade.documents, StorageFacade | DynamoDB `profiles` table, `avatars/<sub>/` objects |
| jobs | `/api/v1/jobs/*` | identity.contract, ComputeFacade | `word-count` Lambda |

Communication between modules:

- synchronous: only through the owning module's `contract.py`;
- asynchronous: the in-process `EventBus`. `profiles` subscribes to `UserRegistered` and creates the profile. When a module becomes a service, the bus is the seam that switches to SNS, SQS or EventBridge without touching the handlers.

Adding a module (worked example with sequence diagrams: [guides/buy-product.md](guides/buy-product.md)):

1. `src/app/modules/<name>/module.py` with `MODULE = ModuleSpec(...)`;
2. `<name>` in `INSTALLED` (`modules/__init__.py`);
3. migrations in `<name>/migrations/NNN_*.sql`, or tables in `document_tables`;
4. the prefix in `strangler.yaml` with `owner: modern`;
5. the module in the independence contract list in `pyproject.toml`.

## Facades and ports

| Facade | Methods | Port(s) | AWS adapter |
|---|---|---|---|
| `identity` | `register`, `confirm`, `login`, `refresh`, `logout`, `grant_group`, `user_info`, `verify` | `IdentityProvider`, `TokenVerifier` | `CognitoIdentityProvider`, `CognitoJwtVerifier` |
| `database` | `sql`, `documents(<logical table>)` | `SqlDatabase`, `DocumentStore` | `RdsDataSqlDatabase`, `DynamoDbDocumentStore` |
| `storage` | `save`, `load`, `remove`, `keys`, `upload_url`, `download_url` | `ObjectStorage` | `S3ObjectStorage` |
| `compute` | `run_function(<logical name>)`, `containers`, `machines` | `FunctionRuntime`, `ContainerRuntime`, `MachineFleet` | Lambda, ECS Fargate, EC2 |
| `network` | `discover(<service>)` | `ServiceRegistry` (+ `NetworkProvisioner`, `Dns`, `LoadBalancers` in the bootstrap) | Cloud Map, EC2, Route 53, ELBv2 |

Facades speak in logical names ("profiles", "word-count"); `container.py` maps them to physical names (`sup-profiles`, `sup-word-count`).

## Resources provisioned by the bootstrap

| Resource | Name | Notes |
|---|---|---|
| Cognito user pool | `sup-users` | email sign-in, password of 8+ characters with upper case, lower case, digit and symbol; `api` client without secret; `admin` group |
| Aurora PostgreSQL | `sup-db`, database `app` | Data API on, password managed in Secrets Manager, encrypted storage |
| DynamoDB | `sup-profiles` | on-demand, SSE |
| S3 | `sup-assets-<account>` | Block Public Access, SSE-S3, denies non-TLS |
| Lambda | `sup-word-count` | python3.12, own role |
| VPC | `sup-vpc` 10.20.0.0/16 | 2 private subnets, SGs `sup-app` and `sup-db` (5432 from the app SG only) |
| Cloud Map | `sup.local` | `api` service registered with the API host and port |

The identifiers go to `.platform/outputs.json` (git-ignored), which `config.py` reads when the matching `APP_*` variable is not set.

## Language parity

The Go, TypeScript and Spring versions must keep:

- the recipes `just install`, `just dev`, `just test`, `just explain`, `just demo`, `just bootstrap`, `just admin`, `just rollout`, `just status`, `just destroy`;
- the same stacks (`<project>-<stage>-platform` and `-api`), the same public routes in API Gateway and the same post-deploy checks. The `platform` stack does not depend on the language and can be copied as is; only the API Lambda bundle and handler change;
- the same ports and methods (names adapted to each language's conventions);
- `strangler.yaml` in the same format, with the same example legacy route;
- the same resource names and the same keys in `.platform/outputs.json`;
- the same dependency rules, with each language's tool (Go: `go-arch-lint` or `internal/` packages; TypeScript: `dependency-cruiser`; Spring: Spring Modulith or ArchUnit);
- the same `scripts/demo.sh`, which tests the API over HTTP and works for any language.

SDK adjustments already covered by the emulator guide:

| SDK | Certificate | Host prefix |
|---|---|---|
| Go v2 | `AWS_CA_BUNDLE` | not listed in the emulator guide; confirm when porting |
| JavaScript v3 | `NODE_EXTRA_CA_CERTS` | `disableHostPrefix: true` |
| Java 2.x | `truststore.p12` through `-Djavax.net.ssl.trustStore` | `SdkAdvancedClientOption.DISABLE_HOST_PREFIX_INJECTION` |

In all of them: endpoint `https://127.0.0.1:9297` (not `localhost`, which makes JS, Java and Go use S3 virtual-host addressing) and credentials read from the emulator's file.
