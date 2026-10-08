"""DynamoDB implementation of awx_spine's OperationStore.

One item per Airwallex request_id, written with a conditional put BEFORE the network call.
The conditions keep awx_spine's store semantics under duplicate delivery and concurrent
retries:

- begin: attribute_not_exists(request_id). A second begin for the same request_id returns
  the stored record if the body hash matches and raises RequestIdReuse if it does not.
- record_attempt: one atomic UpdateItem increments attempts and sets status PENDING, only
  for an existing, non-definitive record.
- complete: when the result is OUTCOME_UNKNOWN, the same UpdateItem also sets
  may_have_landed = true; no write ever sets it false, and record_attempt
  leaves it alone, so once set it is never cleared.
- record_attempt: the same UpdateItem sets attempt_started_at = now and
  first_attempt_at = if_not_exists(first_attempt_at, now), so the first is written once.
- complete: overwrites any record whose stored result is not definitive (PENDING,
  OUTCOME_UNKNOWN, or a retryable FAILED such as RateLimited), exactly like awx_spine's
  executor rule. Once a record holds a definitive result (SUCCEEDED, or a non-retryable FAILED) that
  first definitive result wins and later completions return it unchanged.

Records have no TTL: Airwallex deduplicates a request_id for only about 7 days, so this table
is the long-term guard against a second write. Every value is stored as a string
(results as awx_common.transport JSON), so no amount ever becomes a DynamoDB number.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from awx_common.ops import Operation, OperationResult, OperationStatus
from awx_common.transport import dumps, loads
from awx_spine.store import (
    PENDING,
    OperationRecord,
    RequestIdReuse,
    UnknownRequestId,
    body_hash,
)

from . import codec
from .adapter_core import definitive
from .kit_store import DynamoDbKitStore  # also importable from awx_spine_aws.store


def _utcnow() -> datetime:
    """The default clock; local_runner swaps it for the run's clock."""
    return datetime.now(UTC)


class DynamoDbOperationStore:
    def __init__(
        self,
        table_name: str,
        *,
        client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        self._table = table_name
        self._db = client
        self._clock = clock or _utcnow

    def _now(self) -> str:
        return self._clock().astimezone(UTC).isoformat()

    def begin(self, op: Operation) -> OperationRecord:
        now = self._now()
        digest = body_hash(op)
        item = {
            "request_id": {"S": op.request_id},
            "kit": {"S": op.kit},
            "name": {"S": op.name},
            "business_key": {"S": op.business_key},
            "intent_hash": {"S": op.intent_hash},
            "body_hash": {"S": digest},
            "status": {"S": PENDING},
            "final": {"BOOL": False},
            "attempts": {"N": "0"},
            "created_at": {"S": now},
            "updated_at": {"S": now},
        }
        try:
            self._db.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression="attribute_not_exists(request_id)",
            )
        except self._db.exceptions.ConditionalCheckFailedException:
            existing = self.get(op.request_id)
            if existing is None:  # deleted between the put and the get; never expected
                raise
            if existing.body_hash != digest:
                raise RequestIdReuse(
                    op.request_id, stored_hash=existing.body_hash, new_hash=digest
                ) from None
            return existing
        return self._record(item)

    def record_attempt(self, request_id: str) -> OperationRecord:
        """Called immediately before each send. One UpdateItem increments attempts AND sets
        status PENDING (final stays false), so a record that is PENDING with attempts > 0
        always means "a send may have left and never completed", even after an earlier
        retryable FAILED such as a 429 (awx_spine's OperationStore protocol). The
        previous result is kept as history. A definitive record is never reopened."""
        try:
            resp = self._db.update_item(
                TableName=self._table,
                Key={"request_id": {"S": request_id}},
                UpdateExpression=(
                    "ADD attempts :one SET #s = :pending, updated_at = :now, "
                    "attempt_started_at = :now, "
                    "first_attempt_at = if_not_exists(first_attempt_at, :now)"
                ),
                ConditionExpression="attribute_exists(request_id) AND #f = :false",
                ExpressionAttributeNames={"#s": "status", "#f": "final"},
                ExpressionAttributeValues={
                    ":one": {"N": "1"},
                    ":pending": {"S": PENDING},
                    ":false": {"BOOL": False},
                    ":now": {"S": self._now()},
                },
                ReturnValues="ALL_NEW",
            )
        except self._db.exceptions.ConditionalCheckFailedException:
            existing = self.get(request_id)
            if existing is None:
                raise UnknownRequestId(request_id) from None
            raise ValueError(f"{request_id} already has a definitive result") from None
        return self._record(resp["Attributes"])

    def complete(self, request_id: str, result: OperationResult) -> OperationRecord:
        if result.request_id != request_id:
            raise ValueError("result belongs to a different request_id")
        update = "SET #s = :status, #r = :result, #f = :final, updated_at = :now"
        names = {"#s": "status", "#r": "result", "#f": "final"}
        values: dict[str, Any] = {
            ":status": {"S": result.status.value},
            ":result": {"S": dumps(result)},
            ":final": {"BOOL": definitive(result)},
            ":false": {"BOOL": False},
            ":now": {"S": self._now()},
        }
        if result.status is OperationStatus.OUTCOME_UNKNOWN:
            update += ", #m = :true"  # same UpdateItem; nothing ever writes it false
            names["#m"] = "may_have_landed"
            values[":true"] = {"BOOL": True}
        try:
            resp = self._db.update_item(
                TableName=self._table,
                Key={"request_id": {"S": request_id}},
                UpdateExpression=update,
                ConditionExpression="attribute_exists(request_id) AND #f = :false",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except self._db.exceptions.ConditionalCheckFailedException:
            existing = self.get(request_id)
            if existing is None:
                raise UnknownRequestId(request_id) from None
            return existing  # the first definitive result wins
        return self._record(resp["Attributes"])

    def get(self, request_id: str) -> OperationRecord | None:
        resp = self._db.get_item(
            TableName=self._table,
            Key={"request_id": {"S": request_id}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        return None if item is None else self._record(item)

    @staticmethod
    def _record(item: dict[str, Any]) -> OperationRecord:
        result = item.get("result")
        return OperationRecord(
            request_id=item["request_id"]["S"],
            kit=item["kit"]["S"],
            name=item["name"]["S"],
            business_key=item["business_key"]["S"],
            intent_hash=item["intent_hash"]["S"],
            body_hash=item["body_hash"]["S"],
            status=item["status"]["S"],
            attempts=int(item["attempts"]["N"]),
            result=None if result is None else codec.result_from(loads(result["S"])),
            created_at=datetime.fromisoformat(item["created_at"]["S"]),
            updated_at=datetime.fromisoformat(item["updated_at"]["S"]),
            may_have_landed=bool(item.get("may_have_landed", {}).get("BOOL", False)),
            first_attempt_at=_time(item, "first_attempt_at"),
            attempt_started_at=_time(item, "attempt_started_at"),
        )


def _time(item: dict[str, Any], key: str) -> datetime | None:
    value = item.get(key)
    return None if value is None else datetime.fromisoformat(value["S"])


__all__ = ["PENDING", "DynamoDbKitStore", "DynamoDbOperationStore", "body_hash"]
