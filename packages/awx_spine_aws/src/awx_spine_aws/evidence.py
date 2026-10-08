"""Amazon S3 implementation of awx_common.evidence.EvidenceStore over the artifacts bucket.

Key: evidence/<kit>/<tenant_id>/<evidence_id>. The instance is scoped to one kit and one
tenant at construction, so an EvidenceRef can only name a blob in its own run's prefix.
The bucket is KMS-encrypted with the spine key, blocks public access, enforces TLS and is
versioned. Only the adapter role may read under evidence/ (tests/security checks this); a
kit's own upload Lambda writes it (kit 4's evidence upload handler, awx_k4_dispute.upload).
"""

from __future__ import annotations

from typing import Any

from awx_common.evidence import MAX_EVIDENCE_BYTES, EvidenceIntegrityError
from awx_common.kits import KITS

PREFIX = "evidence"


def evidence_key(kit: str, tenant_id: str, evidence_id: str) -> str:
    return f"{PREFIX}/{kit}/{tenant_id}/{evidence_id}"


class S3EvidenceStore:
    def __init__(self, bucket: str, *, kit: str, tenant_id: str, client: Any | None = None) -> None:
        if kit not in KITS:
            raise ValueError(f"unknown kit {kit!r}")
        if not tenant_id or "/" in tenant_id:
            raise ValueError("tenant_id is required and may not contain '/'")
        if client is None:
            import boto3

            client = boto3.client("s3")
        self._bucket = bucket
        self._kit = kit
        self._tenant = tenant_id
        self._s3 = client

    def get_bytes(self, evidence_id: str) -> bytes:
        if not evidence_id or "/" in evidence_id:
            raise KeyError(evidence_id)
        key = evidence_key(self._kit, self._tenant, evidence_id)
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=key)
        except self._s3.exceptions.NoSuchKey:
            raise KeyError(evidence_id) from None
        if int(resp.get("ContentLength") or 0) > MAX_EVIDENCE_BYTES:
            raise EvidenceIntegrityError("stored evidence is larger than the evidence limit")
        return resp["Body"].read(MAX_EVIDENCE_BYTES + 1)
