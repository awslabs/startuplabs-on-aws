# 0004 AWS Emulator as the local environment

Status: accepted, 2026-11-12

## Decision

Use the AWS Emulator (a local server that implements the AWS APIs from their Smithy models) in a container, published only on `127.0.0.1:9297`, with SigV4 verification on. The image is built from the official RPM (`emulator/Dockerfile`), as the emulator guide describes.

## Why

- Same SDK, same calls and same errors as AWS: the `MasterUsername` validation and the PostgreSQL `timestamp` type check showed up here before any deploy.
- Lambda really runs the Python handler; Cognito issues JWTs with a JWKS; the RDS Data API runs real SQL.

## Caveats

- In-memory state and new keys on every start: the bootstrap is idempotent and runs on every `just dev`.
- Known divergences are tracked in the upstream repository's `issues/`; check before relying on a new operation.
- Not for security testing or production, as the emulator itself states.
