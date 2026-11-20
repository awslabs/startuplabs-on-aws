# Building a feature: buy_product

This guide adds a `buy_product` feature end to end, following the boilerplate structure: a new `orders` module, its SQL tables, a strangler route that takes purchases away from the legacy system, tests on in-memory adapters and on the AWS Emulator, and the CDK change it needs before `just rollout`.

The finished code ships as [buy-product.patch](buy-product.patch). Apply it from this folder with `patch -p1 < docs/guides/buy-product.patch`. The snippets below are abridged from it; with the patch applied, the project passes `just lint`, 68 unit tests, 8 integration tests on the emulator, and `cdk synth` with 0 open cdk-nag findings. The patched version has not been deployed to AWS; section 7 shows what `just rollout` does with it.

What the feature does:

- `GET /api/v1/orders/products`: catalog with stock.
- `POST /api/v1/orders/purchases`: buy `quantity` of `product_id` as the logged-in user. Stock is decremented atomically; a purchase that would oversell gets 409 and changes nothing.
- `GET /api/v1/orders/purchases`: the caller's orders.
- `GET /api/v1/orders/purchases/{order_id}/receipt`: presigned URL for the JSON receipt stored in S3.
- Publishes a `ProductPurchased` event other modules can subscribe to.

## Files you touch

| File | Why |
|---|---|
| `src/app/modules/orders/domain.py` | `Product`, `Order`, `Order.place()` validates quantity |
| `src/app/modules/orders/migrations/001_orders.sql` | `products` and `orders` tables, `CHECK (stock >= 0)`, seed catalog |
| `src/app/modules/orders/infrastructure.py` | `ProductRepository` (with the atomic `take_stock`), `OrderRepository` |
| `src/app/modules/orders/contract.py` | `ProductPurchased` event, the only file other modules may import |
| `src/app/modules/orders/application.py` | `OrderService.buy_product()`: transaction, receipt, event |
| `src/app/modules/orders/api.py` | FastAPI routes and DTOs, `CurrentPrincipal` from `identity.contract` |
| `src/app/modules/orders/module.py` | `MODULE = ModuleSpec(...)` wires the service to the platform facades |
| `src/app/modules/__init__.py` | `"orders"` in `INSTALLED` |
| `strangler.yaml` | `/api/v1/orders/products` and `/api/v1/orders/purchases` owned by `modern` |
| `pyproject.toml` | `app.modules.orders` in the import-linter independence contract |
| `infra/stacks/api.py` | S3 grant on `receipts/*` and its cdk-nag acknowledgment |
| `tests/unit/test_orders.py`, `tests/integration/test_orders_e2e.py` | proof |

Things you do not touch: `main.py`, `container.py`, the facades, the adapters, the bootstrap, the Lambda handler and the API Gateway routes. Module discovery, migrations, the JWT authorizer and the Lambda bundle all pick up a new module from `INSTALLED`.

## 1. Development workflow

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant J as just
    participant R as Repository files
    participant L as ruff, mypy, import-linter
    participant T as pytest
    participant E as AWS Emulator

    Dev->>J: just explain
    J-->>Dev: modules, routes, strangler table, adapters

    Note over Dev,R: Scaffold the module
    Dev->>R: modules/orders/domain.py, infrastructure.py, contract.py
    Dev->>R: modules/orders/application.py, api.py, module.py
    Dev->>R: modules/orders/migrations/001_orders.sql

    Note over Dev,R: Register it
    Dev->>R: modules/__init__.py, add "orders" to INSTALLED
    Dev->>R: strangler.yaml, purchases and products owned by modern
    Dev->>R: pyproject.toml, orders in the independence contract
    Dev->>R: infra/stacks/api.py, S3 grant on receipts/*

    Note over Dev,E: Prove it
    Dev->>R: tests/unit/test_orders.py, tests/integration/test_orders_e2e.py
    Dev->>J: just test-unit
    J->>T: pytest -m "not integration"
    T-->>Dev: pass, on in-memory adapters and SQLite
    Dev->>J: just lint
    J->>L: orders may import identity.contract only
    L-->>Dev: 4 contracts kept
    Dev->>J: just test-integration
    J->>E: emulator-up, then bootstrap
    E-->>J: migration orders/001_orders.sql applied
    J->>T: pytest -m integration
    T-->>Dev: pass, every adapter on the emulator
    Dev->>J: just dev, then call the API
    Dev->>J: just rollout
```

## 2. Write the module

**Domain.** Rules that need no IO live here. `Order.place()` rejects a bad quantity before any database call:

```python
@classmethod
def place(cls, user_id: str, product: Product, quantity: int) -> Order:
    if not 1 <= quantity <= MAX_QUANTITY:
        raise errors.ValidationFailed(f"quantity must be between 1 and {MAX_QUANTITY}")
    return cls(id=str(uuid.uuid4()), user_id=user_id, product_id=product.id,
               quantity=quantity, total_cents=product.price_cents * quantity,
               created_at=datetime.now(UTC).replace(microsecond=0))
```

**Migration.** Portable PostgreSQL: it runs on SQLite in unit tests, on the emulator's Data API and on Aurora. The `CHECK` is the last line of defense against negative stock.

```sql
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    stock INTEGER NOT NULL CHECK (stock >= 0)
);
```

**Repository.** Subclass `SqlRepository`, map rows, add what the domain needs. A race-sensitive invariant goes into one SQL statement, not a read-modify-write in Python:

```python
def take_stock(self, product_id: str, quantity: int) -> int | None:
    rows = self.db.execute(
        "UPDATE products SET stock = stock - :q WHERE id = :id AND stock >= :q "
        "RETURNING stock",
        {"q": quantity, "id": product_id},
    )
    return int(rows[0]["stock"]) if rows else None
```

**Contract.** Events other modules may react to. Nothing else in `orders` is importable from outside:

```python
@dataclass(frozen=True, slots=True)
class ProductPurchased(Event):
    order_id: str
    user_id: str
    product_id: str
    quantity: int
    total_cents: int
```

**Use case.** The transaction covers the stock decrement and the order row. The receipt and the event happen only after commit:

```python
def buy_product(self, principal: Principal, product_id: str, quantity: int) -> Order:
    with self._db.transaction() as tx:
        products, orders = ProductRepository(tx), OrderRepository(tx)
        product = products.get(product_id)
        if product is None:
            raise errors.NotFound(f"product {product_id}")
        order = Order.place(principal.subject, product, quantity)
        if products.take_stock(product_id, quantity) is None:
            raise errors.Conflict("insufficient stock")
        orders.add(order)
    self._storage.save(f"receipts/{principal.subject}/{order.id}.json", ..., "application/json")
    self._events.publish(ProductPurchased(order.id, order.user_id, order.product_id,
                                          order.quantity, order.total_cents))
    return order
```

**API and wiring.** Routes take `CurrentPrincipal` from `identity.contract`; the module receives the facades it needs from `Platform`:

```python
@router.post("/api/v1/orders/purchases", status_code=status.HTTP_201_CREATED)
def buy_product(principal: CurrentPrincipal, body: BuyProductIn) -> OrderOut:
    return OrderOut.of(service.buy_product(principal, body.product_id, body.quantity))
```

```python
def _router(platform: Platform) -> APIRouter:
    service = OrderService(platform.database.sql, platform.storage, platform.events)
    return build_router(service)

MODULE = ModuleSpec(name="orders", summary="buy_product: ...", build_router=_router,
                    migrations=Path(__file__).parent / "migrations")
```

## 3. What happens on a purchase

On `just dev`, every adapter below talks to the emulator on `127.0.0.1:9297`. On AWS the same code runs in Lambda against Cognito, Aurora and S3.

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant S as StranglerMiddleware
    participant A as orders api.py
    participant I as identity.contract
    participant V as IdentityFacade and CognitoJwtVerifier
    participant O as OrderService
    participant P as ProductRepository and OrderRepository
    participant D as SqlDatabase, RdsDataSqlDatabase
    participant St as StorageFacade, S3ObjectStorage
    participant B as EventBus
    participant E as AWS Emulator or AWS

    C->>S: POST /api/v1/orders/purchases, Bearer token
    S->>S: RouteTable.owner_of(path) is MODERN
    S->>A: forward to the FastAPI router
    A->>I: Depends(authenticated)
    I->>V: platform.identity.verify(token)
    V->>E: GET JWKS once, then cached
    V-->>I: Principal(subject, groups)
    A->>A: validate BuyProductIn (product_id, quantity 1 to 10)
    A->>O: buy_product(principal, product_id, quantity)
    O->>D: transaction()
    D->>E: rds-data BeginTransaction
    O->>P: products.get(product_id)
    P->>D: SELECT * FROM products WHERE id = :id
    O->>O: Order.place() validates quantity
    O->>P: products.take_stock(product_id, quantity)
    P->>D: UPDATE products ... WHERE stock >= :q RETURNING stock
    D-->>P: new stock
    O->>P: orders.add(order)
    P->>D: INSERT INTO orders ...
    D->>E: rds-data CommitTransaction
    O->>St: save receipts/sub/order_id.json
    St->>E: s3 PutObject
    O->>B: publish ProductPurchased
    B-->>B: call each subscriber, failures logged, never raised
    O-->>A: Order
    A-->>C: 201 OrderOut
```

## 4. Not enough stock

The decrement and the order insert share a transaction, so a failed purchase leaves no trace: no stock change, no order, no receipt, no event.

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant A as orders api.py
    participant O as OrderService
    participant P as ProductRepository
    participant D as SqlDatabase
    participant M as main.py error handler

    C->>A: POST /api/v1/orders/purchases, quantity 3, stock 2
    A->>O: buy_product(...)
    O->>D: transaction()
    O->>P: take_stock(product_id, 3)
    P->>D: UPDATE ... WHERE stock >= 3 RETURNING stock
    D-->>P: no rows
    P-->>O: None
    O->>O: raise Conflict("insufficient stock")
    O->>D: exception leaves the with block
    D->>D: RollbackTransaction
    O-->>M: Conflict propagates
    M-->>C: 409 error conflict, message insufficient stock
```

## 5. Strangling the legacy route

`/api/v1/orders` stays with the legacy system, which still serves order history. The two new prefixes are longer, so they win for the paths they cover:

```yaml
  - prefix: /api/v1/orders
    owner: legacy
  - prefix: /api/v1/orders/products
    owner: modern
  - prefix: /api/v1/orders/purchases
    owner: modern
```

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant S as StranglerMiddleware
    participant R as RouteTable
    participant M as orders module
    participant L as Legacy system, APP_LEGACY_BASE_URL

    C->>S: GET /api/v1/orders
    S->>R: owner_of("/api/v1/orders")
    R-->>S: LEGACY, prefix /api/v1/orders
    S->>L: same method, path, query and Authorization header
    L-->>S: order history
    S-->>C: response plus x-strangler-route legacy

    C->>S: POST /api/v1/orders/purchases
    S->>R: owner_of("/api/v1/orders/purchases")
    R-->>S: MODERN, the longest prefix wins
    S->>M: handled in the monolith
    M-->>C: 201 OrderOut
```

When order history also moves to the monolith, change `/api/v1/orders` to `modern` and delete the two longer entries. When no route is `legacy` any more, remove the proxy.

## 6. Tests

| Test | Runs on | Covers |
|---|---|---|
| `test_buy_product` | in-memory adapters, SQLite | 201, total, stock decremented, order listed, event published, receipt URL |
| `test_insufficient_stock_rolls_back` | in-memory | 409, stock unchanged, no order |
| `test_unknown_product_and_bad_quantity` | in-memory | 404, 422 |
| `test_requires_auth_and_hides_other_users_receipts` | in-memory | 401 without token, 404 on someone else's receipt |
| `test_strangler_routes_purchases_to_the_monolith` | route table | longest prefix wins, history stays legacy |
| `test_buy_product_on_the_emulator` | AWS Emulator | Cognito login, Data API transaction, 409, receipt fetched from S3 through the presigned URL |

Use the `client` and `platform` fixtures from `tests/conftest.py` for unit tests and `api` from `tests/integration/conftest.py` for the emulator. `register_and_login()` gives you an authenticated header.

## 7. Shipping it

The bundle, the migrations and the JWT authorizer need no change. The one infra change is IAM: the API Lambda could write only `avatars/*`, so the receipt needs a grant on `receipts/*`. cdk-nag flags the new wildcard, and the acknowledgment states why it is scoped:

```python
fn.add_to_role_policy(iam.PolicyStatement(
    actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
    resources=[platform.assets.arn_for_objects("avatars/*"),
               platform.assets.arn_for_objects("receipts/*")],
))
```

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant RO as just rollout
    participant CDK as CDK and cdk-nag
    participant CF as CloudFormation
    participant AU as Aurora Data API
    participant GW as API Gateway JWT authorizer
    participant FN as API Lambda, same FastAPI app

    Dev->>RO: just rollout
    RO->>RO: build bundle, now with modules/orders and its migration
    RO->>RO: pip-audit
    RO->>CDK: synth, AwsSolutions checks
    CDK-->>RO: 0 open findings, receipts/* acknowledged
    RO->>CDK: diff against the deployed stacks
    CDK-->>RO: changed ApiFunction code and IAM policy
    RO-->>Dev: guardrails panel and diff summary
    Dev->>RO: confirm
    RO->>CF: deploy the reviewed cdk.out
    CF-->>RO: sup-dev-api UPDATE_COMPLETE, shown live per service
    RO->>AU: migrations, applies orders/001_orders.sql
    RO->>GW: health, JWT gate, smoke test

    Note over Dev,FN: After the rollout
    Dev->>GW: POST /api/v1/orders/purchases, Bearer token
    GW->>GW: verify the Cognito JWT, 401 if missing or forged
    GW->>FN: invoke
    FN->>AU: same transaction as on the emulator
    FN-->>Dev: 201 OrderOut
```

The rollout's built-in smoke test does not call the new endpoints. To cover them on every deploy, add a step to `Checks.smoke()` in `infra/checks.py`.

## Checklist

- [ ] `modules/<name>/` with `domain`, `infrastructure`, `contract`, `application`, `api`, `module`
- [ ] migrations under `modules/<name>/migrations/`, portable PostgreSQL
- [ ] invariants that can race enforced in SQL (`WHERE ... RETURNING`, `CHECK`, `UNIQUE`)
- [ ] side effects (storage, events) after the transaction commits
- [ ] `INSTALLED`, `strangler.yaml`, import-linter contract in `pyproject.toml`
- [ ] IAM grants in `infra/stacks/api.py` for any new resource or prefix, with a cdk-nag reason
- [ ] unit tests on the `client` fixture, one integration test on the emulator
- [ ] `just test`, then `just rollout`
