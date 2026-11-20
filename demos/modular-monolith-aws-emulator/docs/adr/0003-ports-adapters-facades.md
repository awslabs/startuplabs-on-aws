# 0003 Ports, adapters and facades

Status: accepted, 2026-11-12

## Decision

- Ports are `typing.Protocol` classes in `platform/ports/`, with no SDK dependency.
- AWS adapters live in `platform/adapters/aws/`, in-memory adapters in `platform/adapters/memory/`, implementing the same ports.
- Facades in `platform/facades/` are the only interface modules receive (`Platform`). They speak in logical names and hide buckets, tables and ARNs.
- Provider errors become `DomainError` inside the adapter.

## Consequences

- Unit tests run without network (62 tests in about 1.5 s).
- Integration tests exercise each adapter against the emulator, proving the port holds for the real APIs.
