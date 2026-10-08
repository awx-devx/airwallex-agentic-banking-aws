"""What the spine knows about each Airwallex write endpoint the kits use.

Kits name operations with these op names. For each one the spine knows:

- where the provider puts the object id and status in the response;
- whether the provider deduplicates on the body's request_id (`dedupes`). Deny by default: only
  endpoints where the sandbox rejected a replayed request_id are True. Runner rule 9 resends an
  operation after an unknown outcome only when this is True (or the endpoint is `resend_safe`);
- how to read the object back after an unknown outcome or a duplicate-request rejection
  (`readback`), and which body fields the found object must echo before it is accepted as this
  operation's result (`match_fields`).

Evidence for every entry is in FAKE-FIDELITY.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class ReadbackSpec:
    """GET list_path?<query_field>=<value> returns {"items": [...]}.

    The value comes from the operation: its request_id, or for cardholders (whose create
    ignores request_id and deduplicates on email) the body field named by body_field.
    """

    list_path: str
    query_field: str = "request_id"
    body_field: str | None = None  # None: match on the Operation's request_id


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    path: str  # template; {id} matches one path segment
    id_key: str = "id"
    status_key: str | None = "status"
    readback: ReadbackSpec | None = None
    dedupes: bool = False  # sandbox evidence that a replayed request_id is rejected
    resend_safe: bool = False  # a duplicate send is harmless (only files.upload)
    match_fields: tuple[str, ...] = ()  # body fields a read-back object must echo

    def matches(self, path: str) -> bool:
        return bool(_compile(self.path).fullmatch(path))

    @property
    def may_resend_after_unknown(self) -> bool:
        return self.dedupes or self.resend_safe


def _compile(template: str) -> re.Pattern[str]:
    return re.compile(re.escape(template).replace(r"\{id\}", r"[^/]+"))


_E = EndpointSpec
_R = ReadbackSpec
ENDPOINTS: dict[str, EndpointSpec] = {
    e.name: e
    for e in (
        _E(
            "transfers.create",
            "/api/v1/transfers/create",
            readback=_R("/api/v1/transfers"),
            dedupes=True,  # duplicate_request_id
            match_fields=(
                "transfer_amount",
                "transfer_currency",
                "source_amount",  # compared only when the body carries it
                "beneficiary_id",
                "source_currency",
                "transfer_method",
            ),
        ),
        _E("transfers.simulate_transition", "/api/v1/simulation/transfers/{id}/transition"),
        # Sandbox, 6 Oct ([r1-ben-replay-same]): a replayed request_id created a second beneficiary.
        _E("beneficiaries.create", "/api/v1/beneficiaries/create"),
        _E("beneficiaries.validate", "/api/v1/beneficiaries/validate", status_key=None),
        # Quotes ignore request_id: a replay returned a new quote_id.
        _E("fx.quotes.create", "/api/v1/fx/quotes/create", id_key="quote_id", status_key=None),
        _E(
            "fx.conversions.create",
            "/api/v1/fx/conversions/create",
            id_key="conversion_id",
            readback=_R("/api/v1/fx/conversions"),
            dedupes=True,  # invalid_argument "has been used in the past"
            match_fields=("buy_currency", "sell_currency", "buy_amount", "sell_amount", "quote_id"),
        ),
        _E("global_accounts.create", "/api/v1/global_accounts/create"),  # not tested: deny
        _E("deposits.simulate", "/api/v1/simulation/deposit/create"),
        _E("accounts.create", "/api/v1/accounts/create"),
        _E("accounts.update", "/api/v1/accounts/{id}/update"),
        _E("accounts.submit", "/api/v1/accounts/{id}/submit"),
        _E("accounts.simulate_status", "/api/v1/simulation/accounts/{id}/update_status"),
        _E(
            "connected_account_transfers.create",
            "/api/v1/connected_account_transfers/create",
            readback=_R("/api/v1/connected_account_transfers"),
            dedupes=True,  # request_id_duplicate
            # The list echoes reason (sandbox r2: GET /connected_account_transfers keys).
            match_fields=("amount", "currency", "destination", "reason"),
        ),
        _E(
            "charges.create",
            "/api/v1/charges/create",
            readback=_R("/api/v1/charges"),
            dedupes=True,  # request_id_duplicate
            match_fields=("amount", "currency", "source", "reason"),  # list echoes reason
        ),
        _E(
            "issuing.cardholders.create",
            "/api/v1/issuing/cardholders/create",
            id_key="cardholder_id",
            readback=_R("/api/v1/issuing/cardholders", query_field="email", body_field="email"),
            match_fields=("email", "type"),
        ),
        _E(
            "issuing.cards.create",
            "/api/v1/issuing/cards/create",
            id_key="card_id",
            status_key="card_status",
            dedupes=True,  # "already associated with a previous request"
        ),
        _E(
            "issuing.cards.update",
            "/api/v1/issuing/cards/{id}/update",
            id_key="card_id",
            status_key="card_status",
        ),
        # provider_id is the simulation's card_transaction_id. It is NOT the id
        # GET /issuing/transactions/{id} takes; join a listed transaction on
        # card_transaction_data.card_transaction_id (awx_spine.issuing.find_card_transaction).
        _E(
            "issuing.simulate_authorization",
            "/api/v1/simulation/issuing/create",
            id_key="card_transaction_id",
            status_key="process_result",
        ),
        _E(
            "issuing.simulate_capture",
            "/api/v1/simulation/issuing/card_transaction_lifecycles/{id}/capture",
            id_key="card_transaction_id",
            status_key="process_result",
        ),
        _E(
            "issuing.simulate_reverse",
            "/api/v1/simulation/issuing/card_transaction_lifecycles/{id}/reverse",
            id_key="card_transaction_id",
            status_key="process_result",
        ),
        _E("payment_intents.create", "/api/v1/pa/payment_intents/create", dedupes=True),
        _E("payment_intents.confirm", "/api/v1/pa/payment_intents/{id}/confirm"),
        _E("disputes.simulate_create", "/api/v1/simulation/pa/payment_disputes/create"),
        # The API reference requires request_id on accept and challenge, but deduplication is
        # not evidenced (kit 4 payments are not enabled on this sandbox): deny.
        _E("disputes.accept", "/api/v1/pa/payment_disputes/{id}/accept"),
        _E("disputes.challenge", "/api/v1/pa/payment_disputes/{id}/challenge"),
        _E("disputes.simulate_escalate", "/api/v1/simulation/pa/payment_disputes/{id}/escalate"),
        _E("disputes.simulate_resolve", "/api/v1/simulation/pa/payment_disputes/{id}/resolve"),
        _E("platform_reports.create", "/api/v1/platform_reports/create"),
        _E(
            "files.upload",
            "/api/v1/files/upload",
            id_key="file_id",
            status_key=None,
            resend_safe=True,  # a second upload only creates an inert file
        ),
    )
}

_ID_KEYS = (
    "id",
    "conversion_id",
    "card_id",
    "cardholder_id",
    "quote_id",
    "file_id",
    "card_transaction_id",
)


def endpoint_for(name: str, path: str) -> EndpointSpec | None:
    """The spec whose name AND path template match, else the first spec matching the path.

    A known name on a path its template does not match is not trusted.
    """
    spec = ENDPOINTS.get(name)
    if spec is not None and spec.matches(path):
        return spec
    for candidate in ENDPOINTS.values():
        if candidate.matches(path):
            return candidate
    return None


def extract_ids(spec: EndpointSpec | None, body: Any) -> tuple[str | None, str | None]:
    """(provider_id, provider_status) from a 2xx body."""
    if not isinstance(body, Mapping):
        return None, None
    provider_id: Any = body.get(spec.id_key) if spec else None
    if provider_id is None:
        provider_id = next((body[k] for k in _ID_KEYS if body.get(k) is not None), None)
    status: Any = None
    if spec is None or spec.status_key is not None:
        status = body.get(spec.status_key if spec and spec.status_key else "status")
    return (
        str(provider_id) if provider_id is not None else None,
        str(status) if status is not None else None,
    )


def _same(expected: Any, found: Any) -> bool:
    if isinstance(expected, bool) or isinstance(found, bool):
        return expected == found
    if isinstance(expected, int | Decimal) or isinstance(found, int | Decimal):
        try:
            return Decimal(str(expected)) == Decimal(str(found))
        except (InvalidOperation, ValueError):
            return False
    return expected == found


def mismatched_fields(spec: EndpointSpec | None, body: Mapping[str, Any], found: Any) -> list[str]:
    """Fields the operation set that the found object does not echo exactly.

    Only fields in the endpoint's match_fields that the operation's body actually carries are
    compared; a field the object omits counts as a mismatch, so a read-back fails closed.
    """
    if spec is None or not isinstance(found, Mapping):
        return []
    return [
        f
        for f in spec.match_fields
        if f in body and body[f] is not None and (f not in found or not _same(body[f], found[f]))
    ]
