from __future__ import annotations

from app.platform.ports.network import Endpoint, ServiceRegistry


class NetworkFacade:
    """Service discovery inside the application's namespace."""

    def __init__(self, registry: ServiceRegistry | None, namespace: str) -> None:
        self._registry = registry
        self.namespace = namespace

    def discover(self, service: str) -> list[Endpoint]:
        if self._registry is None:
            return []
        return self._registry.discover(self.namespace, service)
