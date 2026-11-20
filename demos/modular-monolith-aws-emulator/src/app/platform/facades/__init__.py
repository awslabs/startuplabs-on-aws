"""Facades: the only platform surface business modules see.

A facade speaks the application's language (logical names, plain values) and
hides which adapter, bucket, table or ARN sits behind it. Modules receive a
`Platform` and never import `app.platform.adapters` (enforced by import-linter).
"""

from app.platform.facades.compute import ComputeFacade
from app.platform.facades.database import DatabaseFacade
from app.platform.facades.identity import IdentityFacade
from app.platform.facades.network import NetworkFacade
from app.platform.facades.platform import Platform
from app.platform.facades.storage import StorageFacade

__all__ = [
    "ComputeFacade",
    "DatabaseFacade",
    "IdentityFacade",
    "NetworkFacade",
    "Platform",
    "StorageFacade",
]
