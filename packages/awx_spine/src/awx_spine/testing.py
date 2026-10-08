"""Test support: a scripted Transport and operation builders. No network, fake credentials."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from awx_common import Operation, derive_request_id

from awx_spine.credentials import AirwallexCredentials, StaticCredentials
from awx_spine.transport import HttpRequest, HttpResponse, TransportError

CLIENT_ID = "test-client-id-0123456789abcdef"
API_KEY = "test-api-key-ZXCVBNMASDFGHJKL0123456789"
TOKEN = "tok_" + "A1b2C3d4" * 6
TOKEN2 = "tok_" + "Z9y8X7w6" * 6
HASH = "b" * 64


def after_lease() -> datetime:
    """An executor clock past the executor's attempt lease, for tests that resume a crashed attempt
    recorded a moment ago (so it counts as crashed, not as live elsewhere)."""
    return datetime.now(UTC) + timedelta(minutes=10)


def creds() -> StaticCredentials:
    return StaticCredentials(AirwallexCredentials(CLIENT_ID, API_KEY))


def resp(status: int, body: Any) -> HttpResponse:
    return HttpResponse(status, json.dumps(body).encode() if not isinstance(body, bytes) else body)


def login_ok(token: str = TOKEN) -> HttpResponse:
    return resp(201, {"token": token, "expires_at": "2099-01-01T00:00:00+0000"})


@dataclass
class ScriptedTransport:
    """Returns queued responses in order; a queued exception is raised instead."""

    script: list[HttpResponse | Exception | Callable[[HttpRequest], HttpResponse]] = field(
        default_factory=list
    )
    sent: list[HttpRequest] = field(default_factory=list)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        if not self.script:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, HttpResponse):
            return item(request)
        return item

    def api_calls(self) -> list[HttpRequest]:
        return [r for r in self.sent if not r.url.endswith("/authentication/login")]


def timeout() -> TransportError:
    return TransportError("timed out waiting for the provider", sent=True)


def refused() -> TransportError:
    return TransportError("connection failed (ConnectionRefusedError)", sent=False)


def transfer_op(amount: str = "10.00", key: str = "payout:1", **overrides: Any) -> Operation:
    rid = derive_request_id("k3", key)
    fields: dict[str, Any] = {
        "kit": "k3",
        "name": "transfers.create",
        "method": "POST",
        "path": "/api/v1/transfers/create",
        "body": {
            "request_id": rid,
            "beneficiary_id": "ben-1",
            "transfer_amount": Decimal(amount),
            "transfer_currency": "USD",
            "source_currency": "USD",
            "transfer_method": "LOCAL",
            "reference": "poc-test",
            "reason": "professional_business_services",
        },
        "request_id": rid,
        "business_key": key,
        "intent_hash": HASH,
    }
    fields.update(overrides)
    return Operation(**fields)


def make_op(
    kit: str,
    name: str,
    path: str,
    body: dict[str, Any],
    key: str,
    on_behalf_of: str | None = None,
    with_request_id: bool = True,
) -> Operation:
    rid = derive_request_id(kit, key)
    if with_request_id:
        body = {"request_id": rid, **body}
    return Operation(
        kit=kit,
        name=name,
        method="POST",
        path=path,
        body=body,
        request_id=rid,
        business_key=key,
        intent_hash=HASH,
        on_behalf_of=on_behalf_of,
    )
