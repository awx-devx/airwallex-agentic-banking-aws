from dataclasses import replace
from decimal import Decimal

import pytest
from awx_common import (
    Money,
    OperationResult,
    OperationStatus,
    TenantContext,
    check_tenant_scope,
)
from awx_common.errors import TenantScopeViolation, ValidationFailed
from awx_common.workflow import ReconcileUndetermined
from awx_k8_settlement.ledger import CycleInput, SellerLedger, settle_cycle
from awx_k8_settlement.operations import (
    PAYOUT_REASON,
    RECOVERY_REASON,
    Outcome,
    available_balance,
    build_payout_op,
    build_recovery_op,
    find_by_request_id,
    payout_request_id,
    read_outcome,
    recovery_request_id,
    resolve,
    seller_balance_request,
)

HASH = "b" * 64
BOOK = "book0001"
BOB = TenantContext("bob", "acct_bob000001")
ALICE = TenantContext("alice", "acct_alice0001")
# FAKE-FIDELITY.md row 106: accepted on both endpoints; settlement and commission are not.
ACCEPTED = {"goods_purchased", "professional_business_services", "other_services", "wages_salary"}


def settlement(seller="bob"):
    s, _ = settle_cycle(
        SellerLedger.open(seller, "USD"),
        CycleInput("c1", Money.of(100, "USD"), Money.of(0, "USD"), Money.of(10, "USD")),
    )
    return s


def test_payout_is_a_contract_bound_platform_call_that_passes_the_scope_check():
    op = build_payout_op(settlement(), BOB, book_id=BOOK, intent_hash=HASH)
    assert op.path == "/api/v1/connected_account_transfers/create"
    assert op.body["destination"] == "acct_bob000001"
    assert op.on_behalf_of is None  # the header would make the seller the payer
    check_tenant_scope(op, BOB)
    assert op.body["amount"] == Decimal("90.00")
    assert op.body["reference"].startswith("poc-i8-")
    assert op.body["reason"] == PAYOUT_REASON


def test_request_id_comes_from_the_payout_business_key_not_the_proposal():
    a = build_payout_op(settlement(), BOB, book_id=BOOK, intent_hash=HASH)
    b = build_payout_op(settlement(), BOB, book_id=BOOK, intent_hash="d" * 64)
    assert a.request_id == b.request_id
    assert a.business_key == "payout:book0001:bob:c1"


def test_two_books_seeded_the_same_way_have_disjoint_request_ids():
    """Rule M2: a fresh book can never adopt an earlier run's payouts or charges."""
    from awx_common.store import InMemoryKitStore
    from awx_k8_settlement import store as st

    books = []
    for _ in range(2):
        kstore = InMemoryKitStore()
        st.seed_seller(kstore, st.SellerProfile("bob", "USD", 30, Decimal("0.01")))
        books.append(st.book_id(kstore))
    assert books[0] != books[1]
    payouts = {payout_request_id(b, "bob", "c1") for b in books}
    charges = {recovery_request_id(b, "bob", 1) for b in books}
    assert len(payouts) == 2
    assert len(charges) == 2
    assert not payouts & charges
    with pytest.raises(ValueError, match="book id"):
        payout_request_id("", "bob", "c1")
    kstore = InMemoryKitStore()
    st.seed_seller(kstore, st.SellerProfile("bob", "USD", 30, Decimal("0.01")))
    with pytest.raises(Exception):  # a second mint is refused  # noqa: B017, PT011
        st.create_book(kstore)


def test_recovery_charge_names_the_seller_as_source_and_passes_the_scope_check():
    op = build_recovery_op(1, Money.of(5, "USD"), BOB, book_id=BOOK, intent_hash=HASH)
    assert op.path == "/api/v1/charges/create"
    assert op.body["source"] == "acct_bob000001"
    check_tenant_scope(op, BOB)
    assert op.body["reason"] == RECOVERY_REASON
    other = build_recovery_op(2, Money.of(5, "USD"), BOB, book_id=BOOK, intent_hash=HASH)
    assert other.request_id != op.request_id


def test_reason_values_are_sandbox_accepted():
    assert {PAYOUT_REASON, RECOVERY_REASON} <= ACCEPTED


def test_cross_tenant_movement_is_rejected():
    with pytest.raises(TenantScopeViolation):
        build_payout_op(settlement("bob"), ALICE, book_id=BOOK, intent_hash=HASH)
    op = build_payout_op(settlement(), BOB, book_id=BOOK, intent_hash=HASH)
    with pytest.raises(TenantScopeViolation):
        check_tenant_scope(op, ALICE)
    with pytest.raises(TenantScopeViolation):
        check_tenant_scope(replace(op, body={**op.body, "destination": "acct_alice0001"}), BOB)
    with pytest.raises(TenantScopeViolation):
        check_tenant_scope(replace(op, on_behalf_of="acct_bob000001"), BOB)


def test_platform_tenant_without_connected_account_cannot_move_money():
    with pytest.raises(TenantScopeViolation):
        build_payout_op(settlement(), TenantContext("bob", None), book_id=BOOK, intent_hash=HASH)
    with pytest.raises(TenantScopeViolation):
        seller_balance_request(TenantContext("bob", None))


def test_statuses_follow_fake_fidelity():
    assert read_outcome({"status": "NEW"}) is Outcome.IN_FLIGHT
    assert read_outcome({"status": "PENDING"}) is Outcome.IN_FLIGHT
    assert read_outcome({"status": "SETTLED"}) is Outcome.SETTLED
    assert read_outcome({"status": "FAILED"}) is Outcome.FAILED
    assert read_outcome({"status": "SUSPENDED"}) is Outcome.NEEDS_PERSON
    assert read_outcome({}) is Outcome.IN_FLIGHT


def _found(op, **over):
    return {
        "id": "tr_1",
        "request_id": op.request_id,
        "amount": op.body["amount"],
        "currency": "USD",
        "destination": op.body["destination"],
        "status": "SETTLED",
        **over,
    }


def test_reconcile_by_request_id_checks_the_object_matches():
    op = build_payout_op(settlement(), BOB, book_id=BOOK, intent_hash=HASH)
    assert find_by_request_id(op, lambda r: {"items": []}) is None
    assert find_by_request_id(op, lambda r: {"items": [_found(op)]})["id"] == "tr_1"
    for bad in ({"amount": Decimal("1")}, {"destination": "acct_x"}, {"currency": "EUR"}):
        with pytest.raises(ReconcileUndetermined):
            find_by_request_id(op, lambda r, b=bad: {"items": [_found(op, **b)]})
    with pytest.raises(ReconcileUndetermined):
        find_by_request_id(op, lambda r: {"oops": 1})


def test_duplicate_request_is_unknown_not_failed():
    op = build_payout_op(settlement(), BOB, book_id=BOOK, intent_hash=HASH)
    dup = OperationResult(op.request_id, OperationStatus.FAILED, error_type="DuplicateRequest")
    out, _ = resolve(op, dup, lambda r: {"items": [_found(op)]})
    assert out is Outcome.SETTLED
    out, _ = resolve(op, dup, lambda r: {"items": []})
    assert out is Outcome.UNKNOWN
    nsf = OperationResult(op.request_id, OperationStatus.FAILED, error_type="InsufficientFunds")
    assert resolve(op, nsf, None)[0] is Outcome.INSUFFICIENT_FUNDS
    # Kit rule M1: an empty read-back, with or without a reply, is never a failure.
    assert resolve(op, None, lambda r: {"items": []})[0] is Outcome.UNKNOWN
    assert resolve(op, None, None)[0] is Outcome.UNKNOWN
    assert read_outcome({"status": "CANCELLED"}) is Outcome.UNKNOWN
    assert read_outcome({"status": "CANCELLED", "failure": {"code": "x"}}) is Outcome.FAILED


def test_balance_parsing():
    body = {
        "items": [
            {"currency": "EUR", "available_amount": "5"},
            {"currency": "USD", "available_amount": Decimal("12.50")},
        ]
    }
    assert available_balance(body, "USD") == Money.of("12.50", "USD")
    assert available_balance(body, "GBP").is_zero()
    with pytest.raises(TypeError, match="float"):
        available_balance({"items": [{"currency": "USD", "available_amount": 1.5}]}, "USD")
    with pytest.raises(ValidationFailed):
        available_balance({"weird": 1}, "USD")


@pytest.mark.parametrize("error_type", ["RateLimited", "TransientServerError"])
def test_a_retryable_failed_is_never_definitive(error_type):
    """The kit's own defence, kept although the executor already reports this case uncertain."""
    op = build_payout_op(settlement(), BOB, book_id=BOOK, intent_hash=HASH)
    failed = OperationResult(op.request_id, OperationStatus.FAILED, 429, error_type=error_type)
    assert resolve(op, failed, lambda r: {"items": []})[0] is Outcome.UNKNOWN
    assert resolve(op, failed, None)[0] is Outcome.UNKNOWN


@pytest.mark.parametrize("status", [OperationStatus.OUTCOME_UNKNOWN, OperationStatus.FAILED])
def test_a_request_id_reused_mismatch_never_adopts_the_objects_status(status):
    """The object under our request_id belongs to another payment.
    Its status (SETTLED here) must never become ours: the money stays in flight."""
    op = build_payout_op(settlement(), BOB, book_id=BOOK, intent_hash=HASH)
    reused = OperationResult(
        op.request_id,
        status,
        200,
        provider_id="tr_1",
        error_type="StateConflict",
        error_code="request_id_reused",
    )
    other = _found(op, amount=Decimal("1"))
    outcome, _ = resolve(
        op, reused, lambda r: other if r.path.endswith("tr_1") else {"items": [other]}
    )
    assert outcome is Outcome.UNKNOWN
