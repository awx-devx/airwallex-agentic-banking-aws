"""The client and FakeAirwallex against observed sandbox behaviour.

Redirects, header guards, JSON constants, endpoint-specific insufficient-funds codes and write
faults, transfer simulations, tenant isolation, card limits, the join from a card simulation to
its issuing transaction, and the sandbox transfer driver `make_transfer_advance`.
"""

from __future__ import annotations

import io
import urllib.request
from datetime import timedelta
from decimal import Decimal
from email.message import Message

import pytest
from awx_common import Money, OperationStatus, ReadRequest
from awx_common.errors import NotFound, OutcomeUnknown, TransientServerError, ValidationFailed
from awx_common.evidence import FILES_UPLOAD_OP
from awx_spine import FakeAirwallex, Fault
from awx_spine.client import AirwallexClient
from awx_spine.fake import EPOCH, INSUFFICIENT_FUNDS_OPS
from awx_spine.issuing import find_card_transaction
from awx_spine.normalise import result_from_response
from awx_spine.sandbox import make_transfer_advance
from awx_spine.testing import (
    API_KEY,
    CLIENT_ID,
    ScriptedTransport,
    creds,
    login_ok,
    make_op,
    refused,
    resp,
    transfer_op,
)
from awx_spine.transport import HttpRequest, UrllibTransport, build_opener
from awx_spine.wire import decode_json

OK = OperationStatus.SUCCEEDED
USD = "USD"


def usd(x: str) -> Money:
    return Money.of(x, USD)


# ---- redirects are never followed -------------------------------------------------------------


class _Redirecting(urllib.request.BaseHandler):
    """Answers every https request with a 302 to production and counts requests.

    handler_order puts it ahead of urllib's real HTTPSHandler, so nothing leaves the process.
    """

    handler_order = 100

    def __init__(self) -> None:
        self.seen: list[urllib.request.Request] = []

    def https_open(self, req: urllib.request.Request):  # type: ignore[no-untyped-def]
        self.seen.append(req)
        headers = Message()
        headers["Location"] = "https://api.airwallex.com/api/v1/transfers/create"
        reply = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
        reply.msg = "Found"
        return reply


import urllib.response  # noqa: E402


def test_transport_returns_redirect_instead_of_following_it():
    handler = _Redirecting()
    transport = UrllibTransport(build_opener(handler))
    got = transport.send(
        HttpRequest(
            "POST",
            "https://api.sandbox.airwallex.com/api/v1/authentication/login",
            {"x-client-id": CLIENT_ID, "x-api-key": API_KEY},
            b"",
        )
    )
    assert got.status == 302
    assert len(handler.seen) == 1
    assert handler.seen[0].full_url.startswith("https://api.sandbox.airwallex.com/")


def test_redirect_is_a_definitive_failure_on_writes_reads_and_login():
    t = ScriptedTransport([login_ok(), resp(302, b"")])
    c = AirwallexClient(creds(), transport=t, sleep=lambda s: None)
    result = c.execute(transfer_op())
    assert result.status is OperationStatus.FAILED
    assert result.error_code == "redirect_refused"
    assert not result.error().retryable
    assert len(t.api_calls()) == 1
    t = ScriptedTransport([login_ok(), resp(301, b"")])
    with pytest.raises(Exception, match="redirect refused"):
        AirwallexClient(creds(), transport=t, sleep=lambda s: None).read(
            ReadRequest("/api/v1/balances/current")
        )
    assert len(t.api_calls()) == 1
    t = ScriptedTransport([resp(307, b"")])
    login = AirwallexClient(creds(), transport=t).execute(transfer_op())
    assert login.error_code == "redirect_refused"
    assert len(t.sent) == 1


# ---- header guards, JSON constants and upload failures ----------------------------------------


def test_trailing_slash_cannot_skip_the_platform_header_guard():
    op = make_op(
        "k5",
        "charges.create",
        "/api/v1/charges/create/",
        {
            "amount": Decimal("1"),
            "currency": USD,
            "source": "acct_a",
            "reason": "taxes",
            "reference": "poc",
        },
        "slash",
        on_behalf_of="acct_a",
    )
    with pytest.raises(ValueError, match="plain"):
        AirwallexClient(creds(), transport=ScriptedTransport()).execute(op)


def test_non_finite_json_constants_are_refused():
    for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'):
        with pytest.raises(ValueError, match="non-finite"):
            decode_json(text)


def test_cardholder_email_collision_is_a_duplicate():
    op = make_op(
        "k2",
        "issuing.cardholders.create",
        "/api/v1/issuing/cardholders/create",
        {"email": "x@example.com"},
        "ch",
        with_request_id=False,
    )
    body = {
        "code": "bad_request",
        "message": "The given email or mobile number is already associated with a cardholder.",
    }
    assert result_from_response(op, 400, body).error_type == "DuplicateRequest"
    other = make_op("k2", "beneficiaries.create", "/api/v1/beneficiaries/create", {}, "b")
    assert result_from_response(other, 400, body).error_type == "ValidationFailed"


def test_card_simulation_without_header_is_outcome_unknown():
    op = make_op(
        "k5",
        "issuing.simulate_authorization",
        "/api/v1/simulation/issuing/create",
        {"card_id": "c"},
        "sim",
        with_request_id=False,
    )
    got = result_from_response(
        op, 400, {"code": "bad_request", "message": "Transaction could not be created"}
    )
    assert got.status is OperationStatus.OUTCOME_UNKNOWN


def test_upload_not_sent_is_transient_not_unknown():
    t = ScriptedTransport([login_ok(), refused()])
    with pytest.raises(TransientServerError):
        AirwallexClient(creds(), transport=t).upload(
            b"x", file_name="e.jpg", content_type="image/jpeg"
        )


# ---- the fake refuses on_behalf_of on body-bound endpoints like the client --------------------


@pytest.mark.parametrize(
    ("name", "path", "field"),
    [
        ("charges.create", "/api/v1/charges/create", "source"),
        (
            "connected_account_transfers.create",
            "/api/v1/connected_account_transfers/create",
            "destination",
        ),
    ],
)
def test_fake_and_client_agree_on_body_bound_header(name, path, field):
    fake = FakeAirwallex(balances={USD: usd("100")})
    a = fake.add_connected_account("poc-a", {USD: usd("50")})
    b = fake.add_connected_account("poc-b", {})
    op = make_op(
        "k8",
        name,
        path,
        {
            "amount": Decimal("1.00"),
            "currency": USD,
            field: b,
            "reason": "goods_purchased",
            "reference": "poc",
        },
        f"guard:{name}",
        on_behalf_of=a,
    )
    from_fake = fake.execute(op)
    from_client = AirwallexClient(creds(), transport=ScriptedTransport()).execute(op)
    assert from_fake == from_client
    assert from_fake.error_type == "TenantScopeViolation"
    assert fake.balance(USD, a) == usd("50")
    assert fake.requests == []


# ---- endpoint-specific insufficient funds and upload faults -----------------------------------


def test_insufficient_funds_fault_only_where_the_sandbox_has_one():
    fake = FakeAirwallex()
    for name in ("beneficiaries.create", "fx.quotes.create", "deposits.simulate", FILES_UPLOAD_OP):
        with pytest.raises(ValueError, match="no insufficient-funds"):
            fake.inject(name, Fault.INSUFFICIENT_FUNDS)
    assert "issuing.simulate_authorization" in INSUFFICIENT_FUNDS_OPS


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("transfers.create", "balance_insufficient"),
        ("charges.create", "insufficient_fund"),
        ("connected_account_transfers.create", "insufficient_fund"),
        ("fx.conversions.create", "insufficient_funds"),
    ],
)
def test_injected_insufficient_funds_codes(name, code):
    fake = FakeAirwallex()
    fake.inject(name, Fault.INSUFFICIENT_FUNDS)
    got = fake.execute(make_op("k1", name, "/api/v1/x/create", {}, f"nsf:{name}"))
    assert (got.error_type, got.error_code) == ("InsufficientFunds", code)


def _card(fake: FakeAirwallex, obo: str | None = None, limits=None) -> str:
    ch = fake.execute(
        make_op(
            "k2",
            "issuing.cardholders.create",
            "/api/v1/issuing/cardholders/create",
            {"type": "DELEGATE", "email": f"poc-{obo}@example.com"},
            f"ch:{obo}",
            on_behalf_of=obo,
            with_request_id=False,
        )
    )
    card = fake.execute(
        make_op(
            "k2",
            "issuing.cards.create",
            "/api/v1/issuing/cards/create",
            {
                "cardholder_id": ch.provider_id,
                "created_by": "poc",
                "form_factor": "VIRTUAL",
                "is_personalized": False,
                "program": {"purpose": "COMMERCIAL"},
                "authorization_controls": {
                    "allowed_transaction_count": "MULTIPLE",
                    "transaction_limits": {"currency": USD, "limits": limits or []},
                },
            },
            f"card:{obo}",
            on_behalf_of=obo,
        )
    )
    return card.provider_id


def test_injected_insufficient_funds_on_card_authorisation_is_a_decline():
    fake = FakeAirwallex(balances={USD: usd("100")})
    card = _card(fake)
    fake.inject("issuing.simulate_authorization", Fault.INSUFFICIENT_FUNDS)
    got = fake.execute(
        make_op(
            "k2",
            "issuing.simulate_authorization",
            "/api/v1/simulation/issuing/create",
            {"card_id": card, "transaction_amount": Decimal("1.00"), "transaction_currency": USD},
            "a",
            with_request_id=False,
        )
    )
    assert got.status is OK
    assert got.http_status == 201
    assert got.provider_status == "DECLINED"
    assert got.body["failure_reason"] == "INSUFFICIENT_FUNDS"
    bal = fake.read(ReadRequest("/api/v1/balances/current"))["items"][0]
    assert bal["reserved_amount"] == 0


def test_upload_write_faults():
    fake = FakeAirwallex()
    fake.inject(FILES_UPLOAD_OP, Fault.DUPLICATE_DELIVERY)
    second = fake.upload(b"x", file_name="e.jpg", content_type="image/jpeg")
    files = fake.objects("files")
    assert len(files) == 2
    assert second["file_id"] == files[1]["file_id"]
    fake.inject(FILES_UPLOAD_OP, Fault.SERVER_ERROR, applied=True)
    with pytest.raises(Exception, match="x"):
        fake.upload(b"y", file_name="e.jpg", content_type="image/jpeg")
    assert len(fake.objects("files")) == 3
    fake.inject(FILES_UPLOAD_OP, Fault.SERVER_ERROR)
    with pytest.raises(OutcomeUnknown):  # a 5xx on a write, as from the client
        fake.upload(b"z", file_name="e.jpg", content_type="image/jpeg")
    assert len(fake.objects("files")) == 3


def test_server_error_applied_moves_the_transfer_like_the_sandbox():
    fake = FakeAirwallex(balances={USD: usd("100")})
    ben = fake.execute(
        make_op(
            "k3", "beneficiaries.create", "/api/v1/beneficiaries/create", {"beneficiary": {}}, "b"
        )
    ).provider_id
    tid = fake.execute(
        make_op(
            "k3",
            "transfers.create",
            "/api/v1/transfers/create",
            {
                "beneficiary_id": ben,
                "transfer_amount": Decimal("1.00"),
                "transfer_currency": USD,
                "transfer_method": "LOCAL",
                "reference": "poc",
            },
            "t",
        )
    ).provider_id
    fake.inject("transfers.simulate_transition", Fault.SERVER_ERROR, applied=True)
    got = fake.execute(
        make_op(
            "k3",
            "transfers.simulate_transition",
            f"/api/v1/simulation/transfers/{tid}/transition",
            {"next_status": "SENT"},
            "s",
            with_request_id=False,
        )
    )
    assert got.status is OperationStatus.OUTCOME_UNKNOWN
    assert fake.read(ReadRequest(f"/api/v1/transfers/{tid}"))["status"] == "SENT"
    with pytest.raises(ValueError, match="applied"):
        fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, applied=True)


def test_transfers_never_move_past_processing_without_a_simulation_call():
    fake = FakeAirwallex(balances={USD: usd("100")})
    ben = fake.execute(
        make_op(
            "k3", "beneficiaries.create", "/api/v1/beneficiaries/create", {"beneficiary": {}}, "b"
        )
    ).provider_id
    tid = fake.execute(
        make_op(
            "k3",
            "transfers.create",
            "/api/v1/transfers/create",
            {
                "beneficiary_id": ben,
                "transfer_amount": Decimal("1.00"),
                "transfer_currency": USD,
                "transfer_method": "LOCAL",
                "reference": "poc",
            },
            "t",
        )
    ).provider_id
    fake.advance(10**7)
    assert fake.read(ReadRequest(f"/api/v1/transfers/{tid}"))["status"] == "PROCESSING"


def test_beneficiary_replay_creates_a_second_beneficiary_like_the_sandbox():
    fake = FakeAirwallex()
    op = make_op(
        "k3", "beneficiaries.create", "/api/v1/beneficiaries/create", {"beneficiary": {}}, "b"
    )
    first, second = fake.execute(op), fake.execute(op)
    assert first.status is OK
    assert second.status is OK
    assert first.provider_id != second.provider_id


# ---- tenant isolation, determinism, quotes and card limits ------------------------------------


def test_cross_tenant_card_authorisation_writes_nothing():
    fake = FakeAirwallex()
    a = fake.add_connected_account("poc-a", {USD: usd("50")})
    b = fake.add_connected_account("poc-b", {USD: usd("50")})
    card_b = _card(fake, obo=b)
    with pytest.raises(ValidationFailed):
        fake.simulate(
            "card_authorization", card_id=card_b, amount=Decimal("30"), currency=USD, on_behalf_of=a
        )
    assert fake.objects("card_transactions") == []
    assert fake.balance(USD, b) == usd("50")


def test_tenant_cannot_act_on_another_tenants_dispute_or_account():
    fake = FakeAirwallex(online_payments_enabled=True)
    a = fake.add_connected_account("poc-a", {})
    b = fake.add_connected_account("poc-b", {})
    with pytest.raises(NotFound):
        fake.read(ReadRequest(f"/api/v1/accounts/{b}", on_behalf_of=a))
    assert fake.read(ReadRequest(f"/api/v1/accounts/{a}", on_behalf_of=a))["id"] == a
    pi = fake.execute(
        make_op(
            "k4",
            "payment_intents.create",
            "/api/v1/pa/payment_intents/create",
            {"amount": Decimal("5"), "currency": USD, "merchant_order_id": "poc"},
            "pi",
            on_behalf_of=b,
        )
    )
    fake.execute(
        make_op(
            "k4",
            "payment_intents.confirm",
            f"/api/v1/pa/payment_intents/{pi.provider_id}/confirm",
            {"payment_method": {"card": {"number": "4035501000000008"}}},
            "c",
            on_behalf_of=b,
        )
    )
    d = fake.simulate(
        "dispute",
        payment_intent_id=pi.provider_id,
        stage="RFI",
        reason_code="10.4",
        due_at="2026-12-01T00:00:00+0000",
    )
    accept = make_op(
        "k4",
        "disputes.accept",
        f"/api/v1/pa/payment_disputes/{d['id']}/accept",
        {"reason": "OTHERS"},
        "acc",
        on_behalf_of=a,
    )
    assert fake.execute(accept).error_type == "NotFound"
    ok = make_op("k4", "disputes.accept", accept.path, {"reason": "OTHERS"}, "acc2", on_behalf_of=b)
    assert fake.execute(ok).provider_status == "ACCEPTED"


def test_fake_is_deterministic():
    def run() -> object:
        fake = FakeAirwallex()
        return fake.execute(
            make_op(
                "k1",
                "fx.conversions.create",
                "/api/v1/fx/conversions/create",
                {"buy_currency": "EUR", "sell_currency": USD, "buy_amount": Decimal("1")},
                "fx",
            )
        )

    assert run() == run()


def test_quote_without_currency_is_a_validation_failure():
    got = FakeAirwallex().execute(
        make_op(
            "k1",
            "fx.quotes.create",
            "/api/v1/fx/quotes/create",
            {"sell_currency": USD, "validity": "MIN_1", "buy_amount": Decimal("1")},
            "q",
        )
    )
    assert got.error_type == "ValidationFailed"
    assert got.error_code == "field_required"


def test_cumulative_limits_convert_every_currency_to_the_limit_currency():
    fake = FakeAirwallex(balances={USD: usd("1000"), "EUR": Money.of("1000", "EUR")})
    card = _card(fake, limits=[{"amount": Decimal("100"), "interval": "ALL_TIME"}])
    eur = fake.simulate("card_authorization", card_id=card, amount=Decimal("80"), currency="EUR")
    assert eur["process_result"] == "APPROVED"  # 80 EUR is about 89.76 USD
    last = fake.simulate("card_authorization", card_id=card, amount=Decimal("15"), currency=USD)
    assert last["failure_reason"] == "LIMIT_EXCEEDED"


def test_declined_single_phase_is_clearing_like_the_sandbox():
    fake = FakeAirwallex(balances={USD: usd("10")})
    card = _card(fake, limits=[{"amount": Decimal("0.01"), "interval": "PER_TRANSACTION"}])
    ok = fake.simulate(
        "card_authorization", card_id=card, amount=Decimal("0.01"), currency=USD, single_phase=True
    )
    no = fake.simulate(
        "card_authorization", card_id=card, amount=Decimal("0.02"), currency=USD, single_phase=True
    )
    assert (ok["type"], ok["process_result"]) == ("CLEARING", "APPROVED")
    assert (no["type"], no["failure_reason"]) == ("CLEARING", "LIMIT_EXCEEDED")
    bal = fake.read(ReadRequest("/api/v1/balances/current"))["items"][0]
    assert bal["reserved_amount"] == 0


# ---- joining a card simulation to its issuing transaction -------------------------------------


def test_find_card_transaction_joins_on_card_transaction_data():
    fake = FakeAirwallex(balances={USD: usd("10")})
    card = _card(fake)
    sim = fake.simulate("card_authorization", card_id=card, amount=Decimal("1"), currency=USD)
    item = find_card_transaction(fake, card_id=card, card_transaction_id=sim["card_transaction_id"])
    assert item is not None
    assert item["transaction_id"] != sim["card_transaction_id"]
    got = fake.read(ReadRequest(f"/api/v1/issuing/transactions/{item['transaction_id']}"))
    assert got["transaction_id"] == item["transaction_id"]
    with pytest.raises(NotFound):
        fake.read(ReadRequest(f"/api/v1/issuing/transactions/{sim['card_transaction_id']}"))
    assert find_card_transaction(fake, card_id=card, card_transaction_id="nope") is None


# ---- make_transfer_advance --------------------------------------------------------------------


def _transfer(fake: FakeAirwallex, reference: str, obo: str | None = None):
    ben = fake.execute(
        make_op(
            "k3",
            "beneficiaries.create",
            "/api/v1/beneficiaries/create",
            {"beneficiary": {}},
            f"b:{reference}",
            on_behalf_of=obo,
        )
    )
    op = make_op(
        "k3",
        "transfers.create",
        "/api/v1/transfers/create",
        {
            "beneficiary_id": ben.provider_id,
            "transfer_amount": Decimal("1.00"),
            "transfer_currency": USD,
            "transfer_method": "LOCAL",
            "reference": reference,
        },
        f"t:{reference}",
        on_behalf_of=obo,
    )
    return op, fake.execute(op)


def _now(fake: FakeAirwallex):
    return EPOCH + timedelta(seconds=fake._now)


def test_transfer_advance_drives_sent_then_paid_after_hold():
    fake = FakeAirwallex(balances={USD: usd("10")})
    op, result = _transfer(fake, "poc-k3-pay")
    advance = make_transfer_advance("k3", hold_s=30)
    read = lambda: fake.read(ReadRequest(f"/api/v1/transfers/{result.provider_id}"))  # noqa: E731
    step = advance(op, result, read(), _now(fake), 1)
    assert step is not None
    assert step.body == {"next_status": "SENT"}
    assert step.path == f"/api/v1/simulation/transfers/{result.provider_id}/transition"
    assert step.request_id == advance(op, result, read(), _now(fake), 1).request_id
    assert step.request_id != advance(op, result, read(), _now(fake), 2).request_id
    assert fake.execute(step).provider_status == "SENT"
    fake.advance(10)
    assert advance(op, result, read(), _now(fake), 2) is None  # held
    fake.advance(25)
    paid = advance(op, result, read(), _now(fake), 3)
    assert paid is not None
    assert paid.body == {"next_status": "PAID"}
    assert paid.request_id != step.request_id
    assert fake.execute(paid).provider_status == "PAID"
    assert advance(op, result, read(), _now(fake), 4) is None


def test_transfer_advance_stops_at_sent_with_the_marker_and_copies_the_header():
    fake = FakeAirwallex()
    acct = fake.add_connected_account("poc-a", {USD: usd("10")})
    op, result = _transfer(fake, "poc-k3-orig-nosettle", obo=acct)
    advance = make_transfer_advance("k3", hold_s=0)
    body = fake.read(ReadRequest(f"/api/v1/transfers/{result.provider_id}", on_behalf_of=acct))
    step = advance(op, result, body, _now(fake), 1)
    assert step is not None
    assert step.on_behalf_of == acct
    assert fake.execute(step).provider_status == "SENT"
    fake.advance(3600)
    body = fake.read(ReadRequest(f"/api/v1/transfers/{result.provider_id}", on_behalf_of=acct))
    assert advance(op, result, body, _now(fake), 2) is None
    with pytest.raises(ValueError, match="aware"):
        advance(op, result, body, _now(fake).replace(tzinfo=None), 3)
