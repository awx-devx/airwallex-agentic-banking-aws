"""FakeAirwallex test controls and modelled behaviour that kits rely on: prices, credits and
reservations, platform movement outcomes, the REFUSE and END_AS faults, ids unique across
instances, the opt-in nickname filter, dispute body rules and refunds, financial transactions
and balance history, and hidden objects on GET by id."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from awx_common import Money, Operation, OperationStatus, ReadRequest
from awx_common.errors import NotFound, ValidationFailed
from awx_spine import FakeAirwallex, Fault
from awx_spine.fake import EPOCH
from awx_spine.testing import make_op

OK = OperationStatus.SUCCEEDED
FAILED = OperationStatus.FAILED
UNKNOWN = OperationStatus.OUTCOME_UNKNOWN
USD = "USD"


def usd(x: str) -> Money:
    return Money.of(x, USD)


def test_set_price_credit_and_reserve_change_prices_and_balances():
    fake = FakeAirwallex(balances={USD: usd("10")})
    fake.set_price("EUR", Decimal("1.5"))
    assert fake.price("EUR") == Decimal("1.5")
    acct = fake.add_connected_account("poc-a", {})
    assert fake.credit(USD, "5.00", account=acct) == usd("5.00")
    assert fake.credit(USD, "-3.00") == usd("7.00")
    with pytest.raises(ValueError, match="below zero"):
        fake.credit(USD, "-8.00")
    fake.reserve(USD, "2.00")
    bal = fake.read(ReadRequest("/api/v1/balances/current"))["items"][0]
    assert (bal["available_amount"], bal["reserved_amount"]) == (5, 2)
    with pytest.raises(ValueError, match="positive"):
        fake.set_price("EUR", "0")


def _move(fake: FakeAirwallex, kind: str, acct: str, key: str) -> Operation:
    name, path, field = {
        "cat": (
            "connected_account_transfers.create",
            "/api/v1/connected_account_transfers/create",
            "destination",
        ),
        "charge": ("charges.create", "/api/v1/charges/create", "source"),
    }[kind]
    return make_op(
        "k8",
        name,
        path,
        {
            "amount": Decimal("10.00"),
            "currency": USD,
            field: acct,
            "reason": "goods_purchased",
            "reference": "poc",
        },
        key,
    )


@pytest.mark.parametrize("status", ["SUSPENDED", "PENDING", "FAILED"])
def test_set_movement_status_by_request_id_returns_money_on_failed(status):
    fake = FakeAirwallex(balances={USD: usd("100")})
    acct = fake.add_connected_account("poc-seller", {})
    op = _move(fake, "cat", acct, f"m:{status}")
    assert fake.execute(op).status is OK
    obj = fake.set_movement_status(op.request_id, status)
    assert obj["status"] == status
    fake.advance(5)  # never settles once ended
    assert fake.find("connected_account_transfers", op.request_id)["status"] == status
    expect = ("100.00", "0.00") if status == "FAILED" else ("90.00", "10.00")
    assert (fake.balance(USD), fake.balance(USD, acct)) == (usd(expect[0]), usd(expect[1]))
    with pytest.raises(KeyError):
        fake.set_movement_status("nope", "FAILED")
    with pytest.raises(ValueError, match="status must be"):
        fake.set_movement_status(op.request_id, "SETTLED")


def test_end_as_fault_ends_a_charge_failed_at_settlement_time():
    fake = FakeAirwallex(balances={USD: usd("0")})
    acct = fake.add_connected_account("poc-a", {USD: usd("50")})
    op = _move(fake, "charge", acct, "c1")
    fake.inject(op.name, Fault.END_AS, final_status="FAILED")
    assert fake.execute(op).provider_status == "NEW"
    assert fake.balance(USD, acct) == usd("40.00")
    fake.advance(1)
    assert fake.find("charges", op.request_id)["status"] == "FAILED"
    assert fake.balance(USD, acct) == usd("50.00")
    with pytest.raises(ValueError, match="END_AS"):
        fake.inject("transfers.create", Fault.END_AS, final_status="FAILED")
    with pytest.raises(ValueError, match="END_AS"):
        fake.inject(op.name, Fault.END_AS, final_status="SETTLED")


def test_refuse_fault_is_a_definitive_400_on_any_write_and_writes_nothing():
    fake = FakeAirwallex()
    op = make_op(
        "k3", "beneficiaries.create", "/api/v1/beneficiaries/create", {"beneficiary": {}}, "b"
    )
    fake.inject(op.name, Fault.REFUSE, code="invalid_argument")
    got = fake.execute(op)
    assert (got.status, got.http_status, got.error_code) == (FAILED, 400, "invalid_argument")
    assert got.error_type == "ValidationFailed"
    assert fake.objects("beneficiaries") == []
    fake.inject(op.name, Fault.RATE_LIMIT, times=5)
    fake.clear_faults(op.name)
    assert fake.execute(op).status is OK
    with pytest.raises(ValueError, match="REFUSE does not apply"):
        fake.inject("transfers.list", Fault.REFUSE)


def test_transfer_ids_are_unique_across_fake_instances():
    def first_transfer(fake: FakeAirwallex) -> str:
        ben = fake.execute(
            make_op(
                "k6",
                "beneficiaries.create",
                "/api/v1/beneficiaries/create",
                {"beneficiary": {}},
                "b",
            )
        ).provider_id
        return fake.execute(
            make_op(
                "k6",
                "transfers.create",
                "/api/v1/transfers/create",
                {
                    "beneficiary_id": ben,
                    "transfer_amount": Decimal("1"),
                    "transfer_currency": USD,
                    "transfer_method": "LOCAL",
                    "reference": "poc",
                },
                "t",
            )
        ).provider_id

    a, b = (first_transfer(FakeAirwallex(balances={USD: usd("10")})) for _ in range(2))
    assert a != b
    same = {
        first_transfer(FakeAirwallex(balances={USD: usd("10")}, instance_id="fixed"))
        for _ in range(2)
    }
    assert len(same) == 1


@pytest.mark.parametrize("opt_in", [False, True])
def test_beneficiaries_nickname_filter_is_opt_in_because_the_sandbox_ignores_it(opt_in):
    fake = FakeAirwallex(nickname_filter=opt_in)
    for nick in ("poc-k6-alice", "poc-k6-bob"):
        fake.execute(
            make_op(
                "k6",
                "beneficiaries.create",
                "/api/v1/beneficiaries/create",
                {"nickname": nick, "beneficiary": {}},
                nick,
            )
        )
    got = fake.read(ReadRequest("/api/v1/beneficiaries", {"nickname": "poc-k6-bob"}))["items"]
    expect = ["poc-k6-bob"] if opt_in else ["poc-k6-alice", "poc-k6-bob"]
    assert sorted(b["nickname"] for b in got) == expect


def _dispute(fake: FakeAirwallex, stage: str) -> dict:
    pi = fake.execute(
        make_op(
            "k4",
            "payment_intents.create",
            "/api/v1/pa/payment_intents/create",
            {"amount": Decimal("80.00"), "currency": USD, "merchant_order_id": f"poc-{stage}"},
            f"pi:{stage}",
        )
    )
    fake.execute(
        make_op(
            "k4",
            "payment_intents.confirm",
            f"/api/v1/pa/payment_intents/{pi.provider_id}/confirm",
            {"payment_method": {"card": {"number": "4035501000000008"}}},
            f"c:{stage}",
        )
    )
    return fake.simulate(
        "dispute",
        payment_intent_id=pi.provider_id,
        stage=stage,
        reason_code="10.4",
        due_at="2026-12-01T00:00:00+0000",
    )


def test_dispute_accept_and_challenge_body_rules_and_refunds():
    fake = FakeAirwallex(online_payments_enabled=True)
    rfi = _dispute(fake, "RFI")
    path = f"/api/v1/pa/payment_disputes/{rfi['id']}/accept"
    no_rid = make_op(
        "k4", "disputes.accept", path, {"reason": "OTHERS"}, "a0", with_request_id=False
    )
    assert fake.execute(no_rid).error_code == "validation_error"
    accepted = fake.execute(make_op("k4", "disputes.accept", path, {"reason": "OTHERS"}, "a1"))
    assert accepted.provider_status == "ACCEPTED"
    refunds = fake.read(
        ReadRequest("/api/v1/pa/refunds", {"payment_intent_id": rfi["payment_intent_id"]})
    )["items"]
    assert [(r["amount"], r["status"]) for r in refunds] == [(80, "SUCCEEDED")]
    cb = _dispute(fake, "CHARGEBACK")
    up = fake.upload(b"%PDF x", file_name="e.pdf", content_type="application/pdf")
    docs = {
        "supporting_documents": {"documents": [{"type": "PRIMARY", "file_ids": [up["file_id"]]}]}
    }
    cpath = f"/api/v1/pa/payment_disputes/{cb['id']}/challenge"
    missing = fake.execute(make_op("k4", "disputes.challenge", cpath, docs, "ch0"))
    assert missing.error_type == "ValidationFailed"
    assert "product_type" in missing.error_message
    ok = fake.execute(
        make_op(
            "k4", "disputes.challenge", cpath, {**docs, "product_type": "PHYSICAL_GOODS"}, "ch1"
        )
    )
    assert ok.provider_status == "CHALLENGED"
    assert (
        fake.read(
            ReadRequest("/api/v1/pa/refunds", {"payment_intent_id": cb["payment_intent_id"]})
        )["items"]
        == []
    )


def test_financial_transactions_and_balance_history_are_modelled():
    fake = FakeAirwallex(balances={USD: usd("100")})
    ga = fake.execute(
        make_op(
            "k6",
            "global_accounts.create",
            "/api/v1/global_accounts/create",
            {
                "country_code": "US",
                "required_features": [{"currency": USD, "transfer_method": "LOCAL"}],
            },
            "ga",
        )
    )
    fake.advance(60)
    dep = fake.simulate("deposit", global_account_id=ga.provider_id, amount=Decimal("25"))
    fake.credit(USD, "-5")
    txns = fake.read(
        ReadRequest(
            "/api/v1/financial_transactions",
            {"from_created_at": (EPOCH + timedelta(seconds=30)).isoformat()},
        )
    )["items"]
    assert [(t["transaction_type"], t["amount"], t["source_id"]) for t in txns] == [
        ("DEPOSIT", 25, dep["id"])
    ]
    later = (EPOCH + timedelta(seconds=61)).isoformat()
    assert (
        fake.read(ReadRequest("/api/v1/financial_transactions", {"from_created_at": later}))[
            "items"
        ]
        == []
    )
    hist = fake.read(ReadRequest("/api/v1/balances/history", {"currency": USD}))["items"]
    assert [(h["transaction_type"], h["amount"], h["balance"]) for h in hist] == [
        ("OPENING_BALANCE", 100, 100),
        ("DEPOSIT", 25, 125),
        ("ADJUSTMENT", -5, 120),
    ]
    with pytest.raises(ValidationFailed):
        fake.read(ReadRequest("/api/v1/balances/history", {"from_post_at": "yesterday"}))


def test_by_id_read_of_a_landed_but_hidden_object_is_not_found():
    """A kit treats this NotFound, after a send that may have landed, as unknown."""
    fake = FakeAirwallex(balances={USD: usd("100")})
    acct = fake.add_connected_account("poc-a", {})
    op = _move(fake, "cat", acct, "hidden")
    fake.inject(op.name, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=60)
    assert fake.execute(op).status is UNKNOWN
    landed = fake.find("connected_account_transfers", op.request_id)
    assert landed is not None
    with pytest.raises(NotFound):
        fake.read(ReadRequest(f"/api/v1/connected_account_transfers/{landed['id']}"))
    fake.advance(60)
    got = fake.read(ReadRequest(f"/api/v1/connected_account_transfers/{landed['id']}"))
    assert got["id"] == landed["id"]


def test_a_failed_movement_never_takes_the_destination_below_zero():
    fake = FakeAirwallex(balances={USD: usd("100")})
    acct = fake.add_connected_account("poc-seller", {})
    op = _move(fake, "cat", acct, "spent")
    assert fake.execute(op).status is OK
    fake.credit(USD, "-10.00", account=acct)  # the seller spent the money elsewhere
    with pytest.raises(ValueError, match="below zero"):
        fake.set_movement_status(op.request_id, "FAILED")
    assert fake.find("connected_account_transfers", op.request_id)["status"] == "NEW"
    op2 = _move(fake, "cat", acct, "spent2")
    fake.inject(op2.name, Fault.END_AS, final_status="FAILED")
    fake.execute(op2)
    fake.credit(USD, "-10.00", account=acct)
    fake.advance(1)
    assert fake.find("connected_account_transfers", op2.request_id)["status"] == "SUSPENDED"
    assert fake.balance(USD, acct) == usd("0.00")
    assert fake.balance(USD) == usd("80.00")


def test_balance_history_tracks_available_through_card_money():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_f16_test_fake", Path(__file__).with_name("test_fake.py")
    )
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    auth, card_setup = helpers.auth, helpers.card_setup

    fake = FakeAirwallex(balances={USD: usd("100")})
    card = card_setup(fake)
    auth(fake, card, Decimal("12.00"))
    fake.reserve(USD, "3.00")
    hist = fake.read(ReadRequest("/api/v1/balances/history", {"currency": USD}))["items"]
    available = fake.read(ReadRequest("/api/v1/balances/current"))["items"][0]["available_amount"]
    assert hist[-1]["balance"] == available == 85
    assert [h["transaction_type"] for h in hist][-2:] == ["CARD_AUTHORIZATION", "RESERVE"]
