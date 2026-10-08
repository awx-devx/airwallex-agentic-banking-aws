"""Sandbox contract tests for awx_spine (marker `sandbox`; run with `make sandbox-test`).

One happy and one negative path per endpoint family the kits use, against the real Airwallex
sandbox through AirwallexClient and MutationExecutor. Every object created carries a `poc-ct`
label (`poc-ct-<hex>` in references and nicknames). Long-lived fixtures (two ACTIVE connected
accounts, a platform beneficiary, a cardholder and a card) are named by the AWX_CONTRACT_*
environment variables below, and the module skips until all of them are set. No card is
issued per run, and card simulations are single_phase, so nothing is left reserved.

Kit 4 payments are not enabled on this sandbox account (payment intent confirm returns
"not enabled for native API access"), so the dispute tests assert that refusal.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from awx_common import Operation, OperationStatus, ReadRequest, derive_request_id
from awx_common.errors import NotFound, OutcomeUnknown, Unauthorized
from awx_common.evidence import FILES_UPLOAD_OP, FILES_UPLOAD_PATH, InMemoryEvidenceStore
from awx_common.workflow import FinalitySpec
from awx_spine import (
    AirwallexClient,
    EnvFileCredentials,
    InMemoryOperationStore,
    MutationExecutor,
    RequestIdReuse,
    poll_until_terminal,
)
from awx_spine.issuing import find_card_transaction
from awx_spine.sandbox import make_transfer_advance

FIXTURE_VARS = (
    "AWX_CONTRACT_ACCT_A",
    "AWX_CONTRACT_ACCT_B",
    "AWX_CONTRACT_CARDHOLDER",
    "AWX_CONTRACT_BENEFICIARY",
    "AWX_CONTRACT_CARD",
)
_MISSING = [name for name in FIXTURE_VARS if not os.environ.get(name)]

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        bool(_MISSING),
        reason=f"set {', '.join(_MISSING)} to this sandbox account's fixture ids "
        "(packages/awx_spine/README.md)",
    ),
]

ACCT_A = os.environ.get("AWX_CONTRACT_ACCT_A", "")
ACCT_B = os.environ.get("AWX_CONTRACT_ACCT_B", "")
CARDHOLDER = os.environ.get("AWX_CONTRACT_CARDHOLDER", "")
BENEFICIARY = os.environ.get("AWX_CONTRACT_BENEFICIARY", "")
# Fixture card on CARDHOLDER. Each run re-sets its controls to a 0.01 USD per-transaction limit
# and uses single_phase simulations, so nothing is left reserved and no card is issued.
CARD = os.environ.get("AWX_CONTRACT_CARD", "")
HASH = "c" * 64
OK = OperationStatus.SUCCEEDED


@pytest.fixture(scope="module")
def client() -> AirwallexClient:
    return AirwallexClient(EnvFileCredentials())


@pytest.fixture
def executor(client) -> MutationExecutor:
    return MutationExecutor(client, InMemoryOperationStore(), sleep=time.sleep)


def op(
    name: str,
    path: str,
    body: dict,
    *,
    kit: str = "k3",
    obo: str | None = None,
    request_id: bool = True,
) -> Operation:
    key = f"poc-ct:{uuid.uuid4()}"
    rid = derive_request_id(kit, key)
    if request_id:
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
        on_behalf_of=obo,
    )


def label() -> str:
    return f"poc-ct-{uuid.uuid4().hex[:8]}"


def us_beneficiary(nickname: str, category: str = "Checking") -> dict:
    return {
        "nickname": nickname,
        "transfer_methods": ["LOCAL"],
        "beneficiary": {
            "type": "BANK_ACCOUNT",
            "entity_type": "COMPANY",
            "company_name": "poc-ct Contract Supplier",
            "address": {
                "street_address": "1 Main Street",
                "city": "New York",
                "state": "US-NY",
                "postcode": "10001",
                "country_code": "US",
            },
            "bank_details": {
                "account_currency": "USD",
                "account_name": "poc-ct Contract",
                "account_number": "56999922",
                "account_routing_type1": "aba",
                "account_routing_value1": "021000021",
                "bank_account_category": category,
                "bank_country_code": "US",
                "local_clearing_system": "ACH",
            },
        },
    }


def transfer_body(ben: str, amount: str, ref: str) -> dict:
    return {
        "beneficiary_id": ben,
        "transfer_amount": Decimal(amount),
        "transfer_currency": "USD",
        "source_currency": "USD",
        "transfer_method": "LOCAL",
        "reference": ref,
        "reason": "goods_purchased",
    }


def usd_available(client, obo=None) -> Decimal:
    items = client.read(ReadRequest("/api/v1/balances/current", on_behalf_of=obo))["items"]
    return Decimal(str(next(b["available_amount"] for b in items if b["currency"] == "USD")))


# ---- balances and accounts ----------------------------------------------------------------------


def test_balances_read_is_a_mapping_of_items(client):
    items = client.read(ReadRequest("/api/v1/balances/current"))["items"]
    assert any(b["currency"] == "USD" for b in items)
    assert not any(isinstance(v, float) for b in items for v in b.values())


def test_connected_accounts_are_active_and_unknown_header_is_refused(client):
    for acct in (ACCT_A, ACCT_B):
        assert client.read(ReadRequest(f"/api/v1/accounts/{acct}"))["status"] == "ACTIVE"
    with pytest.raises(Unauthorized):
        client.read(ReadRequest("/api/v1/balances/current", on_behalf_of="acct_doesNotExist123"))


# ---- beneficiaries and transfers ----------------------------------------------------------------


@pytest.fixture(scope="module")
def beneficiary() -> str:
    """The long-lived platform beneficiary (AWX_CONTRACT_BENEFICIARY); no new one per run."""
    return BENEFICIARY


def test_beneficiary_negative_wrong_category_casing(executor):
    result = executor.run(
        op(
            "beneficiaries.create",
            "/api/v1/beneficiaries/create",
            us_beneficiary(label(), category="checking"),
        )
    )
    assert result.status is OperationStatus.FAILED
    assert result.error_type == "ValidationFailed"


TRANSFER_FINALITY = FinalitySpec(
    read_request=lambda r: ReadRequest(f"/api/v1/transfers/{r.provider_id}"),
    status_of=lambda b: b["status"],
    terminal=frozenset({"PAID", "CANCELLED"}),
    success=frozenset({"PAID"}),
    interval_s=2,
    timeout_s=30,
)


def transition(client, executor, tid: str, body: dict, expect: str) -> None:
    """Simulate a transition. The sandbox sometimes answers 500 yet applies it ([tr3-sent]);
    the executor then stops at OUTCOME_UNKNOWN (rule 9) and the read decides."""
    for _ in range(3):
        sim = executor.run(
            op(
                "transfers.simulate_transition",
                f"/api/v1/simulation/transfers/{tid}/transition",
                body,
                request_id=False,
            )
        )
        if sim.status is OK or sim.status is OperationStatus.OUTCOME_UNKNOWN:
            time.sleep(1)
            if client.read(ReadRequest(f"/api/v1/transfers/{tid}"))["status"] == expect:
                return
        time.sleep(2)
    raise AssertionError(f"transfer {tid} did not reach {expect}")


def test_transfer_failure_ends_cancelled_with_failure_object(client, executor, beneficiary):
    created = executor.run(
        op(
            "transfers.create",
            "/api/v1/transfers/create",
            transfer_body(beneficiary, "1.00", label()),
        )
    )
    assert created.status is OK
    assert created.provider_status == "SCHEDULED"
    tid = created.provider_id
    listed = client.read(ReadRequest("/api/v1/transfers", {"request_id": created.request_id}))
    assert [t["id"] for t in listed["items"]] == [tid]
    time.sleep(3)
    transition(client, executor, tid, {"next_status": "SENT"}, "SENT")
    transition(
        client,
        executor,
        tid,
        {"next_status": "FAILED", "failure_type": "ACCOUNT_CLOSED"},
        "CANCELLED",
    )
    status, body = poll_until_terminal(client, created, TRANSFER_FINALITY)
    assert status == "CANCELLED"
    assert body["failure"]["code"]
    assert "type" in body["failure"]["details"]


def test_transfer_negatives_insufficient_funds_and_duplicate(client, executor, beneficiary):
    nsf = executor.run(
        op(
            "transfers.create",
            "/api/v1/transfers/create",
            transfer_body(beneficiary, "50000000.00", label()),
        )
    )
    assert (nsf.error_type, nsf.error_code) == ("InsufficientFunds", "balance_insufficient")
    first = op(
        "transfers.create", "/api/v1/transfers/create", transfer_body(beneficiary, "1.00", label())
    )
    assert executor.run(first).status is OK
    # A second executor (fresh store) resends the same request_id: the provider rejects it as a
    # duplicate and the executor resolves to the original object.
    other = MutationExecutor(client, InMemoryOperationStore())
    again = other.run(first)
    assert again.status is OK
    edited = Operation(
        **{**first.__dict__, "body": {**first.body, "transfer_amount": Decimal("2.00")}}
    )
    with pytest.raises(RequestIdReuse):
        executor.run(edited)
    # A fresh store sending the same request_id with another amount is refused by the provider
    # as a duplicate; the read-back finds the 1.00 transfer and fails closed (OUTCOME_UNKNOWN
    # request_id_reused).
    mismatch = MutationExecutor(client, InMemoryOperationStore()).run(edited)
    assert mismatch.status is OperationStatus.OUTCOME_UNKNOWN  # a mismatch is never FAILED
    assert mismatch.error_code == "request_id_reused"
    tid = again.provider_id
    sim = f"/api/v1/simulation/transfers/{tid}/transition"
    time.sleep(2)
    executor.run(
        op("transfers.simulate_transition", sim, {"next_status": "CANCELLED"}, request_id=False)
    )


# ---- FX -----------------------------------------------------------------------------------------


def test_fx_quote_and_conversion(executor, client):
    quote = executor.run(
        op(
            "fx.quotes.create",
            "/api/v1/fx/quotes/create",
            {
                "buy_currency": "EUR",
                "sell_currency": "USD",
                "buy_amount": Decimal("1.00"),
                "validity": "MIN_1",
            },
            kit="k1",
        )
    )
    assert quote.status is OK
    assert quote.provider_id
    assert isinstance(quote.body["client_rate"], Decimal)
    conv = executor.run(
        op(
            "fx.conversions.create",
            "/api/v1/fx/conversions/create",
            {
                "buy_currency": "EUR",
                "sell_currency": "USD",
                "buy_amount": Decimal("1.00"),
                "quote_id": quote.provider_id,
            },
            kit="k1",
        )
    )
    assert conv.status is OK
    assert conv.provider_status == "SETTLED"
    assert conv.body["client_rate"] == quote.body["client_rate"]


def test_fx_negative_insufficient_funds(executor):
    result = executor.run(
        op(
            "fx.conversions.create",
            "/api/v1/fx/conversions/create",
            {"buy_currency": "USD", "sell_currency": "CAD", "buy_amount": Decimal("1.00")},
            kit="k1",
        )
    )
    assert (result.error_type, result.error_code) == ("InsufficientFunds", "insufficient_funds")


# ---- platform money movement --------------------------------------------------------------------

PLATFORM_FINALITY = FinalitySpec(
    read_request=lambda r: ReadRequest(
        f"/api/v1/{'charges' if r.provider_id.startswith('ch_') else 'connected_account_transfers'}"
        f"/{r.provider_id}"
    ),
    status_of=lambda b: b["status"],
    terminal=frozenset({"SETTLED", "FAILED"}),
    success=frozenset({"SETTLED"}),
    interval_s=1,
    timeout_s=30,
)


def test_connected_account_transfer_then_charge_settle(client, executor):
    cat = executor.run(
        op(
            "connected_account_transfers.create",
            "/api/v1/connected_account_transfers/create",
            {
                "amount": Decimal("1.00"),
                "currency": "USD",
                "destination": ACCT_B,
                "reason": "wages_salary",
                "reference": label(),
            },
            kit="k5",
        )
    )
    assert cat.status is OK
    assert cat.provider_status == "NEW"
    assert poll_until_terminal(client, cat, PLATFORM_FINALITY)[0] == "SETTLED"
    charge = executor.run(
        op(
            "charges.create",
            "/api/v1/charges/create",
            {
                "amount": Decimal("1.00"),
                "currency": "USD",
                "source": ACCT_B,
                "reason": "professional_business_services",
                "reference": label(),
            },
            kit="k5",
        )
    )
    assert charge.status is OK
    assert poll_until_terminal(client, charge, PLATFORM_FINALITY)[0] == "SETTLED"


def test_platform_money_negatives(executor):
    bad_reason = executor.run(
        op(
            "connected_account_transfers.create",
            "/api/v1/connected_account_transfers/create",
            {
                "amount": Decimal("1.00"),
                "currency": "USD",
                "destination": ACCT_B,
                "reason": "settlement",
                "reference": label(),
            },
            kit="k8",
        )
    )
    assert bad_reason.error_type == "ValidationFailed"
    nsf = executor.run(
        op(
            "charges.create",
            "/api/v1/charges/create",
            {
                "amount": Decimal("50000.00"),
                "currency": "USD",
                "source": ACCT_B,
                "reason": "professional_business_services",
                "reference": label(),
            },
            kit="k8",
        )
    )
    assert (nsf.error_type, nsf.error_code) == ("InsufficientFunds", "insufficient_fund")
    guarded = executor.run(
        op(
            "charges.create",
            "/api/v1/charges/create",
            {
                "amount": Decimal("1.00"),
                "currency": "USD",
                "source": ACCT_B,
                "reason": "taxes",
                "reference": label(),
            },
            kit="k8",
            obo=ACCT_B,
        )
    )
    assert guarded.error_type == "TenantScopeViolation"
    cat = executor.run(
        op(
            "connected_account_transfers.create",
            "/api/v1/connected_account_transfers/create",
            {
                "amount": Decimal("1.00"),
                "currency": "USD",
                "destination": ACCT_B,
                "reason": "wages_salary",
                "reference": label(),
            },
            kit="k8",
            obo=ACCT_A,
        )
    )
    assert cat.error_type == "TenantScopeViolation"  # refused before sending: A pays nothing


# ---- issuing ------------------------------------------------------------------------------------


def test_card_limits_inclusive_and_declines(client, executor):
    controls = {
        "allowed_transaction_count": "MULTIPLE",
        "allowed_currencies": ["USD"],
        "allowed_merchant_categories": ["5734"],
        "transaction_limits": {
            "currency": "USD",
            "limits": [{"amount": Decimal("0.01"), "interval": "PER_TRANSACTION"}],
        },
    }
    update = executor.run(
        op(
            "issuing.cards.update",
            f"/api/v1/issuing/cards/{CARD}/update",
            {"authorization_controls": controls},
            kit="k2",
            request_id=False,
        )
    )
    assert update.status is OK
    assert update.provider_status == "ACTIVE"

    def auth(amount: str, ccy: str = "USD", mcc: str = "5734") -> dict:
        r = executor.run(
            op(
                "issuing.simulate_authorization",
                "/api/v1/simulation/issuing/create",
                {
                    "card_id": CARD,
                    "transaction_amount": Decimal(amount),
                    "transaction_currency": ccy,
                    "merchant_category_code": mcc,
                    "merchant_info": "poc-ct",
                    "single_phase": True,  # clears or declines at once: nothing stays reserved
                },
                kit="k2",
                request_id=False,
            )
        )
        assert r.status is OK, r
        assert r.body["type"] == "CLEARING"
        return r.body

    before = client.read(ReadRequest("/api/v1/balances/current"))["items"]
    reserved = next(b["reserved_amount"] for b in before if b["currency"] == "USD")
    approved = auth("0.01")
    assert approved["process_result"] == "APPROVED"
    assert auth("0.02")["failure_reason"] == "LIMIT_EXCEEDED"
    assert auth("0.01", ccy="EUR")["failure_reason"] == "CURRENCY_NOT_ALLOWED"
    assert auth("0.01", mcc="5812")["failure_reason"] == "MERCHANT_CATEGORY_NOT_ALLOWED"
    after = client.read(ReadRequest("/api/v1/balances/current"))["items"]
    assert next(b["reserved_amount"] for b in after if b["currency"] == "USD") == reserved
    item = find_card_transaction(
        client, card_id=CARD, card_transaction_id=approved["card_transaction_id"]
    )
    assert item is not None
    got = client.read(ReadRequest(f"/api/v1/issuing/transactions/{item['transaction_id']}"))
    assert got["transaction_id"] == item["transaction_id"]


# ---- sandbox finality driver ---------------------------------------------------------------------


def _fail_if_left_in_flight(client, executor, transfer_id: str) -> None:
    """Never leave a transfer SENT or in flight: FAILED ends it CANCELLED and refunds it.
    (CANCELLED from SENT is refused by the sandbox.)"""
    status = client.read(ReadRequest(f"/api/v1/transfers/{transfer_id}"))["status"]
    if status in ("PAID", "CANCELLED"):
        return
    path = f"/api/v1/simulation/transfers/{transfer_id}/transition"
    if status != "SENT":
        executor.run(
            op("transfers.simulate_transition", path, {"next_status": "SENT"}, request_id=False)
        )
        time.sleep(1)
    executor.run(
        op(
            "transfers.simulate_transition",
            path,
            {"next_status": "FAILED", "failure_type": "CLIENT_REQUESTED"},
            request_id=False,
        )
    )


def test_make_transfer_advance_live(client, executor, beneficiary):
    advance = make_transfer_advance("k3", hold_s=2)
    for reference in (label(), label() + "-nosettle"):
        create = op(
            "transfers.create",
            "/api/v1/transfers/create",
            transfer_body(beneficiary, "1.00", reference),
        )
        result = executor.run(create)
        assert result.status is OK
        try:
            read = ReadRequest(f"/api/v1/transfers/{result.provider_id}")
            final = None
            for attempt in range(1, 8):
                time.sleep(2)
                body = client.read(read)
                if body["status"] in ("PAID", "CANCELLED"):
                    final = body["status"]
                    break
                step = advance(create, result, body, datetime.now(UTC), attempt)
                if step is None:
                    if body["status"] == "SENT" and reference.endswith("-nosettle"):
                        final = "HELD_AT_SENT"
                        break
                    continue
                executor.run(step)  # a 500 that applied is fine: the next poll decides
            if reference.endswith("-nosettle"):
                assert final == "HELD_AT_SENT"
            else:
                assert final == "PAID"
        finally:
            _fail_if_left_in_flight(client, executor, result.provider_id)


# ---- payments, disputes, files ------------------------------------------------------------------


def test_payment_intent_create_and_confirm_refusal(executor):
    pi = executor.run(
        op(
            "payment_intents.create",
            "/api/v1/pa/payment_intents/create",
            {"amount": Decimal("5.00"), "currency": "USD", "merchant_order_id": label()},
            kit="k4",
        )
    )
    assert pi.status is OK
    assert pi.provider_status == "REQUIRES_PAYMENT_METHOD"
    confirm = executor.run(
        op(
            "payment_intents.confirm",
            f"/api/v1/pa/payment_intents/{pi.provider_id}/confirm",
            {
                "payment_method": {
                    "type": "card",
                    "card": {
                        "number": "4035501000000008",
                        "expiry_month": "12",
                        "expiry_year": "2030",
                        "cvc": "123",
                        "name": "poc-ct",
                    },
                }
            },
            kit="k4",
        )
    )
    assert confirm.error_type == "ValidationFailed"
    assert "native API access" in (confirm.error_message or "")
    dispute = executor.run(
        op(
            "disputes.simulate_create",
            "/api/v1/simulation/pa/payment_disputes/create",
            {
                "payment_intent_id": pi.provider_id,
                "stage": "RFI",
                "reason_code": "10.4",
                "due_at": "2026-12-01T00:00:00+0000",
            },
            kit="k4",
            request_id=False,
        )
    )
    assert dispute.status is OperationStatus.OUTCOME_UNKNOWN  # 500 operation_failed
    assert OutcomeUnknown


def test_dispute_get_unknown_is_not_found(client):
    with pytest.raises(NotFound):
        client.read(ReadRequest("/api/v1/pa/payment_disputes/dst_poc_ct_missing"))


def test_files_upload_through_executor(client):
    store = InMemoryEvidenceStore()
    jpeg = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
    ref = store.put("poc-ct-ev", jpeg, file_name="poc-ct.jpg", content_type="image/jpeg")
    ex = MutationExecutor(client, InMemoryOperationStore(), evidence_store=store)
    result = ex.run(
        op(
            FILES_UPLOAD_OP,
            FILES_UPLOAD_PATH,
            {"evidence_ref": ref.to_body(), "notes": label()},
            kit="k4",
            request_id=False,
        )
    )
    assert result.status is OK
    assert result.provider_id
    assert result.body["object_type"] == "file"
