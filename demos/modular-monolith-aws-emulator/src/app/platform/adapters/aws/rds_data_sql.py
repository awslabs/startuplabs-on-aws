"""Aurora PostgreSQL through the RDS Data API (SqlDatabase port).

The Data API is HTTPS + SigV4 + a Secrets Manager secret: no drivers, no
connection pool, no database port to open. On the emulator the engine is SQLite
behind a PostgreSQL front end, so write portable PostgreSQL.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.platform.adapters.aws.errors import translate
from app.platform.adapters.aws.session import AwsClientFactory
from app.platform.ports.database import Row
from app.shared import errors

_PG_UNIQUE_VIOLATION = "SQLState: 23505"
# Aurora Serverless v2 with min capacity 0 auto-pauses; the first call while it
# resumes fails with DatabaseResumingException. Retry within a bounded window.
_RESUME_WINDOW_SECONDS = 25.0


def _param(name: str, value: Any) -> dict[str, Any]:
    p: dict[str, Any] = {"name": name}
    match value:
        case None:
            p["value"] = {"isNull": True}
        case bool():
            p["value"] = {"booleanValue": value}
        case int():
            p["value"] = {"longValue": value}
        case float():
            p["value"] = {"doubleValue": value}
        case bytes():
            p["value"] = {"blobValue": value}
        case datetime():
            p["value"] = {"stringValue": value.strftime("%Y-%m-%d %H:%M:%S.%f")}
            p["typeHint"] = "TIMESTAMP"
        case date():
            p["value"] = {"stringValue": value.isoformat()}
            p["typeHint"] = "DATE"
        case dict() | list():
            p["value"] = {"stringValue": json.dumps(value)}
            p["typeHint"] = "JSON"
        case _:
            p["value"] = {"stringValue": str(value)}
    return p


def _params(params: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [_param(k, v) for k, v in (params or {}).items()]


def _db_error(message: str) -> errors.DomainError:
    if _PG_UNIQUE_VIOLATION in message:
        return errors.Conflict("unique constraint violated")
    return errors.PlatformUnavailable(message)


def _call(fn: Any, **kwargs: Any) -> Any:
    deadline = time.monotonic() + _RESUME_WINDOW_SECONDS
    delay = 1.0
    while True:
        try:
            return fn(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code != "DatabaseResumingException" or time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 5.0)


class RdsDataSqlDatabase:
    def __init__(
        self,
        clients: AwsClientFactory,
        cluster_arn: str,
        secret_arn: str,
        database: str,
        transaction_id: str | None = None,
    ) -> None:
        self._clients = clients
        self._rd = clients.client("rds-data")
        self._target = {"resourceArn": cluster_arn, "secretArn": secret_arn, "database": database}
        self._tx = transaction_id

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> list[Row]:
        kwargs: dict[str, Any] = {
            **self._target,
            "sql": sql,
            "parameters": _params(params),
            "formatRecordsAs": "JSON",
        }
        if self._tx:
            kwargs["transactionId"] = self._tx
        with translate({"DatabaseErrorException": _db_error}):
            resp = _call(self._rd.execute_statement, **kwargs)
        records = resp.get("formattedRecords")
        return list(json.loads(records)) if records else []

    def execute_many(self, sql: str, params: Sequence[Mapping[str, Any]]) -> None:
        kwargs: dict[str, Any] = {
            **self._target,
            "sql": sql,
            "parameterSets": [_params(p) for p in params],
        }
        if self._tx:
            kwargs["transactionId"] = self._tx
        with translate({"DatabaseErrorException": _db_error}):
            _call(self._rd.batch_execute_statement, **kwargs)

    @contextmanager
    def transaction(self) -> Iterator[RdsDataSqlDatabase]:
        if self._tx:  # already inside one: join it
            yield self
            return
        target = {k: self._target[k] for k in ("resourceArn", "secretArn", "database")}
        with translate():
            tx = _call(self._rd.begin_transaction, **target)["transactionId"]
        scoped = RdsDataSqlDatabase(
            self._clients,
            self._target["resourceArn"],
            self._target["secretArn"],
            self._target["database"],
            tx,
        )
        try:
            yield scoped
        except BaseException:
            with translate():
                self._rd.rollback_transaction(
                    resourceArn=target["resourceArn"],
                    secretArn=target["secretArn"],
                    transactionId=tx,
                )
            raise
        with translate():
            self._rd.commit_transaction(
                resourceArn=target["resourceArn"], secretArn=target["secretArn"], transactionId=tx
            )
