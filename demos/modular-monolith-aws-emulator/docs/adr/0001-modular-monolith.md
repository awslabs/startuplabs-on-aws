# 0001 Modular monolith with enforced boundaries

Status: accepted, 2026-11-12

## Context

The boilerplate is a starting point for teams that run a legacy system today and want to grow without starting with microservices.

## Decision

One process, several modules under `src/app/modules/`, each a vertical slice. Modules talk only through `contract.py` and `EventBus` events. import-linter runs four contracts in `just lint` and in the test `tests/architecture/test_boundaries.py`.

## Consequences

- One deploy, one database per kind of data, local transactions.
- Extracting a module into a service means replacing the `EventBus` with messaging and the `contract` with an HTTP client; the rest of the module does not change.
- A boundary violation breaks the build; it does not depend on manual review.
