"""DynamoDB adapter (DocumentStore port). Plain Python values in and out."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from app.platform.adapters.aws.errors import translate
from app.platform.adapters.aws.session import AwsClientFactory
from app.platform.ports.database import Item
from app.shared import errors

_ser = TypeSerializer()
_de = TypeDeserializer()


def _to_ddb(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    return value


def _from_ddb(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    return value


def _marshal(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _ser.serialize(_to_ddb(v)) for k, v in item.items()}


def _unmarshal(item: Mapping[str, Any]) -> Item:
    return {k: _from_ddb(_de.deserialize(v)) for k, v in item.items()}


class DynamoDbDocumentStore:
    def __init__(self, clients: AwsClientFactory) -> None:
        self._ddb = clients.client("dynamodb")

    def put(self, table: str, item: Item, *, if_absent: str | None = None) -> None:
        kwargs: dict[str, Any] = {"TableName": table, "Item": _marshal(item)}
        if if_absent:
            kwargs["ConditionExpression"] = "attribute_not_exists(#k)"
            kwargs["ExpressionAttributeNames"] = {"#k": if_absent}
        with translate({"ConditionalCheckFailedException": errors.Conflict}):
            self._ddb.put_item(**kwargs)

    def get(self, table: str, key: Mapping[str, Any]) -> Item | None:
        with translate():
            resp = self._ddb.get_item(TableName=table, Key=_marshal(key), ConsistentRead=True)
        return _unmarshal(resp["Item"]) if "Item" in resp else None

    def delete(self, table: str, key: Mapping[str, Any]) -> None:
        with translate():
            self._ddb.delete_item(TableName=table, Key=_marshal(key))

    def scan(self, table: str, limit: int = 100) -> list[Item]:
        with translate():
            resp = self._ddb.scan(TableName=table, Limit=limit)
        return [_unmarshal(i) for i in resp.get("Items", [])]
