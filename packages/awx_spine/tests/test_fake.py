from decimal import Decimal

import pytest
from awx_common import Money, OperationStatus, ReadRequest
from awx_common.errors import (
    InsufficientFunds,
    NotFound,
    OutcomeUnknown,
    RateLimited,
    StateConflict,
    TransientServerError,
    Unauthorized,
)
from awx_common.evidence import FILES_UPLOAD_OP, FILES_UPLOAD_PATH, InMemoryEvidenceStore
from awx_spine import FakeAirwallex, Fault, InMemoryOperationStore, MutationExecutor
from awx_spine.testing import make_op
from hypothesis import given, settings
from hypothesis import strategies as st

OK = OperationStatus.SUCCEEDED
USD = "USD"


def usd(x: str) -> Money:
    return Money.of(x, USD)


def setup(balance: str = "1000.00", **kw):
    fake = FakeAirwallex(balances={USD: usd(balance), "EUR": Money.of("1000.00", "EUR")}, **kw)
    return fake


def ben_op(key="ben:1", obo=None):
    return make_op(
        "k3",
        "beneficiaries.create",
        "/api/v1/beneficiaries/create",
        {
            "nickname": f"poc-k3-{key}",
            "transfer_methods": ["LOCAL"],
            "beneficiary": {
                "type": "BANK_ACCOUNT",
                "entity_type": "COMPANY",
                "address": {"country_code": "US", "state": "US-NY"},
                "bank_details": {
                    "bank_account_category": "Checking",
                    "account_routing_type1": "aba",
                },
            },
        },
        key,
        on_behalf_of=obo,
    )


def beneficiary(fake, obo=None, key="ben:1") -> str:
    r = fake.execute(ben_op(key, obo))
    assert r.status is OK, r
    return r.provider_id


def transfer(ben, amount="10.00", key="tr:1", obo=None, method="LOCAL", ccy=USD):
    return make_op(
        "k3",
        "transfers.create",
        "/api/v1/transfers/create",
        {
            "beneficiary_id": ben,
            "transfer_amount": Decimal(amount),
            "transfer_currency": ccy,
            "source_currency": ccy,
            "transfer_method": method,
            "reference": "poc-k3",
            "reason": "goods_purchased",
        },
        key,
        on_behalf_of=obo,
    )


def transition(tid, body, obo=None, key="sim"):
    return make_op(
        "k3",
        "transfers.simulate_transition",
        f"/api/v1/simulation/transfers/{tid}/transition",
        body,
        key,
        on_behalf_of=obo,
        with_request_id=False,
    )


# ---- transfers ----------------------------------------------------------------------------------


def test_transfer_status_machine_and_failure_ends_cancelled():
    fake = setup()
    ben = beneficiary(fake)
    r = fake.execute(transfer(ben))
    assert r.status is OK
    assert r.provider_status == "SCHEDULED"
    tid = r.provider_id
    assert fake.balance(USD) == usd("990.00")
    fake.advance(1)
    get = lambda: fake.read(ReadRequest(f"/api/v1/transfers/{tid}"))  # noqa: E731
    assert get()["status"] == "PROCESSING"
    # FAILED straight from PROCESSING is refused with a 500 and nothing changes (sandbox).
    bad = fake.execute(
        transition(tid, {"next_status": "FAILED", "failure_type": "BENEFICIARY_BANK_RETURNED"})
    )
    assert bad.status is OperationStatus.OUTCOME_UNKNOWN
    assert get()["status"] == "PROCESSING"
    assert fake.execute(transition(tid, {"next_status": "SENT"})).provider_status == "SENT"
    reply = fake.execute(
        transition(tid, {"next_status": "FAILED", "failure_type": "BENEFICIARY_BANK_RETURNED"})
    )
    assert reply.body["status"] == "FAILED"
    assert reply.body["failure_reason"] == "90802: Beneficiary bank returned"
    assert reply.body["failure_type"] == "INCORRECT_ROUTING"
    body = get()
    assert body["status"] == "CANCELLED"
    assert "failure_type" not in body
    assert "failure_reason" not in body
    assert body["failure"] == {
        "code": "90802",
        "details": {"type": "INCORRECT_ROUTING"},
        "message": "Beneficiary bank returned",
    }
    assert fake.balance(USD) == usd("1000.00")  # refunded
    # CANCELLED is terminal: PAID afterwards is a 500
    assert (
        fake.execute(transition(tid, {"next_status": "PAID"})).status
        is OperationStatus.OUTCOME_UNKNOWN
    )


def test_transfer_paid_and_bare_cancel():
    fake = setup()
    ben = beneficiary(fake)
    t1 = fake.execute(transfer(ben, key="a")).provider_id
    t2 = fake.execute(transfer(ben, key="b")).provider_id
    fake.execute(transition(t1, {"next_status": "SENT"}))
    assert fake.execute(transition(t1, {"next_status": "PAID"})).provider_status == "PAID"
    fake.execute(transition(t2, {"next_status": "CANCELLED"}))
    got = fake.read(ReadRequest(f"/api/v1/transfers/{t2}"))
    assert got["status"] == "CANCELLED"
    assert got["failure"] == {"details": {"type": "PAYMENT_RECALLED"}, "message": "simulation"}
    sent_back = fake.execute(transition(t1, {"next_status": "SENT"}))
    assert sent_back.status is OperationStatus.OUTCOME_UNKNOWN


def test_transition_validation():
    fake = setup()
    tid = fake.execute(transfer(beneficiary(fake))).provider_id
    fake.execute(transition(tid, {"next_status": "SENT"}))
    bad = fake.execute(transition(tid, {"next_status": "FAILED", "failure_type": "BOGUS"}))
    assert bad.error_type == "ValidationFailed"
    none = fake.execute(transition(tid, {"next_status": "PAID", "failure_type": None}))
    assert none.error_type == "ValidationFailed"
    other = fake.execute(transition(tid, {"next_status": "FAILED"}))
    assert other.body["failure_reason"] == "99902: Other"


def test_transfer_validation_and_swift_fee():
    fake = setup()
    ben = beneficiary(fake)
    assert fake.execute(transfer(ben, amount="10.001")).error_type == "ValidationFailed"
    swift = fake.execute(transfer(ben, amount="100.00", key="sw", method="SWIFT", ccy="EUR"))
    assert swift.body["fee_amount"] == Decimal("6.21")
    assert swift.body["amount_payer_pays"] == Decimal("106.21")
    assert fake.balance("EUR") == Money.of("893.79", "EUR")


def test_transfer_reason_is_not_validated():
    fake = setup()
    op = transfer(beneficiary(fake))
    op = make_op(
        "k3",
        op.name,
        op.path,
        {
            **{k: v for k, v in op.body.items() if k != "request_id"},
            "reason": "payment_to_supplier",
        },
        "reason",
    )
    assert fake.execute(op).status is OK


def test_duplicate_request_id_on_transfers_names_the_original():
    fake = setup()
    op = transfer(beneficiary(fake))
    first = fake.execute(op)
    again = fake.execute(op)
    assert again.error_type == "DuplicateRequest"
    assert again.error_code == "duplicate_request_id"
    assert again.provider_id == first.provider_id
    listed = fake.read(ReadRequest("/api/v1/transfers", {"request_id": op.request_id}))
    assert [t["id"] for t in listed["items"]] == [first.provider_id]
    assert fake.read(ReadRequest("/api/v1/transfers", {"request_id": "x" * 36}))["items"] == []


# ---- insufficient funds, per endpoint ------------------------------------------------------------


def test_real_insufficient_funds_codes_per_endpoint():
    fake = setup(balance="5.00")
    acct = fake.add_connected_account("poc-k5-a", {USD: usd("1.00")})
    ben = beneficiary(fake)
    tr = fake.execute(transfer(ben, amount="6.00"))
    assert (tr.error_type, tr.error_code) == ("InsufficientFunds", "balance_insufficient")
    charge = fake.execute(
        make_op(
            "k5",
            "charges.create",
            "/api/v1/charges/create",
            {
                "amount": Decimal("2.00"),
                "currency": USD,
                "source": acct,
                "reason": "professional_business_services",
                "reference": "poc-k5-fee",
            },
            "ch",
        )
    )
    assert (charge.error_type, charge.error_code) == ("InsufficientFunds", "insufficient_fund")
    cat = fake.execute(
        make_op(
            "k5",
            "connected_account_transfers.create",
            "/api/v1/connected_account_transfers/create",
            {
                "amount": Decimal("6.00"),
                "currency": USD,
                "destination": acct,
                "reason": "wages_salary",
                "reference": "poc-k5-bridge",
            },
            "cat",
        )
    )
    assert (cat.error_type, cat.error_code) == ("InsufficientFunds", "insufficient_fund")
    fx = fake.execute(
        make_op(
            "k1",
            "fx.conversions.create",
            "/api/v1/fx/conversions/create",
            {"buy_currency": "EUR", "sell_currency": USD, "buy_amount": Decimal("100.00")},
            "fx",
        )
    )
    assert (fx.error_type, fx.error_code) == ("InsufficientFunds", "insufficient_funds")
    assert fake.objects("transfers") == []
    assert fake.objects("charges") == []


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("transfers.create", "balance_insufficient"),
        ("charges.create", "insufficient_fund"),
        ("fx.conversions.create", "insufficient_funds"),
    ],
)
def test_injected_insufficient_funds_matches_endpoint_code(name, code):
    fake = setup()
    fake.inject(name, Fault.INSUFFICIENT_FUNDS)
    op = make_op("k1", name, "/api/v1/x/create", {}, f"nsf:{name}")
    result = fake.execute(op)
    assert result.error_type == "InsufficientFunds"
    assert result.error_code == code


# ---- faults through the executor -------------------------------------------------------------


def run(fake, op, **kw):
    return MutationExecutor(fake, InMemoryOperationStore(), sleep=lambda s: None, **kw).run(op)


def test_timeout_after_submit_creates_exactly_one_transfer():
    fake = setup()
    op = transfer(beneficiary(fake))
    fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT)
    result = run(fake, op)
    assert result.status is OK
    assert len(fake.objects("transfers")) == 1
    assert fake.balance(USD) == usd("990.00")


def test_timeout_plus_stale_read_still_one_transfer():
    fake = setup()
    op = transfer(beneficiary(fake))
    fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT)
    fake.inject("transfers.list", Fault.STALE_READ)
    result = run(fake, op)  # stale read misses, resend is deduplicated, read-back finds it
    assert result.status is OK
    assert len(fake.objects("transfers")) == 1


def test_duplicate_delivery_resolves_to_the_single_object():
    fake = setup()
    acct = fake.add_connected_account("poc-k8-s", {})
    op = make_op(
        "k8",
        "connected_account_transfers.create",
        "/api/v1/connected_account_transfers/create",
        {
            "amount": Decimal("10.00"),
            "currency": USD,
            "destination": acct,
            "reason": "goods_purchased",
            "reference": "poc-k8-payout",
        },
        "payout",
    )
    fake.inject(op.name, Fault.DUPLICATE_DELIVERY)
    result = run(fake, op)
    assert result.status is OK
    assert result.provider_status == "NEW"
    assert len(fake.objects("connected_account_transfers")) == 1
    assert fake.balance(USD, acct) == usd("10.00")


def test_rate_limit_and_server_error_on_writes_are_retried_safely():
    fake = setup()
    op = transfer(beneficiary(fake))
    fake.inject("transfers.create", Fault.RATE_LIMIT)
    fake.inject("transfers.create", Fault.SERVER_ERROR)
    assert run(fake, op).status is OK
    assert len(fake.objects("transfers")) == 1


def test_read_faults_raise_normalised_errors():
    fake = setup()
    fake.inject("balances.current", Fault.RATE_LIMIT)
    fake.inject("balances.current", Fault.SERVER_ERROR)
    with pytest.raises(RateLimited):
        fake.read(ReadRequest("/api/v1/balances/current"))
    with pytest.raises(TransientServerError):
        fake.read(ReadRequest("/api/v1/balances/current"))
    items = fake.read(ReadRequest("/api/v1/balances/current"))["items"]
    assert {i["currency"] for i in items} == {USD, "EUR"}


def test_stale_read_shows_the_state_before_the_last_write():
    fake = setup()
    ben = beneficiary(fake)
    fake.execute(transfer(ben))
    fake.inject("balances.current", Fault.STALE_READ)
    stale = {i["currency"]: i for i in fake.read(ReadRequest("/api/v1/balances/current"))["items"]}
    fresh = {i["currency"]: i for i in fake.read(ReadRequest("/api/v1/balances/current"))["items"]}
    assert stale[USD]["available_amount"] == 1000
    assert fresh[USD]["available_amount"] == 990


def test_inject_validates_names_and_faults():
    fake = setup()
    with pytest.raises(ValueError, match="does not apply"):
        fake.inject("transfers.list", Fault.INSUFFICIENT_FUNDS)
    with pytest.raises(ValueError, match="applies to reads"):
        fake.inject("transfers.create", Fault.STALE_READ)
    with pytest.raises(ValueError, match="unknown op name"):
        fake.inject("nope.create", Fault.RATE_LIMIT)
    fake.inject("transfers.create", Fault.RATE_LIMIT, times=2)
    op = transfer(beneficiary(fake))
    assert fake.execute(op).error_type == "RateLimited"
    assert fake.execute(op).error_type == "RateLimited"
    assert fake.execute(op).status is OK


# ---- connected accounts and platform money ------------------------------------------------------


def test_connected_account_lifecycle_and_fx_readiness():
    fake = FakeAirwallex(fx_ready_delay_s=120)
    created = fake.execute(
        make_op(
            "k5",
            "accounts.create",
            "/api/v1/accounts/create",
            {
                "account_details": {
                    "business_details": {"business_identifiers": [{"type": "EIN"}]}
                },
                "nickname": "poc-k5-a",
            },
            "acct",
            with_request_id=False,
        )
    )
    acct = created.provider_id
    assert acct.startswith("acct_")
    assert created.provider_status == "CREATED"
    fx = make_op(
        "k6",
        "fx.conversions.create",
        "/api/v1/fx/conversions/create",
        {"buy_currency": "EUR", "sell_currency": USD, "sell_amount": Decimal("1.00")},
        "fx",
        on_behalf_of=acct,
    )
    assert fake.execute(fx).error_code == "forbidden"
    sub = make_op(
        "k5", "accounts.submit", f"/api/v1/accounts/{acct}/submit", {}, "s", with_request_id=False
    )
    assert fake.execute(sub).provider_status == "SUBMITTED"
    fake.simulate("account_status", account_id=acct, next_status="ACTIVE")
    assert fake.read(ReadRequest(f"/api/v1/accounts/{acct}"))["status"] == "ACTIVE"
    not_ready = fake.execute(fx)
    assert not_ready.error_code == "unconfigured_client_fee"
    assert isinstance(not_ready.error(), StateConflict)
    fake.advance(120)
    assert fake.execute(fx).error_code == "insufficient_funds"  # same request_id, not burnt


def test_unknown_on_behalf_of_is_access_denied_and_tenants_are_isolated():
    fake = setup()
    a = fake.add_connected_account("poc-k5-a", {USD: usd("50.00")})
    b = fake.add_connected_account("poc-k5-b", {USD: usd("50.00")})
    with pytest.raises(Unauthorized):
        fake.read(ReadRequest("/api/v1/balances/current", on_behalf_of="acct_nope"))
    ben = beneficiary(fake, obo=a, key="ben:a")
    tid = fake.execute(transfer(ben, obo=a)).provider_id
    with pytest.raises(NotFound):
        fake.read(ReadRequest(f"/api/v1/transfers/{tid}", on_behalf_of=b))
    with pytest.raises(NotFound):
        fake.read(ReadRequest(f"/api/v1/transfers/{tid}"))
    # a connected account's transfer cannot be simulated without its header
    assert fake.execute(transition(tid, {"next_status": "SENT"})).error_type == "ValidationFailed"
    assert fake.execute(transition(tid, {"next_status": "SENT"}, obo=a)).status is OK
    assert fake.balance(USD, a) == usd("40.00")
    assert fake.balance(USD, b) == usd("50.00")


def test_platform_money_movement_shapes():
    fake = setup()
    a = fake.add_connected_account("poc-k7-a", {USD: usd("30.00")})
    cat = make_op(
        "k7",
        "connected_account_transfers.create",
        "/api/v1/connected_account_transfers/create",
        {
            "amount": Decimal("100.00"),
            "currency": USD,
            "destination": a,
            "reason": "loan_credit_repayment",
            "reference": "poc-k7-adv",
        },
        "adv",
    )
    r = fake.execute(cat)
    assert r.provider_id.startswith("tr_")
    assert r.provider_status == "NEW"
    fake.advance(1)
    assert (
        fake.read(ReadRequest(f"/api/v1/connected_account_transfers/{r.provider_id}"))["status"]
        == "SETTLED"
    )
    charge = make_op(
        "k7",
        "charges.create",
        "/api/v1/charges/create",
        {
            "amount": Decimal("25.00"),
            "currency": USD,
            "source": a,
            "reason": "professional_business_services",
            "reference": "poc-k7-fee",
        },
        "fee",
    )
    c = fake.execute(charge)
    assert c.provider_id.startswith("ch_")
    assert fake.balance(USD, a) == usd("105.00")
    dup = fake.execute(charge)
    assert dup.error_code == "request_id_duplicate"
    assert dup.provider_id == c.provider_id
    for reason, code in (("settlement", "validation_failed"), ("platform_fee", "invalid_argument")):
        bad = fake.execute(
            make_op(
                "k7",
                "charges.create",
                "/api/v1/charges/create",
                {**{k: v for k, v in charge.body.items() if k != "request_id"}, "reason": reason},
                f"r:{reason}",
            )
        )
        assert bad.error_code == code
    noref = make_op(
        "k7",
        "charges.create",
        "/api/v1/charges/create",
        {"amount": Decimal("1.00"), "currency": USD, "source": a, "reason": "taxes"},
        "noref",
    )
    assert fake.execute(noref).error_code == "field_required"


def test_on_behalf_of_on_platform_calls_matches_sandbox():
    fake = setup()
    a = fake.add_connected_account("poc-k8-a", {USD: usd("10.00")})
    b = fake.add_connected_account("poc-k8-b", {})
    charge = make_op(
        "k8",
        "charges.create",
        "/api/v1/charges/create",
        {
            "amount": Decimal("1.00"),
            "currency": USD,
            "source": a,
            "reason": "taxes",
            "reference": "poc-k8",
        },
        "c",
        on_behalf_of=a,
    )
    # Default: the same guard as AirwallexClient, nothing sent.
    guarded = fake.execute(charge)
    assert guarded.error_type == "TenantScopeViolation"
    assert fake.requests == []
    fake = FakeAirwallex(balances={USD: usd("1000.00")}, raw_platform_headers=True)
    a = fake.add_connected_account("poc-k8-a", {USD: usd("10.00")})
    b = fake.add_connected_account("poc-k8-b", {})
    charge = make_op(
        "k8",
        charge.name,
        charge.path,
        {k: v for k, v in charge.body.items() if k != "request_id"} | {"source": a},
        "c2",
        on_behalf_of=a,
    )
    refused = fake.execute(charge)
    assert refused.error_type == "ValidationFailed"
    assert refused.error_message == "Permission denied."
    assert fake.objects("charges") == []
    # x-on-behalf-of on a connected account transfer makes that account the payer
    cat = make_op(
        "k8",
        "connected_account_transfers.create",
        "/api/v1/connected_account_transfers/create",
        {
            "amount": Decimal("1.00"),
            "currency": USD,
            "destination": b,
            "reason": "goods_purchased",
            "reference": "poc-k8",
        },
        "x",
        on_behalf_of=a,
    )
    assert fake.execute(cat).status is OK
    assert fake.balance(USD, a) == usd("9.00")
    assert fake.balance(USD, b) == usd("1.00")


# ---- FX -----------------------------------------------------------------------------------------


def test_fx_conversion_quote_and_rates():
    fake = setup()
    rate = fake.read(
        ReadRequest("/api/v1/fx/rates/current", {"buy_currency": "EUR", "sell_currency": USD})
    )
    assert rate["currency_pair"] == "EURUSD"
    assert isinstance(rate["rate"], Decimal)
    other = fake.read(
        ReadRequest("/api/v1/fx/rates/current", {"buy_currency": USD, "sell_currency": "EUR"})
    )
    assert other["currency_pair"] == "EURUSD"
    assert other["rate"] < rate["rate"]
    q = fake.execute(
        make_op(
            "k1",
            "fx.quotes.create",
            "/api/v1/fx/quotes/create",
            {
                "buy_currency": "EUR",
                "sell_currency": USD,
                "buy_amount": Decimal("10.00"),
                "validity": "MIN_1",
            },
            "q",
        )
    )
    assert q.http_status == 200
    assert q.body["usage"] == "MULTI_USE"
    qid = q.provider_id
    conv = lambda key, **b: make_op(  # noqa: E731
        "k1",
        "fx.conversions.create",
        "/api/v1/fx/conversions/create",
        {"buy_currency": "EUR", "sell_currency": USD, "buy_amount": Decimal("10.00"), **b},
        key,
    )
    c1 = fake.execute(conv("c1", quote_id=qid))
    assert c1.provider_status == "SETTLED"
    assert c1.body["sell_amount"] == (Decimal("10") * q.body["client_rate"]).quantize(
        Decimal("0.01")
    )
    assert fake.execute(conv("c2", quote_id=qid)).status is OK  # sandbox allows quote reuse
    fake.advance(61)
    assert fake.execute(conv("c3", quote_id=qid)).error_code == "expired_quote"
    dup = fake.execute(conv("c1", quote_id=qid))
    assert dup.error_type == "DuplicateRequest"
    assert dup.error_code == "invalid_argument"
    big = fake.execute(conv("big", buy_amount=Decimal("5000000.00")))
    assert big.error_type == "LimitExceeded"
    novalidity = make_op(
        "k1",
        "fx.quotes.create",
        "/api/v1/fx/quotes/create",
        {"buy_currency": "EUR", "sell_currency": USD, "buy_amount": Decimal("1")},
        "qv",
    )
    assert fake.execute(novalidity).error_code == "field_required"


@settings(max_examples=40)
@given(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5000"), places=2))
def test_fx_conversion_conserves_money_exactly(amount):
    fake = setup(balance="1000000.00")
    before = fake.balance(USD)
    r = fake.execute(
        make_op(
            "k1",
            "fx.conversions.create",
            "/api/v1/fx/conversions/create",
            {"buy_currency": "EUR", "sell_currency": USD, "sell_amount": amount},
            f"fx:{amount}",
        )
    )
    assert r.status is OK
    assert before - fake.balance(USD) == usd(str(amount))
    assert fake.balance("EUR") - Money.of("1000.00", "EUR") == Money.of(
        str(r.body["buy_amount"]), "EUR"
    )


# ---- issuing ------------------------------------------------------------------------------------


def card_setup(fake, obo=None, limits=None):
    ch = fake.execute(
        make_op(
            "k2",
            "issuing.cardholders.create",
            "/api/v1/issuing/cardholders/create",
            {"type": "DELEGATE", "email": f"poc-k2-{obo}@example.com"},
            f"ch:{obo}",
            on_behalf_of=obo,
            with_request_id=False,
        )
    )
    assert ch.provider_status == "READY"
    card = fake.execute(
        make_op(
            "k2",
            "issuing.cards.create",
            "/api/v1/issuing/cards/create",
            {
                "cardholder_id": ch.provider_id,
                "created_by": "poc-k2",
                "form_factor": "VIRTUAL",
                "is_personalized": False,
                "program": {"purpose": "COMMERCIAL"},
                "authorization_controls": {
                    "allowed_transaction_count": "MULTIPLE",
                    "allowed_currencies": [USD],
                    "allowed_merchant_categories": ["5734"],
                    "transaction_limits": {
                        "currency": USD,
                        "limits": limits
                        or [
                            {"amount": Decimal("100"), "interval": "PER_TRANSACTION"},
                            {"amount": Decimal("150"), "interval": "ALL_TIME"},
                        ],
                    },
                },
            },
            f"card:{obo}",
            on_behalf_of=obo,
        )
    )
    assert card.provider_status == "ACTIVE", card
    return card.provider_id


def auth(fake, card, amount, ccy=USD, mcc="5734", obo=None, single=False):
    return fake.simulate(
        "card_authorization",
        card_id=card,
        amount=Decimal(amount),
        currency=ccy,
        merchant_category_code=mcc,
        on_behalf_of=obo,
        single_phase=single,
    )


def test_card_limit_boundary_acceptance():
    fake = setup()
    card = card_setup(fake)
    assert auth(fake, card, "100.00")["process_result"] == "APPROVED"
    assert auth(fake, card, "100.01")["failure_reason"] == "LIMIT_EXCEEDED"
    assert auth(fake, card, "10.00", ccy="EUR")["failure_reason"] == "CURRENCY_NOT_ALLOWED"
    assert (
        auth(fake, card, "10.00", mcc="5812")["failure_reason"] == "MERCHANT_CATEGORY_NOT_ALLOWED"
    )
    assert auth(fake, card, "60.00")["failure_reason"] == "LIMIT_EXCEEDED"  # ALL_TIME 150
    clearing = auth(fake, card, "20.00", single=True)
    assert clearing["type"] == "CLEARING"
    assert clearing["process_result"] == "APPROVED"
    limits = fake.read(ReadRequest(f"/api/v1/issuing/cards/{card}/limits"))
    remaining = {lim["interval"]: lim["remaining"] for lim in limits["limits"]}
    assert remaining == {"PER_TRANSACTION": 100, "ALL_TIME": 30}
    txns = fake.read(ReadRequest("/api/v1/issuing/transactions", {"card_id": card}))["items"]
    assert [t["status"] for t in txns] == [
        "PENDING",
        "FAILED",
        "FAILED",
        "FAILED",
        "FAILED",
        "APPROVED",
    ]
    assert txns[-1]["transaction_type"] == "CLEARING"
    sim_id = clearing["card_transaction_id"]
    assert txns[-1]["card_transaction_data"]["card_transaction_id"] == sim_id


def test_card_limit_checked_before_funds_and_connected_account_quirks():
    fake = setup()
    a = fake.add_connected_account("poc-k5-a", {USD: usd("25.00")})
    card = card_setup(
        fake,
        obo=a,
        limits=[
            {"amount": Decimal("50"), "interval": "PER_TRANSACTION"},
            {"amount": Decimal("200"), "interval": "MONTHLY"},
        ],
    )
    assert auth(fake, card, "70.00", obo=a)["failure_reason"] == "LIMIT_EXCEEDED"
    assert auth(fake, card, "30.00", obo=a)["failure_reason"] == "INSUFFICIENT_FUNDS"
    with pytest.raises(OutcomeUnknown):  # no header: 400, but the auth is created anyway
        auth(fake, card, "10.00")
    txns = fake.read(
        ReadRequest("/api/v1/issuing/transactions", {"card_id": card}, on_behalf_of=a)
    )["items"]
    assert txns[-1]["status"] == "PENDING"
    bal = {
        i["currency"]: i
        for i in fake.read(ReadRequest("/api/v1/balances/current", on_behalf_of=a))["items"]
    }
    assert bal[USD]["available_amount"] == 15
    assert bal[USD]["reserved_amount"] == 10


def test_card_freeze_capture_permissions_and_duplicates():
    fake = setup()
    card = card_setup(fake)
    pending = auth(fake, card, "5.00")
    capture = fake.execute(
        make_op(
            "k2",
            "issuing.simulate_capture",
            f"/api/v1/simulation/issuing/card_transaction_lifecycles/"
            f"{pending['card_transaction_lifecycle_id']}/capture",
            {},
            "cap",
            with_request_id=False,
        )
    )
    assert capture.error_type == "Unauthorized"  # sandbox: "Admin permissions required"
    freeze = make_op(
        "k2",
        "issuing.cards.update",
        f"/api/v1/issuing/cards/{card}/update",
        {"card_status": "INACTIVE"},
        "freeze",
        with_request_id=False,
    )
    assert fake.execute(freeze).provider_status == "INACTIVE"
    assert auth(fake, card, "1.00")["failure_reason"] == "CARD_INACTIVE"
    admin = FakeAirwallex(balances={USD: usd("100.00")}, admin_key=True)
    c2 = card_setup(admin)
    p = auth(admin, c2, "5.00")
    cap = admin.execute(
        make_op(
            "k2",
            "issuing.simulate_capture",
            f"/api/v1/simulation/issuing/card_transaction_lifecycles/"
            f"{p['card_transaction_lifecycle_id']}/capture",
            {},
            "cap2",
            with_request_id=False,
        )
    )
    assert cap.status is OK
    assert cap.provider_status == "APPROVED"


def test_cardholder_dedupes_on_email_not_request_id():
    fake = setup()
    op = make_op(
        "k2",
        "issuing.cardholders.create",
        "/api/v1/issuing/cardholders/create",
        {"type": "DELEGATE", "email": "poc-k2-x@example.com"},
        "chx",
    )
    first = fake.execute(op)
    again = fake.execute(op)
    assert first.status is OK
    assert again.error_type == "DuplicateRequest"  # email collision: the cardholder exists
    found = fake.read(ReadRequest("/api/v1/issuing/cardholders", {"email": "poc-k2-x@example.com"}))
    assert [c["cardholder_id"] for c in found["items"]] == [first.provider_id]
    fake2 = setup()
    fake2.inject(op.name, Fault.TIMEOUT_AFTER_SUBMIT)
    assert run(fake2, op).provider_id == fake2.objects("cardholders")[0]["cardholder_id"]


# ---- payments, disputes, files ------------------------------------------------------------------


def test_payment_intent_confirm_blocked_like_the_sandbox():
    fake = setup()
    pi = fake.execute(
        make_op(
            "k4",
            "payment_intents.create",
            "/api/v1/pa/payment_intents/create",
            {"amount": Decimal("50.00"), "currency": USD, "merchant_order_id": "poc-k4-1"},
            "pi",
        )
    )
    assert pi.provider_status == "REQUIRES_PAYMENT_METHOD"
    confirm = make_op(
        "k4",
        "payment_intents.confirm",
        f"/api/v1/pa/payment_intents/{pi.provider_id}/confirm",
        {"payment_method": {"type": "card", "card": {"number": "4035501000000008"}}},
        "pic",
    )
    assert "native API access" in fake.execute(confirm).error_message
    with pytest.raises(OutcomeUnknown):  # sandbox: 500 operation_failed on an unpaid intent
        fake.simulate(
            "dispute",
            payment_intent_id=pi.provider_id,
            stage="RFI",
            reason_code="10.4",
            due_at="2026-10-20T00:00:00+0000",
        )


def test_dispute_flow_when_online_payments_enabled():
    fake = FakeAirwallex(online_payments_enabled=True)
    ev = InMemoryEvidenceStore()

    def pi(key):
        r = fake.execute(
            make_op(
                "k4",
                "payment_intents.create",
                "/api/v1/pa/payment_intents/create",
                {"amount": Decimal("80.00"), "currency": USD, "merchant_order_id": f"poc-k4-{key}"},
                f"pi:{key}",
            )
        )
        fake.execute(
            make_op(
                "k4",
                "payment_intents.confirm",
                f"/api/v1/pa/payment_intents/{r.provider_id}/confirm",
                {"payment_method": {"card": {"number": "4035501000000008"}}},
                f"c:{key}",
            )
        )
        return r.provider_id

    d = fake.simulate(
        "dispute",
        payment_intent_id=pi("a"),
        stage="RFI",
        reason_code="10.4",
        due_at="2026-10-20T00:00:00+0000",
    )
    assert d["status"] == "REQUIRES_RESPONSE"
    assert d["due_at"]
    ref = ev.put("ev1", b"%PDF-1.4 x", file_name="ev1.pdf", content_type="application/pdf")
    up = MutationExecutor(fake, InMemoryOperationStore(), evidence_store=ev).run(
        make_op(
            "k4",
            FILES_UPLOAD_OP,
            FILES_UPLOAD_PATH,
            {"evidence_ref": ref.to_body()},
            "ev1",
            with_request_id=False,
        )
    )
    assert up.status is OK
    assert up.provider_id
    challenge = make_op(
        "k4",
        "disputes.challenge",
        f"/api/v1/pa/payment_disputes/{d['id']}/challenge",
        {
            "reason": "PRODUCT_RECEIVED",
            "supporting_documents": {
                "documents": [{"type": "PRIMARY", "file_ids": [up.provider_id]}]
            },
        },
        "ch1",
    )
    assert fake.execute(challenge).provider_status == "CHALLENGED"
    second = make_op(
        "k4",
        "disputes.challenge",
        challenge.path,
        {k: v for k, v in challenge.body.items() if k != "request_id"},
        "ch2",
    )
    assert fake.execute(second).error_type == "StateConflict"
    esc = fake.simulate("dispute_escalate", dispute_id=d["id"])
    assert (esc["stage"], esc["status"]) == ("CHARGEBACK", "REQUIRES_RESPONSE")
    with pytest.raises(StateConflict):
        fake.simulate("dispute_escalate", dispute_id=d["id"])
    assert fake.simulate("dispute_resolve", dispute_id=d["id"], outcome="LOST")["status"] == "LOST"
    d2 = fake.simulate(
        "dispute",
        payment_intent_id=pi("b"),
        stage="CHARGEBACK",
        reason_code="13.1",
        due_at="2026-10-20T00:00:00+0000",
    )
    accept = make_op(
        "k4",
        "disputes.accept",
        f"/api/v1/pa/payment_disputes/{d2['id']}/accept",
        {"reason": "LOW_VALUE_TRANSACTION"},
        "acc",
    )
    assert fake.execute(accept).provider_status == "ACCEPTED"
    assert fake.read(ReadRequest(f"/api/v1/pa/payment_disputes/{d2['id']}"))["status"] == "ACCEPTED"
    pre = fake.simulate(
        "dispute",
        payment_intent_id=pi("c"),
        stage="PRE_CHARGEBACK",
        reason_code="13.6",
        due_at="2026-10-20T00:00:00+0000",
    )
    assert pre["status"] == "ACCEPTED"


def test_upload_returns_file_object():
    fake = setup()
    got = fake.upload(b"\xff\xd8x", file_name="e.jpg", content_type="image/jpeg", notes="poc-k4")
    assert set(got) >= {"file_id", "filename", "object_type", "size"}
    assert got["size"] == 3


def test_float_body_raises_like_the_client():
    fake = setup()
    op = transfer("b")
    object.__setattr__(op, "body", {**op.body, "transfer_amount": 1.5})
    with pytest.raises(TypeError):
        fake.execute(op)


def test_insufficient_funds_error_class():
    assert InsufficientFunds
