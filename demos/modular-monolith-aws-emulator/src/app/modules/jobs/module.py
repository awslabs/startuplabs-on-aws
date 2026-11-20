"""Jobs: runs work on the compute facade (a Lambda function, by logical name)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.modules.base import ModuleSpec
from app.modules.identity.contract import CurrentPrincipal
from app.platform.facades import Platform


class WordCountIn(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class JobOut(BaseModel):
    function: str
    result: Any


def _router(platform: Platform) -> APIRouter:
    router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

    @router.post("/word-count")
    def word_count(principal: CurrentPrincipal, body: WordCountIn) -> JobOut:
        result = platform.compute.run_function(
            "word-count", {"text": body.text, "requested_by": principal.subject}
        )
        return JobOut(function="word-count", result=result.payload)

    return router


MODULE = ModuleSpec(
    name="jobs",
    summary="synchronous jobs on the compute facade (Lambda word-count)",
    build_router=_router,
)
