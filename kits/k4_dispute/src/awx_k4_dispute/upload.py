"""The evidence upload handler: the one Lambda kit 4 declares (functions.FUNCTIONS).

An operator (never the model, never a tool) invokes it directly with one evidence file for one
dispute. It is the only way evidence enters the kit:

- the bytes are typed from their magic number (JPG or PDF; PNG refused), sized and hashed here,
  never taken from the caller (evidence.ingest_evidence);
- the code-owned index entry goes to the kit store (`evidence/<dispute_id>/<evidence_id>`);
- the bytes go to the artifacts bucket under `evidence/k4/<tenant_id>/<store_id>`, the key the
  adapter's S3EvidenceStore reads when the challenge's files.upload runs.

The index is written first, so a retry with the same bytes is idempotent and a retry with
different bytes under the same id is refused before anything is overwritten. A failed put is
fixed by re-invoking with the same file.

`text`, when given, is the file's extracted text (the operator's OCR or PDF text). Policy checks
every quote the model cites against it, and its instruction-like markers are shown to the
approver. Without it, quotes are checked against citations only and the approver is told so.

`ingest_upload` is pure and injected (store, put_object), so tests need no AWS. `handler` is the
Lambda entrypoint. It builds the AWS store and S3 client lazily, from the environment KitStack
sets (AWX_KIT, AWX_ARTIFACTS_BUCKET, AWX_EVIDENCE_PREFIX, AWX_LEDGER_TABLE). The DynamoDB
KitStore implementation lives in awx_spine_aws, which KitStack stages into this function's code;
it is imported only inside `_aws_store`, at invocation, never at import time (see DESIGN.md).
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from awx_common.store import KitStore

from .config import DEFAULT_CONFIG, KIT, DisputeConfig
from .evidence import EVIDENCE_ID_PATTERN, EvidenceError, EvidenceKind, ingest_evidence
from .provider import ID_PATTERN

PLATFORM_TENANT = "platform"  # kits 1-4 run as the platform: k4 reads evidence only there
# A synchronous Lambda invoke payload is capped at 6 MB; base64 adds a third, so about 4.4 MB
# of file fits with the JSON around it. Larger files would be refused by Lambda itself, before
# the handler runs, with no kit message.
MAX_UPLOAD_BYTES = 4_400_000
_DISPUTE = re.compile(ID_PATTERN)
_EVIDENCE = re.compile(EVIDENCE_ID_PATTERN)
_FIELDS = {"tenant_id", "dispute_id", "evidence_id", "file_name", "kind", "content_base64"}
_OPTIONAL = {"text"}
MAX_TEXT_CHARS = 200_000

PutObject = Callable[[str, bytes, str], None]  # (key, data, content_type)


class UploadRejected(ValueError):
    """The upload is not acceptable; nothing was written."""


def _check(event: Mapping[str, Any]) -> None:
    if not isinstance(event, Mapping):
        raise UploadRejected("event must be an object")
    keys = set(event)
    if _FIELDS - keys:
        raise UploadRejected(f"missing {sorted(_FIELDS - keys)}")
    if keys - _FIELDS - _OPTIONAL:
        raise UploadRejected(f"unexpected fields {sorted(keys - _FIELDS - _OPTIONAL)}")
    for name in _FIELDS | (_OPTIONAL & keys):
        if not isinstance(event[name], str):
            raise UploadRejected(f"{name} must be a string")
    if event["tenant_id"] != PLATFORM_TENANT:
        raise UploadRejected(
            "kit 4 runs as the platform: tenant_id must be 'platform' (evidence filed under "
            "any other tenant would never be read)"
        )
    if not _DISPUTE.match(event["dispute_id"]):
        raise UploadRejected("dispute_id is malformed")
    if not _EVIDENCE.match(event["evidence_id"]):
        raise UploadRejected("evidence_id is malformed")
    if len(event.get("text", "")) > MAX_TEXT_CHARS:
        raise UploadRejected(f"text is longer than {MAX_TEXT_CHARS} characters")


def ingest_upload(
    event: Mapping[str, Any],
    *,
    store: KitStore,
    put_object: PutObject,
    prefix: str = f"evidence/{KIT}/",
    config: DisputeConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Index one evidence file and store its bytes. Returns metadata only, never the bytes."""
    _check(event)
    try:
        kind = EvidenceKind(event["kind"])
    except ValueError as exc:
        raise UploadRejected(f"kind must be one of {[k.value for k in EvidenceKind]}") from exc
    try:
        data = base64.b64decode(event["content_base64"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UploadRejected("content_base64 is not valid base64") from exc
    if not data:
        raise UploadRejected("the file is empty")
    limit = min(config.max_evidence_bytes, MAX_UPLOAD_BYTES)
    if len(data) > limit:
        raise UploadRejected(
            f"the file is larger than {limit} bytes, the most a synchronous invoke can carry "
            "base64-encoded; split or compress it"
        )
    try:
        entry = ingest_evidence(
            store,
            event["dispute_id"],
            event["evidence_id"],
            event["file_name"],
            data,
            kind,
            config,
            text=event.get("text") or None,
        )
    except EvidenceError as exc:
        raise UploadRejected(str(exc)) from exc
    key = f"{prefix}{event['tenant_id']}/{entry.store_id}"
    put_object(key, data, entry.file.content_type)
    return {
        "dispute_id": entry.dispute_id,
        "evidence_id": entry.file.evidence_id,
        "store_id": entry.store_id,
        "content_type": entry.file.content_type,
        "sha256": entry.file.sha256,
        "size_bytes": entry.file.size_bytes,
        "kind": entry.file.kind.value,
        "text_indexed": entry.text is not None,
        "instruction_markers": list(entry.markers),
    }


def _aws_store(tenant_id: str) -> KitStore:  # pragma: no cover - AWS only
    from awx_spine_aws.kit_store import DynamoDbKitStore  # staged by KitStack; see docstring

    return DynamoDbKitStore(os.environ["AWX_LEDGER_TABLE"], kit=KIT, tenant_id=tenant_id)


def _aws_put(bucket: str) -> PutObject:  # pragma: no cover - AWS only
    import boto3

    s3 = boto3.client("s3")

    def put(key: str, data: bytes, content_type: str) -> None:
        s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

    return put


def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:  # pragma: no cover
    """Lambda entrypoint. Errors come back as {"error": ...} with nothing written."""
    if os.environ.get("AWX_KIT") != KIT:
        return {"error": "this function belongs to kit k4"}
    try:
        _check(event)
        return ingest_upload(
            event,
            store=_aws_store(event["tenant_id"]),
            put_object=_aws_put(os.environ["AWX_ARTIFACTS_BUCKET"]),
            prefix=os.environ["AWX_EVIDENCE_PREFIX"],
        )
    except UploadRejected as exc:
        return {"error": str(exc)}
