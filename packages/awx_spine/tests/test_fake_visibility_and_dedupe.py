"""FakeAirwallex visibility lag, the deduplication window, decline order with injected faults,
the request_id table per deduplicating endpoint, and paging in find_card_transaction."""

from __future__ import annotations

from decimal import Decimal

import pytest
from awx_common import Money, Operation, OperationStatus, ReadRequest
from awx_common.errors import NotFound
from awx_spine import FakeAirwallex, Fault, InMemoryOperationStore, MutationExecutor
from awx_spine.issuing import find_card_transaction
from awx_spine.testing import make_op

OK = OperationStatus.SUCCEEDED
FAILED = OperationStatus.FAILED
UNKNOWN = OperationStatus.OUTCOME_UNKNOWN
USD = "USD"
WEEK = 7 * 86400


def usd(x: str) -> Money:
    return Money.of(x, USD)


# ---- time-based, object-scoped visibility -----------------------------------------------------


def _beneficiary(fake: FakeAirwallex, key: str) -> str:
    return fake.execute(
        make_op(
            "k3", "beneficiaries.create", "/api/v1/beneficiaries/create", {"beneficiary": {}}, key
        )
    ).provider_id


def _transfer_op(ben: str, key: str, amount: str = "1.00") -> Operation:
    return make_op(
        "k3",
        "transfers.create",
        "/api/v1/transfers/create",
        {
            "beneficiary_id": ben,
            "transfer_amount": Decimal(amount),
            "transfer_currency": USD,
            "transfer_method": "LOCAL",
            "reference": "poc",
        },
        key,
    )


def test_landed_but_invisible_write_survives_unrelated_writes():
    fake = FakeAirwallex(balances={USD: usd("100")})
    ben = _beneficiary(fake, "b")
    op = _transfer_op(ben, "pay:1")
    fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=30)
    assert fake.execute(op).status is UNKNOWN
    landed = fake.objects("transfers")[0]
    for i in range(5):  # unrelated writes in between do not end the lag
        fake.execute(_transfer_op(ben, f"other:{i}"))
        fake.advance(1)
    assert fake.read(ReadRequest("/api/v1/transfers", {"request_id": op.request_id}))["items"] == []
    with pytest.raises(NotFound):
        fake.read(ReadRequest(f"/api/v1/transfers/{landed['id']}"))
    # deduplication sees it at once
    assert fake.execute(op).error_type == "DuplicateRequest"
    fake.advance(25)
    listed = fake.read(ReadRequest("/api/v1/transfers", {"request_id": op.request_id}))["items"]
    assert [t["id"] for t in listed] == [landed["id"]]
    assert fake.read(ReadRequest(f"/api/v1/transfers/{landed['id']}"))["id"] == landed["id"]


def test_executor_on_invisible_write_stops_unknown_then_reconciles_after_the_lag():
    fake = FakeAirwallex(balances={USD: usd("100")})
    ben = _beneficiary(fake, "b")
    op = _transfer_op(ben, "pay:2")
    fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=120)
    store = InMemoryOperationStore()
    first = MutationExecutor(fake, store, sleep=fake.advance, backoff_s=1).run(op)
    assert first.status is UNKNOWN  # empty read-back, resend refused as duplicate: no new id
    assert len(fake.objects("transfers")) == 1
    sent_ids = {r[1] for r in fake.requests if r[0] == "POST"}
    assert sent_ids == {"/api/v1/beneficiaries/create", "/api/v1/transfers/create"}
    fake.advance(120)
    later = MutationExecutor(fake, store, sleep=fake.advance).run(op)
    assert later.status is OK
    assert later.provider_id == fake.objects("transfers")[0]["id"]
    assert len(fake.objects("transfers")) == 1


def test_list_lag_after_a_successful_create():
    fake = FakeAirwallex(balances={USD: usd("100")})
    acct = fake.add_connected_account("poc-a", {})
    op = make_op(
        "k5",
        "connected_account_transfers.create",
        "/api/v1/connected_account_transfers/create",
        {
            "amount": Decimal("1.00"),
            "currency": USD,
            "destination": acct,
            "reason": "wages_salary",
            "reference": "poc",
        },
        "cat:lag",
    )
    fake.inject(op.name, Fault.LIST_LAG, visible_after_s=5)
    result = fake.execute(op)
    assert result.status is OK
    q = ReadRequest("/api/v1/connected_account_transfers", {"request_id": op.request_id})
    assert fake.read(q)["items"] == []
    with pytest.raises(NotFound):
        fake.read(ReadRequest(f"/api/v1/connected_account_transfers/{result.provider_id}"))
    fake.advance(5)
    assert len(fake.read(q)["items"]) == 1


def test_global_list_lag_applies_to_every_create():
    fake = FakeAirwallex(balances={USD: usd("100")}, list_lag_s=2)
    ben = _beneficiary(fake, "b")
    op = _transfer_op(ben, "pay:3")
    fake.execute(op)
    q = ReadRequest("/api/v1/transfers", {"request_id": op.request_id})
    assert fake.read(q)["items"] == []
    fake.advance(2)
    assert len(fake.read(q)["items"]) == 1


def test_visibility_arguments_are_validated():
    fake = FakeAirwallex()
    with pytest.raises(ValueError, match="LIST_LAG needs"):
        fake.inject("transfers.create", Fault.LIST_LAG)
    with pytest.raises(ValueError, match="visible_after_s"):
        fake.inject("transfers.create", Fault.RATE_LIMIT, visible_after_s=1)
    with pytest.raises(ValueError, match="list_lag_s"):
        FakeAirwallex(list_lag_s=-1)


# ---- the deduplication window expires ---------------------------------------------------------


def test_request_id_creates_a_second_object_past_the_window():
    fake = FakeAirwallex(balances={USD: usd("100")})
    ben = _beneficiary(fake, "b")
    op = _transfer_op(ben, "pay:4")
    assert fake.execute(op).status is OK
    fake.advance(WEEK - 1)
    assert fake.execute(op).error_type == "DuplicateRequest"
    fake.advance(2)
    again = fake.execute(op)
    assert again.status is OK
    assert len(fake.objects("transfers")) == 2
    assert fake.balance(USD) == usd("98.00")


# ---- injected NSF on a card keeps the sandbox order -------------------------------------------


def _card(fake: FakeAirwallex, limit: str) -> str:
    ch = fake.execute(
        make_op(
            "k2",
            "issuing.cardholders.create",
            "/api/v1/issuing/cardholders/create",
            {"type": "DELEGATE", "email": "poc@example.com"},
            "ch",
            with_request_id=False,
        )
    )
    return fake.execute(
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
                    "transaction_limits": {
                        "currency": USD,
                        "limits": [{"amount": Decimal(limit), "interval": "PER_TRANSACTION"}],
                    },
                },
            },
            "card",
        )
    ).provider_id


def test_injected_nsf_still_lets_the_limit_decline_first():
    fake = FakeAirwallex(balances={USD: usd("100")})
    card = _card(fake, "10")

    def auth(amount: str) -> str:
        fake.inject("issuing.simulate_authorization", Fault.INSUFFICIENT_FUNDS)
        r = fake.execute(
            make_op(
                "k2",
                "issuing.simulate_authorization",
                "/api/v1/simulation/issuing/create",
                {
                    "card_id": card,
                    "transaction_amount": Decimal(amount),
                    "transaction_currency": USD,
                },
                f"a:{amount}",
                with_request_id=False,
            )
        )
        return r.body["failure_reason"]

    assert auth("20.00") == "LIMIT_EXCEEDED"
    assert auth("5.00") == "INSUFFICIENT_FUNDS"


# ---- the request_id table per deduplicating endpoint ------------------------------------------


def _dedupe_cases(fake: FakeAirwallex):
    acct = fake.add_connected_account("poc-a", {USD: usd("50")})
    ben = _beneficiary(fake, "b")
    ch = fake.execute(
        make_op(
            "k2",
            "issuing.cardholders.create",
            "/api/v1/issuing/cardholders/create",
            {"type": "DELEGATE", "email": "poc-d@example.com"},
            "ch",
            with_request_id=False,
        )
    ).provider_id
    card = {
        "cardholder_id": ch,
        "created_by": "poc",
        "form_factor": "VIRTUAL",
        "nick_name": "poc",
        "is_personalized": False,
        "program": {"purpose": "COMMERCIAL"},
        "authorization_controls": {
            "allowed_transaction_count": "MULTIPLE",
            "transaction_limits": {"currency": USD, "limits": []},
        },
    }
    return [
        (
            "transfers.create",
            "/api/v1/transfers/create",
            {
                "beneficiary_id": ben,
                "transfer_amount": Decimal("1.00"),
                "transfer_currency": USD,
                "transfer_method": "LOCAL",
                "reference": "poc",
            },
            "transfer_amount",
            "/api/v1/transfers",
            "duplicate_request_id",
        ),
        (
            "fx.conversions.create",
            "/api/v1/fx/conversions/create",
            {"buy_currency": "EUR", "sell_currency": USD, "buy_amount": Decimal("1.00")},
            "buy_amount",
            "/api/v1/fx/conversions",
            "invalid_argument",
        ),
        (
            "connected_account_transfers.create",
            "/api/v1/connected_account_transfers/create",
            {
                "amount": Decimal("1.00"),
                "currency": USD,
                "destination": acct,
                "reason": "wages_salary",
                "reference": "poc",
            },
            "amount",
            "/api/v1/connected_account_transfers",
            "request_id_duplicate",
        ),
        (
            "charges.create",
            "/api/v1/charges/create",
            {
                "amount": Decimal("1.00"),
                "currency": USD,
                "source": acct,
                "reason": "taxes",
                "reference": "poc",
            },
            "amount",
            "/api/v1/charges",
            "request_id_duplicate",
        ),
        (
            "issuing.cards.create",
            "/api/v1/issuing/cards/create",
            card,
            "nick_name",
            None,
            "invalid_argument",
        ),
        (
            "payment_intents.create",
            "/api/v1/pa/payment_intents/create",
            {"amount": Decimal("5.00"), "currency": USD, "merchant_order_id": "poc"},
            "amount",
            None,
            "duplicate_request",
        ),
    ]


def test_request_id_table_per_deduplicating_endpoint():
    fake = FakeAirwallex(balances={USD: usd("100")})
    for name, path, body, changed, list_path, code in _dedupe_cases(fake):
        op = make_op("k5", name, path, body, f"dd:{name}")
        first = fake.execute(op)
        assert first.status is OK, name
        same = fake.execute(op)
        bumped = body[changed] + 1 if isinstance(body[changed], Decimal) else "poc-changed"
        other = fake.execute(make_op("k5", name, path, {**body, changed: bumped}, f"dd:{name}"))
        for dup in (same, other):
            assert (dup.error_type, dup.error_code) == ("DuplicateRequest", code), name
        if code in ("duplicate_request_id", "request_id_duplicate"):
            assert other.provider_id == first.provider_id, name  # names the original
        if list_path is not None:
            listed = fake.read(ReadRequest(list_path, {"request_id": op.request_id}))["items"]
            assert len(listed) == 1, name


# ---- find_card_transaction pages --------------------------------------------------------------


def test_find_card_transaction_pages_until_found():
    fake = FakeAirwallex(balances={USD: usd("1000")})
    card = _card(fake, "100")
    sims = [
        fake.simulate(
            "card_authorization", card_id=card, amount=Decimal("1"), currency=USD, single_phase=True
        )
        for _ in range(250)
    ]
    item = find_card_transaction(
        fake, card_id=card, card_transaction_id=sims[-1]["card_transaction_id"]
    )
    assert item is not None
    pages = [r for r in fake.requests if r[1] == "/api/v1/issuing/transactions"]
    assert len(pages) == 3
    with pytest.raises(Exception, match="BELOW_MIN_PAGE_SIZE"):
        fake.read(ReadRequest("/api/v1/issuing/transactions", {"card_id": card, "page_size": "5"}))
