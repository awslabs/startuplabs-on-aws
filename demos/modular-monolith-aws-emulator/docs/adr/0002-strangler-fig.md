# 0002 Strangler fig at the HTTP edge and in the composition root

Status: accepted, 2026-11-12

## Decision

1. HTTP edge: `StranglerMiddleware` picks, per prefix (longest prefix wins), between the monolith and `APP_LEGACY_BASE_URL`, configured in `strangler.yaml`. `/healthz`, `/docs` and `/openapi.json` always belong to the monolith.
2. Adapters: `container.py` is the only place that picks the implementation of each port. Replacing a legacy adapter with an AWS one is a one-line change.

## Proxy rules

- The client controls only the path and query; scheme, host and port come from configuration.
- Hop-by-hop headers and `host` are not forwarded; `Authorization` is.
- Body limited to 10 MiB, 10 s timeout, redirects not followed.
- A `legacy` route with no upstream configured answers 502 `not_migrated`.
