"""Strangler fig facade at the HTTP edge.

Every request enters the modular monolith. A route table decides, per path
prefix, whether the new code (`modern`) answers or the request is proxied to
the system being replaced (`legacy`). Migrating a capability = moving its
prefix from `legacy` to `modern` in `strangler.yaml`. When nothing is left on
`legacy`, delete the proxy.
"""

from app.strangler.middleware import StranglerMiddleware
from app.strangler.routes import Owner, Route, RouteTable

__all__ = ["Owner", "Route", "RouteTable", "StranglerMiddleware"]
