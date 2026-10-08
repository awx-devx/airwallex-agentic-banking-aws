"""DynamoDB implementation of awx_common.store.KitStore on the spine's ledger table.

One item per (kit, tenant, key): pk = "<kit>#<tenant_id>", sk = key. The instance is scoped
at construction, so kit code can never name another kit's or tenant's partition. Writes are
optimistic: expected_version=None creates (attribute_not_exists), an int must equal the
stored version, and every successful put bumps the version by one. Values are stored as
awx_common.transport JSON strings, so amounts never become DynamoDB numbers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from awx_common.kits import KITS
from awx_common.store import StoredItem, VersionConflict
from awx_common.transport import dumps, loads

MAX_VALUE_BYTES = 350_000  # DynamoDB items cap at 400 KB; leave room for keys and metadata


class DynamoDbKitStore:
    def __init__(
        self, table_name: str, *, kit: str, tenant_id: str, client: Any | None = None
    ) -> None:
        if kit not in KITS:
            raise ValueError(f"unknown kit {kit!r}")
        if not tenant_id or "#" in tenant_id:
            raise ValueError("tenant_id is required and may not contain '#'")
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        self._table = table_name
        self._db = client
        self._pk = f"{kit}#{tenant_id}"

    def get(self, key: str) -> StoredItem | None:
        item = self._db.get_item(
            TableName=self._table,
            Key={"pk": {"S": self._pk}, "sk": {"S": _key(key)}},
            ConsistentRead=True,
        ).get("Item")
        return None if item is None else _item(item)

    def put(
        self, key: str, value: Mapping[str, Any], *, expected_version: int | None
    ) -> StoredItem:
        text = dumps(value)
        if len(text.encode()) > MAX_VALUE_BYTES:
            raise ValueError(f"value for {key!r} is too large for one ledger item")
        version = 1 if expected_version is None else expected_version + 1
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "Item": {
                "pk": {"S": self._pk},
                "sk": {"S": _key(key)},
                "value": {"S": text},
                "version": {"N": str(version)},
            },
        }
        if expected_version is None:
            # A real conditional create on the full key (pk, sk). Of two concurrent
            # creates of one key exactly one passes; the other gets VersionConflict.
            kwargs["ConditionExpression"] = "attribute_not_exists(pk) AND attribute_not_exists(sk)"
        else:
            kwargs["ConditionExpression"] = "version = :v"
            kwargs["ExpressionAttributeValues"] = {":v": {"N": str(expected_version)}}
        try:
            self._db.put_item(**kwargs)
        except self._db.exceptions.ConditionalCheckFailedException:
            if expected_version is None:
                raise VersionConflict(f"{key} already exists") from None
            raise VersionConflict(f"{key} is not at version {expected_version}") from None
        return StoredItem(key, loads(text), version)

    def query(self, prefix: str) -> Sequence[StoredItem]:
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "ConsistentRead": True,
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": self._pk}},
        }
        if prefix:
            kwargs["KeyConditionExpression"] += " AND begins_with(sk, :prefix)"
            kwargs["ExpressionAttributeValues"][":prefix"] = {"S": prefix}
        out: list[StoredItem] = []
        while True:
            resp = self._db.query(**kwargs)
            out.extend(_item(i) for i in resp["Items"])
            if "LastEvaluatedKey" not in resp:
                return sorted(out, key=lambda i: i.key)
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _key(key: str) -> str:
    if not key:
        raise ValueError("key is required")
    return key


def _item(item: Mapping[str, Any]) -> StoredItem:
    return StoredItem(item["sk"]["S"], loads(item["value"]["S"]), int(item["version"]["N"]))
