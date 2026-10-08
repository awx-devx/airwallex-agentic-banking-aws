"""Evidence files (dispute evidence, carrier documents) and how they reach Airwallex.

Operation bodies are JSON, so a file never travels inside one. A kit builds a files.upload
Operation whose body carries an EvidenceRef (`{"evidence_ref": {...}, "notes": "poc-..."}`).
The mutation executor resolves the ref through an EvidenceStore, checks size, type and SHA-256
with load_verified, and sends the bytes to the files host through a FileUploadPort. In AWS the
store is the spine's artifacts bucket under evidence/<kit>/<tenant_id>/<evidence_id>, readable
only by the adapter role.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

FILES_UPLOAD_OP = "files.upload"
FILES_UPLOAD_PATH = "/api/v1/files/upload"  # served by files.sandbox.airwallex.com
ALLOWED_CONTENT_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024  # set here: the Airwallex hackathon guide states no limit

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class EvidenceIntegrityError(Exception):
    """The stored bytes do not match the reference, or the reference is not acceptable."""


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    sha256: str
    file_name: str
    content_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not _EVIDENCE_ID.match(self.evidence_id):
            raise EvidenceIntegrityError("evidence_id must match [A-Za-z0-9_-]{1,64}")
        if not _SHA256.match(self.sha256):
            raise EvidenceIntegrityError("sha256 must be 64 lower-case hex characters")
        if self.content_type not in ALLOWED_CONTENT_TYPES:
            raise EvidenceIntegrityError(f"content type {self.content_type!r} is not allowed")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise EvidenceIntegrityError("size_bytes must be an int")
        if not 0 < self.size_bytes <= MAX_EVIDENCE_BYTES:
            raise EvidenceIntegrityError("size_bytes is out of range")
        if not self.file_name or "/" in self.file_name or "\\" in self.file_name:
            raise EvidenceIntegrityError("file_name must be a bare file name")

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> EvidenceRef:
        ref = body.get("evidence_ref")
        if not isinstance(ref, Mapping):
            raise EvidenceIntegrityError("files.upload body needs an evidence_ref object")
        return cls(
            evidence_id=ref["evidence_id"],
            sha256=ref["sha256"],
            file_name=ref["file_name"],
            content_type=ref["content_type"],
            size_bytes=ref["size_bytes"],
        )

    def to_body(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
            "file_name": self.file_name,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
        }


@runtime_checkable
class EvidenceStore(Protocol):
    def get_bytes(self, evidence_id: str) -> bytes:
        """Return the stored bytes. Raises KeyError when the evidence does not exist."""
        ...


@runtime_checkable
class FileUploadPort(Protocol):
    def upload(
        self,
        data: bytes,
        *,
        file_name: str,
        content_type: str,
        notes: str | None = None,
        on_behalf_of: str | None = None,
    ) -> Mapping[str, Any]:
        """Multipart upload to the files host. Raises an awx_common.errors.AwxError on failure."""
        ...


def load_verified(store: EvidenceStore, ref: EvidenceRef) -> bytes:
    """Fetch the bytes for ref and refuse them unless size and SHA-256 match exactly."""
    try:
        data = store.get_bytes(ref.evidence_id)
    except KeyError as exc:
        raise EvidenceIntegrityError(f"evidence {ref.evidence_id} is not in the store") from exc
    if len(data) != ref.size_bytes:
        raise EvidenceIntegrityError("stored evidence size differs from the reference")
    if hashlib.sha256(data).hexdigest() != ref.sha256:
        raise EvidenceIntegrityError("stored evidence SHA-256 differs from the reference")
    return data


class InMemoryEvidenceStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(
        self, evidence_id: str, data: bytes, *, file_name: str, content_type: str
    ) -> EvidenceRef:
        ref = EvidenceRef(
            evidence_id=evidence_id,
            sha256=hashlib.sha256(data).hexdigest(),
            file_name=file_name,
            content_type=content_type,
            size_bytes=len(data),
        )
        self._blobs[evidence_id] = bytes(data)
        return ref

    def get_bytes(self, evidence_id: str) -> bytes:
        return self._blobs[evidence_id]
