"""The one contract between kit domain code and the spine.

Kit domain code is pure: it turns an approved intent into Operations and interprets reads.
Only the spine's mutation adapter executes an Operation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .errors import AwxError, error_class
from .kits import KITS

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Airwallex deduplicates a request_id for about 7 days (guide). A rule-9 resend is allowed
# only while the first attempt is younger than this margin, and kits use it for M1. It lives
# here so kits depend on awx_common only; awx_spine.executor re-exports it.
DEDUP_RESEND_MARGIN = timedelta(days=6)


class OperationStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"  # provider accepted the write (2xx); finality is a separate read
    FAILED = "FAILED"  # provider rejected it and nothing was created
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"  # timeout or 5xx after submit; reconcile first


@dataclass(frozen=True)
class Operation:
    """One Airwallex write.

    body is provider-shaped JSON; amounts inside it are Decimal and the spine encodes them as
    exact JSON numbers. The spine sends the body exactly as the kit built it and never adds or
    removes a field, request_id included: a kit puts request_id in the body only for endpoints
    that accept it, and relies on the operation store and StepSpec.reconcile for the rest.
    on_behalf_of is set only by awx_common.tenancy.bind_operation from the server-resolved
    tenant, never from model output. A files.upload operation carries an evidence reference,
    not bytes (awx_common.evidence).
    """

    kit: str
    name: str  # stable op name, e.g. "transfers.create"
    method: str
    path: str  # e.g. "/api/v1/transfers/create"
    body: Mapping[str, Any]
    request_id: str
    business_key: str
    intent_hash: str
    on_behalf_of: str | None = None

    def __post_init__(self) -> None:
        if self.kit not in KITS:
            raise ValueError(f"unknown kit {self.kit!r}")
        if self.method not in _WRITE_METHODS:
            raise ValueError("Operation is for writes; use ReadRequest for GET")
        if not self.path.startswith("/api/v1/"):
            raise ValueError("path must start with /api/v1/")
        if "request_id" in self.body and self.body["request_id"] != self.request_id:
            raise ValueError("body request_id differs from Operation.request_id")
        if len(self.intent_hash) != 64:
            raise ValueError("intent_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "body", dict(self.body))


@dataclass(frozen=True)
class OperationResult:
    request_id: str
    status: OperationStatus
    http_status: int | None = None
    provider_id: str | None = None
    provider_status: str | None = None
    body: Mapping[str, Any] | None = None
    error_type: str | None = None  # an awx_common.errors class name
    error_code: str | None = None  # the provider's code
    error_message: str | None = None  # redacted; never carries credentials

    def error(self) -> AwxError | None:
        if self.error_type is None:
            return None
        return error_class(self.error_type)(
            self.error_message or self.error_type,
            code=self.error_code,
            http_status=self.http_status,
        )


@dataclass(frozen=True)
class ReadRequest:
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    on_behalf_of: str | None = None

    def __post_init__(self) -> None:
        if not self.path.startswith("/api/v1/"):
            raise ValueError("path must start with /api/v1/")


@runtime_checkable
class AirwallexPort(Protocol):
    def execute(self, op: Operation) -> OperationResult:
        """Send one write. Provider rejections come back as FAILED or OUTCOME_UNKNOWN results."""
        ...

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        """GET. Raises an awx_common.errors.AwxError subclass on failure.

        Always a Mapping: a response whose top level is a JSON array (for example
        /api/v1/balances/current) comes back as {"items": [...]}. JSON numbers with a fraction
        or exponent are Decimal, never float.
        """
        ...
