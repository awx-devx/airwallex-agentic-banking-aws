"""Tenant map, binding paths, tool/spec contract, validation, finality, reconcile and reads."""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

import pytest
from awx_common import (
    PLATFORM_BODY_FIELDS,
    SERVER_OWNED_FIELDS,
    OperationResult,
    OperationStatus,
    ReadRequest,
    TenantContext,
    ToolKind,
    check_tenant_scope,
    derive_request_id,
)
from awx_common.errors import StateConflict, TenantScopeViolation
from awx_common.store import InMemoryKitStore
from awx_common.workflow import PolicyContext, ReconcileUndetermined
from awx_k5_platform_spend import READS, SPECS, TOOLS, TenantMap, TenantRecord
from awx_k5_platform_spend.amounts import available_balance, provider_decimal
from awx_k5_platform_spend.finality import (
    BRIDGE_FINALITY,
    CARD_FINALITY,
    MOVEMENT_TERMINAL,
    CardTxnOutcome,
    CreateOutcome,
    bridge_reconcile,
    card_transaction_outcome,
    interpret_create,
    simulation_reconcile,
)
from awx_k5_platform_spend.operations import (
    bridge_operation,
    card_simulation_operation,
    cardholder_email,
    cardholder_operation,
    charge_operation,
    merchant_tag,
)
from awx_k5_platform_spend.reads import READ_ALLOWLIST
from awx_k5_platform_spend.tenants import account_strings, bind_customer_operation, redact_accounts
from awx_k5_platform_spend.validation import InvalidIntent
from awx_k5_platform_spend.workflow import APPROVAL_MATRIX, configure_platform, current_setup
from k5_support import ACCT, NOW, Platform, make_book, usd

TENANTS = make_book().tenants
ALPHA = TENANTS.resolve("t-alpha")
HASH = "c" * 64
BOOK = "b00c0000000000000000000000000001"
SENT = NOW


def _ops(tenant):
    return [
        bridge_operation(
            tenant,
            TENANTS,
            book_id=BOOK,
            obligation_id="o",
            amount=usd("1"),
            step="create",
            intent_hash=HASH,
        ),
        charge_operation(
            tenant,
            TENANTS,
            book_id=BOOK,
            kind="fee",
            ref="2026-10",
            amount=usd("25"),
            intent_hash=HASH,
        ),
        charge_operation(
            tenant,
            TENANTS,
            book_id=BOOK,
            kind="repayment",
            ref="r",
            amount=usd("1"),
            intent_hash=HASH,
        ),
        cardholder_operation(tenant, TENANTS, book_id=BOOK, cardholder_ref="c", intent_hash=HASH),
        card_simulation_operation(
            tenant,
            TENANTS,
            book_id=BOOK,
            scenario_ref="s",
            card_id="card_1",
            amount=usd("1"),
            merchant_category_code="5734",
            not_before=SENT,
            intent_hash=HASH,
        ),
    ]


# ---- tenant map ------------------------------------------------------------------------


def test_tenant_map_is_one_to_one_and_resolves_server_side():
    assert ALPHA == TenantContext("t-alpha", ACCT["t-alpha"])
    with pytest.raises(TenantScopeViolation):
        TENANTS.resolve("t-unknown")
    with pytest.raises(ValueError, match="one tenant only"):
        TenantMap([TenantRecord("a", "acct_x", "n"), TenantRecord("b", "acct_x", "n")])
    with pytest.raises(ValueError, match="duplicate"):
        TenantMap([TenantRecord("a", "acct_x", "n"), TenantRecord("a", "acct_y", "n")])
    with pytest.raises(ValueError, match="acct_"):
        TenantRecord("a", "not-an-account", "n")
    with pytest.raises(ValueError, match="platform"):
        TenantRecord("platform", "acct_p", "n")


def test_forged_context_is_refused():
    with pytest.raises(TenantScopeViolation):
        TENANTS.record(TenantContext("t-alpha", ACCT["t-bravo"]))
    with pytest.raises(TenantScopeViolation):
        TENANTS.record(TenantContext("t-alpha", None))


def test_platform_movements_use_bind_platform_operation_fields():
    op = bridge_operation(
        ALPHA,
        TENANTS,
        book_id=BOOK,
        obligation_id="ob-1",
        amount=usd("12.50"),
        step="create",
        intent_hash=HASH,
    )
    assert op.on_behalf_of is None
    assert op.body[PLATFORM_BODY_FIELDS[op.path]] == ACCT["t-alpha"]
    assert op.body["amount"] == Decimal("12.50")
    assert op.body["reference"].startswith("poc-k5-")
    assert op.request_id == derive_request_id("k5", f"t-alpha:{BOOK}:bridge:ob-1", "create")
    charge = charge_operation(
        ALPHA, TENANTS, book_id=BOOK, kind="fee", ref="2026-10", amount=usd("25"), intent_hash=HASH
    )
    assert charge.body[PLATFORM_BODY_FIELDS[charge.path]] == ACCT["t-alpha"]
    assert charge.on_behalf_of is None
    assert charge.body["reason"] == "professional_business_services"


@pytest.mark.parametrize("tenant_id", ["t-alpha", "t-bravo", "t-charlie"])
def test_every_emitted_operation_passes_check_tenant_scope_and_names_only_its_tenant(tenant_id):
    tenant = TENANTS.resolve(tenant_id)
    for op in _ops(tenant):
        check_tenant_scope(op, tenant)
        assert account_strings(op.body) | ({op.on_behalf_of} - {None}) <= {ACCT[tenant_id]}
        other = TENANTS.resolve("t-bravo" if tenant_id != "t-bravo" else "t-alpha")
        with pytest.raises(TenantScopeViolation):
            check_tenant_scope(op, other)


def test_customer_operations_refuse_foreign_accounts_and_platform_context():
    op = _ops(ALPHA)[-1]
    assert op.on_behalf_of == ACCT["t-alpha"]
    with pytest.raises(TenantScopeViolation):
        bind_customer_operation(replace(op, on_behalf_of=ACCT["t-bravo"]), ALPHA, TENANTS)
    with pytest.raises(TenantScopeViolation):
        bind_customer_operation(
            replace(op, body={**op.body, "card_id": ACCT["t-bravo"]}), ALPHA, TENANTS
        )
    with pytest.raises(TenantScopeViolation):
        bind_customer_operation(op, TenantContext("platform"), TENANTS)


def test_request_ids_come_from_business_keys():
    a = _ops(ALPHA)
    b = _ops(ALPHA)
    assert [o.request_id for o in a] == [o.request_id for o in b]
    assert all(o.business_key.startswith("t-alpha:") for o in a)
    other_amount = bridge_operation(
        ALPHA,
        TENANTS,
        book_id=BOOK,
        obligation_id="o",
        amount=usd("2"),
        step="create",
        intent_hash="d" * 64,
    )
    assert other_amount.request_id == a[0].request_id  # same object, same request_id


def test_a_fresh_book_never_reuses_an_old_books_request_ids():
    """M2: two books seeded with the same references produce disjoint request_ids and tags."""
    other = bridge_operation(
        ALPHA,
        TENANTS,
        book_id="f" * 32,
        obligation_id="o",
        amount=usd("1"),
        step="create",
        intent_hash=HASH,
    )
    assert other.request_id != _ops(ALPHA)[0].request_id
    assert merchant_tag(ALPHA, BOOK, "s") != merchant_tag(ALPHA, "f" * 32, "s")
    with pytest.raises(ValueError, match="book_id"):
        bridge_operation(
            ALPHA,
            TENANTS,
            book_id="",
            obligation_id="o",
            amount=usd("1"),
            step="create",
            intent_hash=HASH,
        )


def test_server_derived_identifiers_fit_the_sandbox():
    assert cardholder_email(ALPHA, BOOK, "x").endswith("@example.com")
    bravo = TENANTS.resolve("t-bravo")
    assert cardholder_email(ALPHA, BOOK, "x") != cardholder_email(bravo, BOOK, "x")
    tag = merchant_tag(ALPHA, BOOK, "a" * 48)
    assert len(tag) <= 25
    assert "_" not in tag


def test_redact_accounts_strips_every_account_identifier():
    body = {
        "a": "acct_x",
        "destination": "x",
        "nested": [{"account_id": "1", "ok": 1}, "acct_y", "fine"],
    }
    assert redact_accounts(body) == {"nested": [{"ok": 1}, "fine"]}


# ---- contract ----------------------------------------------------------------------------


def test_every_propose_tool_has_a_spec_and_every_read_tool_a_read():
    proposes = {t.name for t in TOOLS if t.kind is ToolKind.PROPOSE}
    reads = {t.name for t in TOOLS if t.kind is ToolKind.READ}
    assert proposes == {s.tool_name for s in SPECS.values()}
    assert reads == set(READS)
    assert set(APPROVAL_MATRIX) == set(SPECS)
    assert all(spec.kit == "k5" for spec in SPECS.values())


def test_platform_reads_are_on_the_platform_allowlist_only():
    for action in ("bridge_shortfall", "collect_fee", "collect_repayment"):
        spec = SPECS[action]
        base = (
            "/api/v1/connected_account_transfers"
            if action == "bridge_shortfall"
            else "/api/v1/charges"
        )
        assert spec.allows_platform_read(f"{base}/x1")
        assert spec.allows_platform_read(base)
        assert not spec.allows_read(f"{base}/x1")


def test_every_step_that_can_be_unknown_has_a_reconcile_hook():
    for spec in SPECS.values():
        for step in spec.steps:
            if step.name not in ("register", "apply"):  # these send no operations
                assert step.reconcile is not None, (spec.action, step.name)


def _names(schema):
    out = set()
    for k, v in (schema.get("properties") or {}).items():
        out.add(k)
        if isinstance(v, dict):
            out |= _names(v)
    return out


def test_no_tool_exposes_a_server_owned_or_computed_field():
    computed = {
        "amount_due",
        "shortfall",
        "fee",
        "destination",
        "source",
        "limit",
        "email",
        "first_name",
    }
    for tool in TOOLS:
        names = _names(tool.input_schema)
        assert not names & SERVER_OWNED_FIELDS, tool.name
        assert not names & computed, tool.name
        assert not any("acct" in n for n in names)


def test_specs_fail_closed_without_setup_or_platform_store():
    configure_platform(None)
    with pytest.raises(RuntimeError, match="no PlatformSetup"):
        current_setup()
    spec = SPECS["collect_fee"]
    # No setup is a stated BLOCK, never a raw exception into the tool plane.
    decision = spec.policy({"period": "2026-10"}, PolicyContext(ALPHA, NOW, Platform().fake.read))
    assert decision.reasons == ("kit 5 is not configured yet",)
    configure_platform(make_book().setup)
    try:
        with pytest.raises(StateConflict, match="platform_store"):
            spec.policy({"period": "2026-10"}, PolicyContext(ALPHA, NOW, Platform().fake.read))
    finally:
        configure_platform(None)


def test_no_process_global_book_state():
    """Two runs over two platform stores see two independent ledgers."""
    import awx_k5_platform_spend.workflow as wf

    assert not hasattr(wf, "_BOOK")
    setup = make_book().setup
    a, b = InMemoryKitStore(), InMemoryKitStore()
    wf.book_for(
        PolicyContext(ALPHA, NOW, Platform().fake.read, platform_store=a), setup
    ).ledger.fund("cap", usd("5"), at=NOW)
    view = wf.book_for(PolicyContext(ALPHA, NOW, Platform().fake.read, platform_store=b), setup)
    assert view.ledger.cash == usd("0")
    assert view.book_id is None


@pytest.mark.parametrize(
    ("action", "raw"),
    [
        ("bridge_shortfall", {"obligation_ref": "ob-1", "amount": "100"}),
        ("bridge_shortfall", {"obligation_ref": "ob-1", "connected_account_id": "acct_x"}),
        ("bridge_shortfall", {"obligation_ref": "ob-1", "tenant_id": "t-bravo"}),
        ("bridge_shortfall", {"obligation_ref": "ob-1", "request_id": "r"}),
        ("bridge_shortfall", {"obligation_ref": "../x"}),
        ("register_shortfall", {"obligation_ref": "ob-1", "shortfall": "1"}),
        (
            "simulate_card_spend",
            {
                "scenario_ref": "s",
                "card_id": "c",
                "amount": 10.5,
                "currency": "USD",
                "merchant_category_code": "5734",
            },
        ),
        (
            "simulate_card_spend",
            {
                "scenario_ref": "s",
                "card_id": "c",
                "amount": "10.555",
                "currency": "USD",
                "merchant_category_code": "5734",
            },
        ),
        ("collect_fee", {"period": "2026-13"}),
        ("collect_repayment", {"amount": "1"}),
        ("issue_card", {"cardholder_ref": "a", "program": "standard", "email": "a@b.co"}),
    ],
)
def test_validation_rejects_bad_or_owned_input(action, raw):
    with pytest.raises(InvalidIntent):
        SPECS[action].validate(raw)


# ---- amounts, finality and reconcile -----------------------------------------------------


def test_amounts_never_accept_floats():
    with pytest.raises(TypeError):
        provider_decimal(1.5)
    with pytest.raises(TypeError):
        provider_decimal(True)
    assert provider_decimal("10.10") == Decimal("10.10")
    assert available_balance({"items": [{"currency": "USD", "available_amount": 5}]}, "USD") == usd(
        "5"
    )
    assert available_balance({"items": []}, "USD") == usd("0")
    with pytest.raises(ValueError, match="twice"):
        available_balance({"items": [{"currency": "USD"}, {"currency": "USD"}]}, "USD")


def test_new_is_not_final_and_only_settled_succeeds():
    assert "NEW" not in BRIDGE_FINALITY.terminal
    assert "CANCELLED" not in MOVEMENT_TERMINAL
    assert {"SETTLED", "FAILED", "SUSPENDED"} == MOVEMENT_TERMINAL
    assert BRIDGE_FINALITY.success == frozenset({"SETTLED"})
    req = BRIDGE_FINALITY.read_request(
        OperationResult("r", OperationStatus.SUCCEEDED, provider_id="tr_1")
    )
    assert req == ReadRequest(
        path="/api/v1/connected_account_transfers", query={"request_id": "r"}
    )  # polled by request_id: a create can stay out of GET-by-id reads for a while
    assert BRIDGE_FINALITY.status_of({"items": []}) == "NOT_VISIBLE"
    assert (
        BRIDGE_FINALITY.status_of({"items": [{"status": "NEW"}, {"status": "NEW"}]}) == "AMBIGUOUS"
    )
    assert BRIDGE_FINALITY.status_of({"items": [{"status": "SETTLED"}]}) == "SETTLED"
    assert (
        CARD_FINALITY.read_request(
            OperationResult("r", OperationStatus.SUCCEEDED, provider_id="c")
        ).on_behalf_of
        is None
    )
    with pytest.raises(ValueError, match="reconcile"):
        CARD_FINALITY.read_request(OperationResult("r", OperationStatus.OUTCOME_UNKNOWN))


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (OperationResult("r", OperationStatus.SUCCEEDED), CreateOutcome.ACCEPTED),
        (OperationResult("r", OperationStatus.OUTCOME_UNKNOWN), CreateOutcome.UNKNOWN),
        (
            OperationResult("r", OperationStatus.FAILED, error_type="DuplicateRequest"),
            CreateOutcome.DUPLICATE,
        ),
        (
            OperationResult("r", OperationStatus.FAILED, error_type="InsufficientFunds"),
            CreateOutcome.REJECTED,
        ),
        (
            OperationResult("r", OperationStatus.FAILED, error_type="TransientServerError"),
            CreateOutcome.UNKNOWN,
        ),
    ],
)
def test_interpret_create(result, expected):
    assert interpret_create(result) is expected


def test_bridge_reconcile_uses_the_request_id_filter_and_checks_the_object():
    op = _ops(ALPHA)[0]
    row = {
        "request_id": op.request_id,
        "id": "tr_1",
        "amount": Decimal("1.0"),
        "currency": "USD",
        "destination": ACCT["t-alpha"],
    }
    seen = []

    def read(req):
        seen.append(req)
        return {"items": [row]}

    assert bridge_reconcile(op, read) == row
    assert seen[0].query == {"request_id": op.request_id}
    assert bridge_reconcile(op, lambda req: {"items": []}) is None
    with pytest.raises(ReconcileUndetermined):
        bridge_reconcile(op, lambda req: {"items": [{**row, "amount": Decimal("2")}]})


def _txn(op, at):
    return {
        "merchant": {"name": op.body["merchant_info"].ljust(25, "_")},
        "transaction_amount": Decimal("-1"),
        "transaction_currency": "USD",
        "transaction_date": at.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
    }


def test_simulation_reconcile_never_reports_absent():
    op = _ops(ALPHA)[-1]
    with pytest.raises(ReconcileUndetermined):
        simulation_reconcile(op, lambda req: {"items": []})
    row = _txn(op, SENT)
    assert simulation_reconcile(op, lambda req: {"items": [row]}) == row


def test_simulation_reconcile_refuses_an_authorisation_older_than_the_send():
    from datetime import timedelta

    op = _ops(ALPHA)[-1]
    old = _txn(op, SENT - timedelta(hours=1))  # an earlier run's authorisation, same tag
    with pytest.raises(ReconcileUndetermined, match="not found"):
        simulation_reconcile(op, lambda req: {"items": [old]})
    no_time = {k: v for k, v in _txn(op, SENT).items() if k != "transaction_date"}
    with pytest.raises(ReconcileUndetermined, match="time"):
        simulation_reconcile(op, lambda req: {"items": [no_time]})


def test_simulation_reconcile_pages_to_the_end_and_fails_closed_past_the_bound():
    op = _ops(ALPHA)[-1]
    filler = {"merchant": {"name": "other"}, "transaction_amount": Decimal("-9")}

    def paged(req):
        page = int(req.query["page_num"])
        if page < 3:
            return {"items": [filler], "has_more": True}
        return {"items": [_txn(op, SENT)], "has_more": False}

    assert simulation_reconcile(op, paged)["transaction_currency"] == "USD"
    with pytest.raises(ReconcileUndetermined, match="page bound"):
        simulation_reconcile(op, lambda req: {"items": [filler], "has_more": True})


def test_card_transaction_vocabulary():
    reply_ok = {"process_result": "APPROVED", "type": "CLEARING"}
    reply_no = {"process_result": "DECLINED", "failure_reason": "LIMIT_EXCEEDED"}
    assert card_transaction_outcome(reply_ok) == (CardTxnOutcome.CLEARED, None)
    assert card_transaction_outcome(reply_no) == (CardTxnOutcome.DECLINED, "LIMIT_EXCEEDED")
    assert (
        card_transaction_outcome({"transaction_type": "CLEARING", "status": "APPROVED"})[0]
        is CardTxnOutcome.CLEARED
    )
    assert (
        card_transaction_outcome({"transaction_type": "AUTHORIZATION", "status": "PENDING"})[0]
        is CardTxnOutcome.PENDING
    )
    assert (
        card_transaction_outcome({"process_result": "APPROVED", "type": "AUTHORIZATION"})[0]
        is CardTxnOutcome.PENDING
    )


# ---- read tools --------------------------------------------------------------------------


def test_read_tools_are_tenant_bound_and_leak_no_accounts():
    p = Platform(wallets={"t-alpha": "12.30", "t-bravo": "999"})
    configure_platform(p.setup)
    try:
        alpha = p.tenant("t-alpha")
        ctx = PolicyContext(
            alpha, NOW, p.fake.read, store=InMemoryKitStore(), platform_store=p.platform_store
        )
        summary = READS["read_tenant_summary"]({}, ctx)
        assert summary == {
            "balances": [
                {"currency": "USD", "available": "12.30", "pending": "0.00", "total": "12.30"}
            ]
        }
        assert READS["read_cards"]({}, ctx) == {"cards": []}
        assert READS["read_card_transactions"]({"card_id": "card_1"}, ctx) == {"transactions": []}
        position = READS["read_bridge_position"]({}, ctx)
        assert set(position) == {"receivable", "bridges_in_flight", "queued_shortfalls"}
        assert "acct_" not in str(position)
        reads = [r for r in p.fake.requests if r[0] == "GET"]
        assert reads
        assert {r[2] for r in reads} == {p.acct["t-alpha"]}
        assert all(any(re.match(pat, r[1]) for pat in READ_ALLOWLIST) for r in reads)
        with pytest.raises(InvalidIntent):
            READS["read_cards"]({"tenant_id": "t-bravo"}, ctx)
        with pytest.raises(TenantScopeViolation):
            READS["read_cards"](
                {},
                PolicyContext(
                    TenantContext("platform"), NOW, p.fake.read, platform_store=p.platform_store
                ),
            )
    finally:
        configure_platform(None)


@pytest.mark.parametrize(
    "raw",
    ["2026-10-06T04:49:06.196+0000", "2026-10-06T04:49:06+0000", "2026-10-06T04:49:06+00:00"],
)
def test_transaction_time_parses_sandbox_and_fake_formats(raw):
    from datetime import UTC, datetime

    from awx_k5_platform_spend.finality import _transaction_time

    got = _transaction_time({"transaction_date": raw})
    assert got is not None
    assert got.replace(microsecond=0) == datetime(2026, 10, 6, 4, 49, 6, tzinfo=UTC)
    assert _transaction_time({"transaction_date": "not a date"}) is None
