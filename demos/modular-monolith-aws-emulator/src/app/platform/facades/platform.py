from __future__ import annotations

from dataclasses import dataclass

from app.platform.facades.compute import ComputeFacade
from app.platform.facades.database import DatabaseFacade
from app.platform.facades.identity import IdentityFacade
from app.platform.facades.network import NetworkFacade
from app.platform.facades.storage import StorageFacade
from app.shared.events import EventBus


@dataclass(frozen=True, slots=True)
class Platform:
    """Everything a module may use. Passed to each module's `register()`."""

    identity: IdentityFacade
    database: DatabaseFacade
    storage: StorageFacade
    compute: ComputeFacade
    network: NetworkFacade
    events: EventBus
    target: str
