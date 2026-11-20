"""AWS Lambda entry point (API Gateway HTTP API, payload v2). Used by `just rollout`."""

from __future__ import annotations

import logging

from mangum import Mangum

from app.config import get_settings
from app.main import create_app

logging.getLogger().setLevel(get_settings().log_level.upper())

# Built once per execution environment (cold start), reused across invocations.
handler = Mangum(create_app(), lifespan="off")
