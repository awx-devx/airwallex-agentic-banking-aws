"""Kit 5 scenarios through the real runner (local_runner over FakeAirwallex)."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from awx_common import OperationResult, OperationStatus
from awx_common.approvals import Origin, RiskTier
from awx_common.errors import StateConflict
from awx_k5_platform_spend import ProviderMovement
from awx_k5_platform_spend.amounts import provider_money
from awx_k5_platform_spend.operations import request_id_for
from awx_spine.fake import Fault
from k5_support import (
    LABEL,
    CrashAfterSend,
    FailingReads,
    Platform,
    approve,
    expire,
    obligation,
    reject,
    usd,
)

CARD = {"cardholder_ref": "ops-lead", "program": "standard"}
BRIDGE = "connected_account_transfers.create"
CHARGE = "charges.create"
SIM = "issuing.simulate_authorization"


def sim(scenario: str, card_id: str, amount: str, currency: str = "USD", mcc: str = "5734"):
    return {
        "scenario_ref": scenario,
        "card_id": card_id,
        "amount": amount,
        "currency": currency,
        "merchant_category_code": mcc,
    }


def issue(p: Platform, tenant: str = "t-alpha", ref: str = "ops-lead") -> str:
    out = p.run("issue_card", {**CARD, "cardholder_ref": ref}, tenant=tenant)
    assert out.state == "SUCCEEDED", (out.state, out.detail, out.postcondition)
    return out.results["create_card"][0].provider_id


def provider_movements(p: Platform) -> list[ProviderMovement]:
    out = []
    for kind, field, label in (
        ("connected_account_transfers", "destination", "connected_account_transfer"),
        ("charges", "source", "charge"),
    ):
        for o in p.movements(kind):
            amount = provider_money(o["amount"], o["currency"])
            out.append(
                ProviderMovement(o["id"], o["request_id"], label, o["status"], amount, o[field])
            )
    return out


def bridged(p: Platform, ob: str = "ob-a1", tenant: str = "t-alpha"):
    return p.run("bridge_shortfall", {"obligation_ref": ob}, tenant=tenant)


def assert_stale(out) -> None:
    """Rule 1 through the engine: state FAILED, flagged stale, on_abandon(STALE) ran."""
    assert out.state == "FAILED"
    assert "changed since the proposal" in (out.detail or "")


def alpha_platform(**kw) -> Platform:
    return Platform(obligations=(obligation("t-alpha", "ob-a1", "500"),), **kw)


# ---- the kit's acceptance test ---------------------------------------------------------


def acceptance_scenario(p: Platform) -> None:
    """The kit 5 acceptance scenario.

    With a binding platform reserve: issue a customer card over the server-side tenant path,
    bridge one shortfall, settle the movement, collect an approved fee, and prove a second
    advance is denied because it would breach the floor. Replay of the card simulation must
    not create a second transaction.
    """
    alpha = p.acct["t-alpha"]
    card = p.run("issue_card", CARD, tenant="t-alpha")
    assert card.state == "SUCCEEDED", card.detail
    assert card.proposal.risk_tier is RiskTier.SINGLE
    issuing = [r for r in p.fake.requests if "/issuing/" in r[1] and r[0] == "POST"]
    assert {r[2] for r in issuing} == {alpha}  # every issuing write over the tenant's header
    card_id = card.results["create_card"][0].provider_id

    ok = p.run("simulate_card_spend", sim("spend-1", card_id, "40.00"), tenant="t-alpha")
    over = p.run("simulate_card_spend", sim("spend-2", card_id, "100.01"), tenant="t-alpha")
    mcc = p.run(
        "simulate_card_spend", sim("spend-3", card_id, "10.00", mcc="5812"), tenant="t-alpha"
    )
    assert ok.postcondition.detail == "CLEARED"
    assert over.postcondition.detail == "DECLINED (LIMIT_EXCEEDED)"
    assert mcc.postcondition.detail == "DECLINED (MERCHANT_CATEGORY_NOT_ALLOWED)"

    bridge = bridged(p)  # due 500, wallet 160 after spend: 340 from platform capital
    assert bridge.state == "SUCCEEDED", (bridge.detail, bridge.postcondition)
    assert bridge.proposal.computed["amount"] == usd("340")
    (transfer,) = p.movements("connected_account_transfers")
    assert transfer["destination"] == alpha
    assert transfer["status"] == "SETTLED"
    assert p.book.ledger.receivable("t-alpha") == usd("340")

    fee = p.run("collect_fee", {"period": "2026-10"}, tenant="t-alpha")
    assert fee.state == "SUCCEEDED"
    assert fee.proposal.risk_tier is RiskTier.SINGLE
    assert p.book.ledger.fee_revenue() == usd("25")

    second = bridged(p, "ob-b1", "t-bravo")  # headroom 685 - 600 = 85 < 250
    assert second.state == "BLOCKED"
    assert second.proposal.reasons == ("bridge denied: would breach the reserve floor",)
    assert p.book.ledger.cash - p.book.ledger.held >= p.setup.config.reserve_floor
    assert p.wallet(None) - p.setup.config.reserve_floor >= usd("0")

    sims = len(p.sent(SIM))
    replay = p.run("simulate_card_spend", sim("spend-1", card_id, "40.00"), tenant="t-alpha")
    assert replay.state == "BLOCKED"
    assert len(p.sent(SIM)) == sims  # no second authorisation

    p.advance(seconds=5)
    assert all(m["status"] == "SETTLED" for m in provider_movements_raw(p))
    pending = [t for t in p.fake.objects("card_transactions") if t["status"] == "PENDING"]
    assert pending == []  # nothing left reserved on the customer wallet
    p.book.ledger.check_invariants()
    assert p.book.ledger.reconcile(provider_movements(p), accounts=p.acct) == []


def provider_movements_raw(p: Platform):
    return [*p.movements("connected_account_transfers"), *p.movements("charges")]


def acceptance_platform() -> Platform:
    return Platform(
        obligations=(obligation("t-alpha", "ob-a1", "500"), obligation("t-bravo", "ob-b1", "400"))
    )


def test_acceptance_binding_reserve_card_bridge_settle_fee_second_advance_denied_and_replay_safe():
    acceptance_scenario(acceptance_platform())


@pytest.mark.sandbox
def test_acceptance_sandbox():
    """The sandbox acceptance entry point. It runs the scenario against FakeAirwallex; no live
    wiring is committed, so with `AWX_K5_LIVE` set it skips (DESIGN.md, "Live sandbox run")."""
    if os.environ.get("AWX_K5_LIVE"):
        pytest.skip("live wiring for kit 5 is not committed")
    acceptance_scenario(acceptance_platform())


# ---- cards and simulations --------------------------------------------------------------


def test_simulation_body_matches_the_sandbox_shape():
    p = Platform()
    card_id = issue(p)
    out = p.run("simulate_card_spend", sim("s-1", card_id, "5.00"), tenant="t-alpha")
    (sent,) = out.results["simulate"]
    assert sent.status is OperationStatus.SUCCEEDED
    assert sent.body["process_result"] == "APPROVED"
    (txn,) = p.fake.objects("card_transactions")
    assert txn["merchant"]["category_code"] == "5734"
    assert txn["transaction_type"] == "CLEARING"  # single_phase
    holder = p.fake.objects("cardholders")[0]
    assert holder["type"] == "DELEGATE"
    assert holder["email"].startswith("poc-k5-")


def test_simulation_timeout_after_submit_reconciles_and_creates_exactly_one_transaction():
    p = Platform()
    card_id = issue(p)
    p.fake.inject(SIM, Fault.TIMEOUT_AFTER_SUBMIT)
    out = p.run("simulate_card_spend", sim("s-1", card_id, "40.00"), tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition.detail == "CLEARED"
    assert len(p.fake.objects("card_transactions")) == 1
    assert len(p.sent(SIM)) == 1


def test_simulation_unknown_and_invisible_is_never_resent():
    p = Platform()
    card_id = issue(p)
    p.fake.inject(SIM, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    out = p.run("simulate_card_spend", sim("s-1", card_id, "40.00"), tenant="t-alpha")
    assert out.state == "FAILED"
    assert out.postcondition.detail == "simulation outcome unknown; not resent"
    assert len(p.sent(SIM)) == 1
    again = p.run("simulate_card_spend", sim("s-1", card_id, "40.00"), tenant="t-alpha")
    assert again.state == "BLOCKED"
    assert len(p.sent(SIM)) == 1


def test_simulation_500_that_still_applies_is_found_not_resent():
    p = Platform()
    card_id = issue(p)
    p.fake.inject(SIM, Fault.SERVER_ERROR, applied=True)
    out = p.run("simulate_card_spend", sim("s-1", card_id, "40.00"), tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert len(p.fake.objects("card_transactions")) == 1
    assert len(p.sent(SIM)) == 1


def test_simulation_only_on_cards_this_kit_issued_and_within_the_cap():
    p = Platform()
    alpha_card = issue(p)
    blocked = p.run("simulate_card_spend", sim("x", alpha_card, "1.00"), tenant="t-bravo")
    assert blocked.state == "BLOCKED"
    assert blocked.proposal.reasons == ("card was not issued by this kit for this tenant",)
    capped = p.run("simulate_card_spend", sim("y", alpha_card, "101.01"), tenant="t-alpha")
    assert capped.state == "BLOCKED"
    assert p.sent(SIM) == []


def test_model_started_simulation_needs_approval_but_a_scheduled_one_does_not():
    p = Platform()
    card_id = issue(p)
    agent = p.run("simulate_card_spend", sim("a", card_id, "1.00"), tenant="t-alpha", decide=reject)
    assert agent.proposal.risk_tier is RiskTier.SINGLE
    assert agent.state == "REJECTED"
    sched = p.run(
        "simulate_card_spend",
        sim("b", card_id, "1.00"),
        tenant="t-alpha",
        origin=Origin.SCHEDULE,
        decide=reject,
    )
    assert sched.proposal.risk_tier is RiskTier.AUTO
    assert sched.state == "SUCCEEDED"


def test_card_is_issued_once_per_cardholder_ref():
    p = Platform()
    issue(p)
    assert p.run("issue_card", CARD, tenant="t-alpha").state == "BLOCKED"
    assert len(p.fake.objects("cards")) == 1


def test_cardholder_timeout_reconciles_by_email():
    p = Platform()
    p.fake.inject("issuing.cardholders.create", Fault.TIMEOUT_AFTER_SUBMIT)
    out = p.run("issue_card", CARD, tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert len(p.fake.objects("cardholders")) == 1
    assert len(p.fake.objects("cards")) == 1


def test_a_reset_tenant_store_gets_a_new_book_and_never_adopts_old_objects():
    from awx_common.store import InMemoryKitStore

    p = Platform()
    old = issue(p)
    p.run("simulate_card_spend", sim("s-1", old, "5.00"), tenant="t-alpha")
    p.tenant_stores["t-alpha"] = InMemoryKitStore()  # demo re-seed
    new = issue(p)
    assert new != old
    out = p.run("simulate_card_spend", sim("s-1", new, "5.00"), tenant="t-alpha")
    assert out.state == "SUCCEEDED"
    emails = [h["email"] for h in p.fake.objects("cardholders")]
    assert len(set(emails)) == 2
    tags = {t["merchant"]["name"] for t in p.fake.objects("card_transactions")}
    assert len(tags) == 2


# ---- bridges ----------------------------------------------------------------------------


def test_policy_is_pure_under_the_real_runner():
    """The engine hands policies a read-only store and fails a write as PurityViolation."""
    p = alpha_platform()
    for action, raw, decide in (
        ("bridge_shortfall", {"obligation_ref": "ob-a1"}, reject),
        ("collect_fee", {"period": "2026-10"}, reject),
        ("issue_card", CARD, reject),
        ("reconcile_bridge", {"obligation_ref": "ob-a1"}, reject),
    ):
        out = p.run(action, raw, tenant="t-alpha", decide=decide)
        assert "PurityViolation" not in (out.detail or "")
    assert p.fake.requests == [r for r in p.fake.requests if r[0] == "GET"]


def test_register_shortfall_zero_operation_step_runs_through_the_engine():
    p = alpha_platform()
    out = p.run("register_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha", decide=reject)
    assert out.proposal.risk_tier is RiskTier.AUTO  # agent floor lowered; DESIGN.md justifies it
    assert out.state == "SUCCEEDED", out.detail
    assert out.results["register"] == ()
    assert p.book.queue_entry(("t-alpha", "ob-a1"))[0].shortfall == usd("300")
    assert [r for r in p.fake.requests if r[0] == "POST"] == []


def test_rejected_higher_priority_request_stops_blocking_the_next_one():
    p = Platform(
        obligations=(obligation("t-alpha", "ob-a1", "350"), obligation("t-bravo", "ob-b1", "400"))
    )
    p.book.ledger.place_hold("other", "t-charlie", usd("100"), owner="x")  # headroom 300
    assert (
        p.run("register_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha").state
        == "SUCCEEDED"
    )
    assert (
        p.run("register_shortfall", {"obligation_ref": "ob-b1"}, tenant="t-bravo").state
        == "SUCCEEDED"
    )
    denied = bridged(p, "ob-b1", "t-bravo")
    assert denied.proposal.reasons == ("bridge denied: would breach the reserve floor",)
    alpha = p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha", decide=reject)
    assert alpha.state == "REJECTED"
    assert p.book.queue_entry(("t-alpha", "ob-a1")) is None  # on_abandon dropped the place
    assert bridged(p, "ob-b1", "t-bravo").state == "SUCCEEDED"
    assert p.book.ledger.receivables() == {"t-bravo": usd("250")}


def test_stale_queue_places_do_not_ration_capital():
    p = Platform(
        obligations=(obligation("t-alpha", "ob-a1", "350"), obligation("t-bravo", "ob-b1", "400"))
    )
    p.book.ledger.place_hold("other", "t-charlie", usd("100"), owner="x")
    p.run("register_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    p.advance(hours=5)  # beyond the 4 h bridge approval TTL, same UTC day
    assert bridged(p, "ob-b1", "t-bravo").state == "SUCCEEDED"


def test_platform_wallet_below_the_books_binds_the_floor():
    """The wallet holds less than the ledger thinks (other kits, manual moves): the floor
    binds on the observed balance."""
    p = alpha_platform(platform_usd="700")  # wallet room 100 < shortfall 300
    out = bridged(p)
    assert out.state == "BLOCKED"
    assert out.proposal.reasons == ("bridge denied: would breach the reserve floor",)
    assert p.sent(BRIDGE) == []


def test_tenant_mismatch_never_reaches_another_tenants_account():
    p = Platform(obligations=(obligation("t-bravo", "ob-b1", "400"),))
    from awx_common.errors import TenantScopeViolation

    with pytest.raises(TenantScopeViolation):  # the propose step refuses; nothing starts
        p.run("bridge_shortfall", {"obligation_ref": "ob-b1"}, tenant="t-alpha")
    issue(p)
    bravo = p.acct["t-bravo"]
    assert all(r[2] != bravo for r in p.fake.requests)


def test_capital_consumed_after_approval_fails_stale():
    p = alpha_platform()

    def decide(prop):
        p.book.ledger.place_hold("other", "t-charlie", usd("200"), owner="x")
        return approve(prop)

    out = p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha", decide=decide)
    assert_stale(out)
    assert p.sent(BRIDGE) == []


def test_deposit_landing_after_approval_fails_stale():
    p = alpha_platform()

    def decide(prop):
        p.deposit("t-alpha", "50")
        return approve(prop)

    out = p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha", decide=decide)
    assert_stale(out)
    assert p.book.ledger.holds() == ()


def test_unrelated_capital_change_does_not_void_an_approval():
    p = alpha_platform()

    def decide(prop):
        p.book.ledger.fund("capital-2", usd("5"), at=p.now())
        return approve(prop)

    assert (
        p.run(
            "bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha", decide=decide
        ).state
        == "SUCCEEDED"
    )


def test_high_risk_bridge_needs_two_approvers():
    p = Platform(
        platform_usd="2000",
        capital="2000",
        floor="100",
        high_risk="200",
        obligations=(obligation("t-alpha", "ob-a1", "500"),),
    )
    out = p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha", decide=reject)
    assert out.proposal.risk_tier is RiskTier.DUAL
    assert out.state == "REJECTED"
    assert bridged(p).state == "SUCCEEDED"  # two distinct approvers


@pytest.mark.parametrize(
    ("obligations", "reason"),
    [
        ((), "no such obligation for this tenant"),
        (
            (obligation("t-alpha", "ob-a1", "150"),),
            "no shortfall: the customer wallet covers the payment",
        ),
    ],
)
def test_bridge_blocked_without_a_shortfall(obligations, reason):
    p = Platform(obligations=obligations)
    out = bridged(p)
    assert out.state == "BLOCKED"
    assert out.proposal.reasons == (reason,)


def test_inactive_tenant_cannot_be_bridged_or_issued_a_card():
    p = alpha_platform(inactive=("t-alpha",))
    assert bridged(p).state == "BLOCKED"
    assert p.run("issue_card", CARD, tenant="t-alpha").state == "BLOCKED"


def test_platform_book_must_exist_before_bridging_or_fees():
    p = alpha_platform(capital="0")
    for action, raw in (
        ("bridge_shortfall", {"obligation_ref": "ob-a1"}),
        ("collect_fee", {"period": "2026-10"}),
    ):
        out = p.run(action, raw, tenant="t-alpha")
        assert out.state == "BLOCKED"
        assert "not initialised" in out.proposal.reasons[0]


# ---- faults on the bridge (every one through the runner) ---------------------------------


def test_bridge_lands_at_the_timeout_but_stays_invisible_for_a_while():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=20)
    out = bridged(p)
    assert out.state == "SUCCEEDED", out.detail
    assert len(p.movements("connected_account_transfers")) == 1
    assert p.book.ledger.receivable("t-alpha") == usd("300")


def test_bridge_list_lag_after_a_successful_create_does_not_fail_or_release():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.LIST_LAG, visible_after_s=20)
    out = bridged(p)
    assert out.state == "SUCCEEDED", out.detail
    assert p.book.ledger.receivable("t-alpha") == usd("300")
    assert p.book.ledger.holds() == ()


def test_bridge_500_that_still_applies_lands_once():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.SERVER_ERROR, applied=True)
    out = bridged(p)
    assert out.state == "SUCCEEDED", out.detail
    assert len(p.movements("connected_account_transfers")) == 1
    assert p.wallet(None) == usd("700")


def test_bridge_429_then_an_adapter_crash_lands_once():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.RATE_LIMIT)
    out = p.run(
        "bridge_shortfall",
        {"obligation_ref": "ob-a1"},
        tenant="t-alpha",
        port=CrashAfterSend(p.fake, BRIDGE),
    )
    assert out.state == "SUCCEEDED", out.detail
    assert len(p.movements("connected_account_transfers")) == 1
    assert p.book.ledger.receivable("t-alpha") == usd("300")


def test_bridge_duplicate_delivery_moves_money_once():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.DUPLICATE_DELIVERY)
    out = bridged(p)
    assert out.state == "SUCCEEDED", out.detail
    assert len(p.movements("connected_account_transfers")) == 1


def test_bridge_unknown_keeps_the_hold_then_reconcile_bridge_posts_it():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    out = bridged(p)
    assert out.state == "FAILED"
    rid = out.proposal.computed["request_id"]
    assert p.book.ledger.hold(rid) is not None
    again = bridged(p)
    assert again.proposal.reasons == ("a bridge for this obligation is already in flight",)
    early = p.run("reconcile_bridge", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert early.state == "BLOCKED"
    assert early.proposal.reasons[0].startswith("needs a person: no transfer under this request_id")
    p.advance(hours=1, seconds=5)
    done = p.run("reconcile_bridge", {"obligation_ref": "ob-a1"}, tenant="t-alpha", decide=reject)
    assert done.proposal.risk_tier is RiskTier.AUTO  # moves no money; DESIGN.md justifies it
    assert done.state == "SUCCEEDED", done.detail
    assert done.results["apply"] == ()
    assert p.book.ledger.hold(rid) is None
    assert p.book.ledger.receivable("t-alpha") == usd("300")
    sends = [op for op in p.sent(BRIDGE)]
    assert sends  # every resend carried the same request_id, which the provider deduplicated
    assert len(p.movements("connected_account_transfers")) == 1
    assert p.book.ledger.reconcile(provider_movements(p), accounts=p.acct) == []


def test_a_finality_read_fault_that_loses_the_runs_progress_keeps_the_hold():
    """A non-transient read fault during finality (the poll retries only transient ones) stops the
    step. The transfer has landed, so the money stays in flight and the hold is kept."""
    p = alpha_platform()
    out = p.run(
        "bridge_shortfall",
        {"obligation_ref": "ob-a1"},
        tenant="t-alpha",
        port=FailingReads(p.fake, "/api/v1/connected_account_transfers"),
    )
    assert out.state == "FAILED"
    rid = out.proposal.computed["request_id"]
    assert p.book.ledger.hold(rid) is not None
    assert "hold kept" in out.postcondition.detail
    assert p.book.ledger.headroom() == usd("100")


def test_insufficient_funds_on_a_first_send_releases_the_hold_and_moves_on():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.INSUFFICIENT_FUNDS)
    out = bridged(p)
    assert out.state == "FAILED"
    assert out.results["bridge"][0].error_code == "insufficient_fund"
    assert out.postcondition.detail == "bridge rejected (insufficient_fund); hold released"
    assert p.book.ledger.holds() == ()
    retry = bridged(p)
    assert retry.state == "SUCCEEDED"
    assert retry.proposal.computed["request_id"] == request_id_for(
        p.tenant("t-alpha"), p.book.book_id, "bridge", "ob-a1", "replacement-1"
    )


@pytest.mark.parametrize(("status", "kept"), [("FAILED", False), ("SUSPENDED", True)])
def test_reconcile_bridge_after_a_provider_failure_or_suspension(status, kept):
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    out = bridged(p)
    rid = out.proposal.computed["request_id"]
    p.advance(hours=2)
    p.force_movement_status("connected_account_transfers", rid, status)
    rec = p.run("reconcile_bridge", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert (p.book.ledger.hold(rid) is not None) is kept
    if kept:
        assert rec.proposal.reasons[0].startswith("needs a person: the bridge is SUSPENDED")
    else:
        assert rec.state == "SUCCEEDED"


@pytest.mark.parametrize("ending", ["EXPIRED", "REJECTED", "STALE"])
def test_identical_retry_that_is_abandoned_never_releases_a_live_runs_hold(ending):
    """M4 through the runner: B is proposed, then while B awaits approval an identical A (next
    approval window) runs and goes OUTCOME_UNKNOWN; B then expires, is rejected or fails STALE.
    A's hold survives."""
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=10 * 86400)
    seen = {}

    def decide(prop):
        p.advance(hours=4, minutes=1)
        seen["a"] = bridged(p)
        return {"EXPIRED": expire, "REJECTED": reject, "STALE": approve}[ending](prop)

    b = p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha", decide=decide)
    a = seen["a"]
    assert a.state == "FAILED"
    assert b.proposal.computed == a.proposal.computed
    assert b.proposal.proposal_id != a.proposal.proposal_id
    if ending == "STALE":
        assert_stale(b)
    else:
        assert b.state == ending
    rid = a.proposal.computed["request_id"]
    hold = p.book.ledger.hold(rid)
    assert hold is not None
    assert hold.owner == a.proposal.proposal_id
    assert p.book.ledger.headroom() == usd("100")


def test_a_build_never_sends_under_another_runs_hold():
    p = alpha_platform()
    first = p.step_context(
        "bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha", proposal_id="x"
    )
    rid = first.decision.computed["request_id"]
    p.book.ledger.place_hold(rid, "t-alpha", usd("300"), owner="run-a")
    ctx = p.step_context(
        "bridge_shortfall",
        {"obligation_ref": "ob-a1"},
        tenant="t-alpha",
        proposal_id="run-b",
        decision=first.decision,
    )
    with pytest.raises(StateConflict):
        p.specs["bridge_shortfall"].steps[0].build(ctx)
    assert p.sent(BRIDGE) == []


# ---- charges: M1, the dedupe margin and refused resends ---------------------------------


def unknown_fee(p: Platform):
    p.fake.inject(CHARGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=10 * 86400)
    first = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert first.state == "FAILED"
    assert "in flight" in first.postcondition.detail
    return first.proposal.computed["request_id"]


def test_charge_timeout_after_submit_reconciles_inside_the_run():
    p = Platform()
    p.fake.inject(CHARGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=20)
    out = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert out.state == "SUCCEEDED", out.detail
    assert len(p.movements("charges")) == 1


def test_charge_reproposed_inside_the_margin_lands_once():
    p = Platform()
    p.fake.inject(CHARGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3 * 86400)
    first = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    rid = first.proposal.computed["request_id"]
    p.advance(days=5)
    again = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert again.state == "SUCCEEDED", again.detail
    assert again.proposal.computed["request_id"] == rid
    assert len(p.movements("charges")) == 1
    assert p.wallet("t-charlie") == usd("875")


def test_charge_past_the_margin_goes_to_a_person_and_sends_nothing():
    p = Platform()
    rid = unknown_fee(p)
    sent = len(p.sent(CHARGE))
    p.advance(days=6, minutes=1)
    late = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert late.state == "BLOCKED"
    reason = late.proposal.reasons[0]
    assert reason.startswith("NEEDS_PERSON: fee for tenant t-charlie")
    assert rid in reason
    assert len(p.sent(CHARGE)) == sent


def test_past_the_dedupe_window_nothing_is_ever_resent():
    """The fake's request_id dedupe expires after 7 days; a resend then would charge twice."""
    p = Platform()
    unknown_fee(p)
    sent = len(p.sent(CHARGE))
    p.advance(days=8)
    late = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert late.state == "BLOCKED"
    assert len(p.sent(CHARGE)) == sent
    assert len(p.fake.objects("charges")) == 1


def test_a_refused_resend_after_an_unknown_send_keeps_the_charge_in_flight():
    """The first send is unknown; a later run's resend is refused at create with an
    empty read-back. That refusal is not definitive: no new attempt."""
    p = Platform()
    rid = unknown_fee(p)
    p.fake.inject(CHARGE, Fault.INSUFFICIENT_FUNDS, times=5)
    refused = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert refused.state == "FAILED"
    third = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert third.proposal.computed["request_id"] == rid
    assert p.book.ledger.failures(f"t-charlie:{p.book.book_id}:fee:2026-10") == 0


def test_a_refused_resend_after_a_crash_before_the_postcondition_stays_in_flight():
    p = Platform()
    p.fake.inject(CHARGE, Fault.LIST_LAG, visible_after_s=3600)
    crash = CrashAfterSend(p.fake, CHARGE)
    first = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie", port=crash)
    assert first.state == "FAILED"
    rid = first.proposal.computed["request_id"]
    p.fake.inject(CHARGE, Fault.INSUFFICIENT_FUNDS, times=5)
    p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert p.book.ledger.failures(f"t-charlie:{p.book.book_id}:fee:2026-10") == 0
    p.advance(hours=2)
    done = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert done.proposal.computed["request_id"] == rid
    assert p.book.ledger.fee_revenue() == usd("25")
    assert len(p.fake.objects("charges")) == 1


def test_charge_429_then_crash_and_500_applied_each_land_once():
    for fault, port_wrap in ((Fault.RATE_LIMIT, True), (Fault.SERVER_ERROR, False)):
        p = Platform()
        kw = {"applied": True} if fault is Fault.SERVER_ERROR else {}
        p.fake.inject(CHARGE, fault, **kw)
        port = CrashAfterSend(p.fake, CHARGE) if port_wrap else None
        out = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie", port=port)
        assert out.state == "SUCCEEDED", (fault, out.detail)
        assert len(p.fake.objects("charges")) == 1
        assert p.book.ledger.fee_revenue() == usd("25")


def test_fee_cannot_be_collected_twice_or_for_a_future_period():
    p = Platform()
    assert p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie").state == "SUCCEEDED"
    assert p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie").state == "BLOCKED"
    assert p.run("collect_fee", {"period": "2026-11"}, tenant="t-charlie").state == "BLOCKED"
    assert len(p.fake.objects("charges")) == 1


def test_repayment_after_deposit_closes_the_advance():
    p = alpha_platform()
    assert bridged(p).state == "SUCCEEDED"
    p.spend("t-alpha", "500")  # the payment due goes out
    assert p.run("collect_repayment", {}, tenant="t-alpha").state == "BLOCKED"
    p.deposit("t-alpha", "600")
    out = p.run("collect_repayment", {}, tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert p.book.ledger.receivable("t-alpha") == usd("0")
    assert p.book.ledger.cash == usd("1000")
    assert p.wallet(None) == usd("1000")
    assert p.book.ledger.reconcile(provider_movements(p), accounts=p.acct) == []


def test_repayment_in_flight_is_reemitted_not_reported_as_no_deposit():
    p = alpha_platform()
    bridged(p)
    p.spend("t-alpha", "500")
    p.deposit("t-alpha", "300")  # exactly the receivable
    p.fake.inject(CHARGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    first = p.run("collect_repayment", {}, tenant="t-alpha")
    assert first.state == "FAILED"
    assert p.wallet("t-alpha") == usd("0")  # it landed; the wallet now looks short
    p.advance(hours=2)
    again = p.run("collect_repayment", {}, tenant="t-alpha")
    assert again.state == "SUCCEEDED", (again.proposal.reasons, again.detail)
    assert p.book.ledger.receivable("t-alpha") == usd("0")
    assert len(p.fake.objects("charges")) == 1


# ---- the real executor's uncertain shapes ---------------------------------------------


def _two_objects_under_one_request_id(p: Platform, kind: str, rid: str) -> None:
    """Duplicate the provider object, so a `?request_id=` read-back returns two (the sandbox
    cannot provoke this; it is a shape the executor must never treat as a clean landing)."""
    import copy

    objs = p.fake._state.kind(kind)
    (orig,) = [o for o in objs.values() if o["request_id"] == rid]
    twin = copy.deepcopy(orig)
    twin["id"] = orig["id"][:-2] + "zz"
    objs[twin["id"]] = twin


def test_executor_refused_resend_shape_keeps_the_bridge_in_flight():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    p.fake.inject(BRIDGE, Fault.INSUFFICIENT_FUNDS, times=10)  # every resend refused
    out = bridged(p)
    (last,) = out.results["bridge"]
    assert last.status is OperationStatus.OUTCOME_UNKNOWN
    assert last.error_code is None
    assert last.http_status is None
    assert last.body["last_error"]["error_code"] == "insufficient_fund"
    rid = out.proposal.computed["request_id"]
    assert p.book.ledger.hold(rid) is not None
    assert p.book.ledger.failures(f"t-alpha:{p.book.book_id}:bridge:ob-a1") == 0


def test_executor_two_objects_under_one_request_id_keep_the_bridge_in_flight():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=0)
    rid = request_id_for(p.tenant("t-alpha"), p.book.book_id, "bridge", "ob-a1")

    class Twin(CrashAfterSend):
        def execute(self, op):
            result = self.inner.execute(op)
            if op.name == BRIDGE and self.times:
                self.times -= 1
                _two_objects_under_one_request_id(p, "connected_account_transfers", rid)
            return result

    out = p.run(
        "bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha", port=Twin(p.fake, BRIDGE)
    )
    (last,) = out.results["bridge"]
    assert last.status is OperationStatus.OUTCOME_UNKNOWN, last
    assert last.error_code == "multiple_objects"
    assert len(last.body["provider_ids"]) == 2
    assert p.book.ledger.hold(rid) is not None
    assert p.book.ledger.entry(rid) is None
    rec = p.run("reconcile_bridge", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert rec.state == "BLOCKED"
    assert rec.proposal.reasons[0].startswith("needs a person")


def test_executor_refused_resend_shape_keeps_the_charge_in_flight():
    p = Platform()
    p.fake.inject(CHARGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=10 * 86400)
    p.fake.inject(CHARGE, Fault.INSUFFICIENT_FUNDS, times=10)
    first = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    (last,) = first.results["charge"]
    assert last.status is OperationStatus.OUTCOME_UNKNOWN
    assert last.error_code is None
    assert last.body["last_error"]["error_code"] == "insufficient_fund"
    again = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie", decide=reject)
    assert again.proposal.computed["request_id"] == first.proposal.computed["request_id"]
    p.advance(days=6, minutes=1)
    late = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert late.proposal.reasons[0].startswith("NEEDS_PERSON")


def test_reserved_platform_funds_are_outside_the_wallet_room():
    """About 122 USD of earlier card authorisations stay reserved on the sandbox platform account;
    reserved money is not `available_amount`, so the wallet-room check already excludes it."""
    p = alpha_platform(platform_usd="1122")
    p.fake.reserve("USD", Decimal("122"))
    assert bridged(p).state == "SUCCEEDED"  # 1000 available - 300 bridge = 700 >= 600 floor
    assert LABEL == "poc-i5-"


# ---- refused resends, fail-closed reads and row selection -------------------------------


def test_in_run_refused_resend_after_an_unknown_send_keeps_the_bridge_in_flight():
    p = alpha_platform()
    p.fake.inject(BRIDGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    p.fake.inject(BRIDGE, Fault.INSUFFICIENT_FUNDS)  # the executor's own resend is refused
    out = bridged(p)
    rid = out.proposal.computed["request_id"]
    (last,) = out.results["bridge"]
    # The refused resend did not end the run as FAILED; the engine's further resends were
    # deduplicated by the provider, so the step ends OUTCOME_UNKNOWN.
    assert last.status is OperationStatus.OUTCOME_UNKNOWN
    assert p.book.ledger.hold(rid) is not None
    assert p.book.ledger.failures(f"t-alpha:{p.book.book_id}:bridge:ob-a1") == 0
    p.advance(hours=1, seconds=5)  # the original becomes visible
    done = p.run("reconcile_bridge", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert done.state == "SUCCEEDED", done.detail
    assert len(p.movements("connected_account_transfers")) == 1
    assert p.book.ledger.receivable("t-alpha") == usd("300")


def test_in_run_refused_charge_resend_after_an_unknown_send_stays_in_flight():
    p = Platform()
    p.fake.inject(CHARGE, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    p.fake.inject(CHARGE, Fault.INSUFFICIENT_FUNDS)
    first = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    rid = first.proposal.computed["request_id"]
    (last,) = first.results["charge"]
    assert last.status is OperationStatus.OUTCOME_UNKNOWN
    assert "in flight" in first.postcondition.detail
    p.advance(hours=2)
    again = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert again.proposal.computed["request_id"] == rid
    assert again.state == "SUCCEEDED", again.detail
    assert len(p.fake.objects("charges")) == 1
    assert p.book.ledger.fee_revenue() == usd("25")


def test_bridge_policy_fails_closed_without_platform_reads():
    from awx_common.workflow import PolicyContext

    p = alpha_platform()
    spec = p.specs["bridge_shortfall"]
    ctx = PolicyContext(
        p.tenant("t-alpha"),
        p.now(),
        p.fake.read,
        store=p.tenant_stores["t-alpha"],
        platform_store=p.platform_store,
        read_platform=None,
    )
    decision = spec.policy(spec.validate({"obligation_ref": "ob-a1"}), ctx)
    assert decision.risk_tier is RiskTier.BLOCKED
    assert decision.reasons == ("bridges need the platform wallet read; none is wired",)


def test_request_id_reused_mismatch_keeps_the_bridge_in_flight():
    """Another object under our request_id (different amount) is OUTCOME_UNKNOWN
    request_id_reused, never FAILED: hold kept, no new attempt, a person decides."""
    from dataclasses import replace as dc_replace

    from awx_k5_platform_spend.operations import bridge_operation

    p = alpha_platform()
    t = p.tenant("t-alpha")
    op = bridge_operation(
        t,
        p.book.tenants,
        book_id=p.book.book_id,
        obligation_id="ob-a1",
        amount=usd("1.00"),
        step="create",
        intent_hash="0" * 64,
    )
    stray = p.fake.execute(dc_replace(op))  # a stray object under kit 5's request_id
    p.advance(seconds=2)
    out = bridged(p)
    rid = out.proposal.computed["request_id"]
    assert rid == op.request_id
    (last,) = out.results["bridge"]
    assert last.status is OperationStatus.OUTCOME_UNKNOWN
    assert last.error_code == "request_id_reused"
    assert last.provider_id is None
    assert last.provider_status is None
    assert last.body["details"]["provider_id"] == stray.provider_id  # names the stray object
    assert p.book.ledger.hold(rid) is not None
    assert p.book.ledger.failures(f"t-alpha:{p.book.book_id}:bridge:ob-a1") == 0
    rec = p.run("reconcile_bridge", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert rec.state == "BLOCKED"
    assert rec.proposal.reasons[0].startswith("needs a person")


def test_select_returns_only_the_results_own_row():
    """A result naming provider_id X never selects a listed row Y."""
    from awx_k5_platform_spend.finality import BRIDGE_FINALITY

    result = OperationResult("rid-1", OperationStatus.SUCCEEDED, provider_id="tr_X")
    other = {"items": [{"id": "tr_Y", "request_id": "rid-1", "status": "SETTLED"}]}
    assert BRIDGE_FINALITY.select(other, result) is None
    own = {"items": [{"id": "tr_X", "request_id": "rid-1", "status": "SETTLED"}]}
    assert BRIDGE_FINALITY.select(own, result)["id"] == "tr_X"
    no_id = OperationResult("rid-1", OperationStatus.OUTCOME_UNKNOWN)
    assert BRIDGE_FINALITY.select(other, no_id)["id"] == "tr_Y"  # fallback: the one row


def test_shipped_specs_run_on_a_seeded_platform_store_with_no_override():
    """The deployed configuration path: the module-level SPECS, no configure_platform and no
    injected setup provider; the setup document and capital are seeded into the platform store
    the way the operator does at deploy. A bridge runs to SETTLED."""
    from awx_common.store import InMemoryKitStore
    from awx_k5_platform_spend import SPECS
    from awx_k5_platform_spend.ledger import CapitalLedger
    from awx_k5_platform_spend.workflow import current_setup
    from awx_spine_aws.local_runner import run_kit_workflow
    from k5_support import seed_by_operator

    p = alpha_platform()
    with pytest.raises(RuntimeError, match="no PlatformSetup"):
        current_setup()  # nothing configured in this process
    store = InMemoryKitStore()
    seed_by_operator(p, store)  # the shipped operator commands, as at deploy
    out = run_kit_workflow(
        SPECS["bridge_shortfall"],
        {"obligation_ref": "ob-a1"},
        proposed_by="requester",
        tenant=p.tenant("t-alpha"),
        port=p.fake,
        store=p.ops,
        decide=approve,
        now=p.now,
        sleep=p.fake.advance,
        kit_store=p.tenant_stores["t-alpha"],
        platform_store=store,
        tenants=p.book.tenants,
    )
    assert out.state == "SUCCEEDED", out.detail
    (transfer,) = p.movements("connected_account_transfers")
    assert transfer["status"] == "SETTLED"
    assert CapitalLedger(store, p.setup.config.reserve_floor).receivable("t-alpha") == usd("300")
