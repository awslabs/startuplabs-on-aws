"""In-process event bus: the only asynchronous coupling between modules.

Modules publish facts (`UserRegistered`) and other modules subscribe. When a
module is extracted to its own service (the strangler fig end state), this bus
is the seam that gets replaced by SNS/SQS/EventBridge without touching handlers.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for integration events. Subclasses are frozen dataclasses."""


E = TypeVar("E", bound=Event)
Handler = Callable[[Any], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        for handler in self._handlers[type(event)]:
            try:
                handler(event)
            except Exception:
                # A failing subscriber must not roll back the publisher's work.
                log.exception("event handler failed", extra={"event": type(event).__name__})

    def subscriptions(self) -> dict[str, list[str]]:
        return {
            et.__name__: [getattr(h, "__qualname__", repr(h)) for h in hs]
            for et, hs in self._handlers.items()
        }
