"""Kit 8 scenarios through the real runner (local_runner over FakeAirwallex): the report's
acceptance test, approvals and tiers, resolution of every payout and charge outcome, and
regressions for money-safety edge cases."""

from datetime import timedelta

import pytest
from awx_common import Origin, RiskTier
from awx_common.errors import TenantScopeViolation
from awx_k8_settlement import READS
from awx_k8_settlement import store as st
from awx_k8_settlement.evidence import Order
from awx_spine_aws.engine import MAX_RESENDS

CAT = "connected_account_transfers.create"
CHARGE = "charges.create"


def stall(m, op_name):
    """The first send stays in transit and every in-run resend is lost: OUTCOME_UNKNOWN."""
    m.port.script(op_name, "late", *(["lost"] * MAX_RESENDS))


def bob_market(h, platform=None):
    """Only bob is settled here, so the platform holds only bob's c1 cash."""
    return h.Market(platform if platform is not None else h.bob_c1_net())


def owe_bob(h, m, amount="300"):
    m.fund(h.usd(amount))  # the platform's own cash pays the c0 buyer refunds
    m.own_cash = m.own_cash + h.usd(amount)
    m.add_cycle("bob", "c0", 0, amount, 0)
    out = m.run(h.SETTLE, {"cycle_id": "c0"}, "bob")
    assert out.state == "SUCCEEDED", (out.state, out.detail, out.postcondition)
    assert m.ledger("bob").receivable == h.usd(amount)


def settle_all(h, m, cycle_id="c1", **kw):
    return {s: m.run(h.SETTLE, {"cycle_id": cycle_id}, s, **kw) for s in h.SELLERS}


def test_newest_seller_gets_the_highest_reserve(h, market):
    settle_all(h, market)
    held = {s: market.ledger(s).reserve_held for s in h.SELLERS}
    assert held == {"alice": h.usd(500), "bob": h.usd(800), "cara": h.usd(1000)}


def test_acceptance_carrier_failure_for_seller_x(h):
    """Report, kit 8 required acceptance test, through the real runner. Seller X is bob."""
    base = h.Market(h.c1_net())
    settle_all(h, base)
    m = h.Market(h.c1_net(), twin_of=base)
    recalc = m.run(h.RECALC, {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"}, "bob")
    assert recalc.state == "SUCCEEDED", recalc.detail
    assert recalc.proposal.risk_tier is RiskTier.SINGLE  # a material reserve increase
    assert m.port.executed == []  # recalculation moves no money
    outcomes = settle_all(h, m)
    extra = m.ledger("bob").reserve_held - base.ledger("bob").reserve_held
    assert extra == h.usd(2200)
    assert base.settled("bob", "c1").payout - m.settled("bob", "c1").payout == extra
    assert [o["amount"] for o in m.transfers_to("bob")] == [m.settled("bob", "c1").payout.amount]
    for s in ("alice", "cara"):  # unaffected sellers byte-identical
        assert h.dump(m.stores[s]) == h.dump(base.stores[s])
    for s in h.SELLERS:
        assert outcomes[s].state == "SUCCEEDED", outcomes[s].postcondition
        assert m.settled(s, "c1").residual().is_zero()
    assert m.balanced()
    assert m.platform_cash == h.usd(500 + 3000 + 1000)  # = unreleased reserves
    # Refunds surge (the 40 orders are refunded) and exceed bob's cash and reserve.
    refunded = tuple(Order(f"bob-{i:03d}", "bob", h.usd(75), h.usd(75), False) for i in range(40))
    st.put_orders(m.stores["bob"], refunded)
    m.add_cycle("bob", "c2", 500, 4500, 500)
    c2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob", origin=Origin.SCHEDULE)
    assert c2.proposal.risk_tier is RiskTier.SINGLE  # a receivable is created
    assert m.settled("bob", "c2").shortfall == h.usd(1000)
    # X lacks funds: no recovery charge is attempted; the shortfall is a receivable.
    m.set_wallet("bob", h.usd(0))
    rec = m.run(h.RECOVER, {}, "bob")
    assert rec.state == "SUCCEEDED", rec.detail
    assert m.charges_from("bob") == []
    assert m.ledger("bob").receivable == h.usd(1000)
    assert m.balanced()
    # Cross-tenant movement is rejected: bob's evidence under alice's tenant is BLOCKED.
    cross = m.run(h.RECALC, {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"}, "alice")
    assert cross.state == "BLOCKED"


def test_evidence_hold_carries_forward_and_decays_as_orders_deliver(h):
    m = bob_market(h)
    m.run(h.RECALC, {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"}, "bob")
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    m.add_cycle("bob", "c2", 8000, 0, 8000)
    m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert m.ledger("bob").reserve_held == h.usd(3000)
    half = tuple(Order(f"bob-{i:03d}", "bob", h.usd(75), h.usd(0), i < 20) for i in range(40))
    st.put_orders(m.stores["bob"], half)
    m.add_cycle("bob", "c3", 8000, 0, 8000)
    m.run(h.SETTLE, {"cycle_id": "c3"}, "bob")
    assert m.ledger("bob").reserve_held == h.usd(1500)
    assert m.balanced()


def test_recovery_charge_always_needs_approval_and_settles_the_receivable(h):
    m = bob_market(h, h.usd(0))
    owe_bob(h, m)
    m.set_wallet("bob", h.usd(1000))
    pending = m.run(h.RECOVER, {}, "bob", approve=False, origin=Origin.SCHEDULE)
    assert pending.state == "EXPIRED"  # nobody approved: a charge never runs unapproved
    assert pending.proposal.risk_tier is RiskTier.SINGLE
    assert m.charges_from("bob") == []
    done = m.run(h.RECOVER, {}, "bob")
    assert done.state == "SUCCEEDED", (done.detail, done.postcondition)
    assert len(m.charges_from("bob")) == 1
    assert m.ledger("bob").receivable.is_zero()
    assert m.wallet("bob") == h.usd(700)
    assert m.balanced()


def test_insufficient_funds_on_a_first_charge_keeps_the_receivable_and_never_retries(h):
    m = bob_market(h, h.usd(0))
    owe_bob(h, m)
    m.set_wallet("bob", h.usd(1000))
    m.port.script(CHARGE, "nsf")
    out = m.run(h.RECOVER, {}, "bob")
    assert out.state == "FAILED"
    assert "receivable" in out.postcondition.detail
    assert len([op for op in m.port.executed if op.name == CHARGE]) == 1
    assert m.ledger("bob").receivable == h.usd(300)
    assert st.recovery_attempts(m.stores["bob"])[0].outcome == "INSUFFICIENT_FUNDS"
    nxt = m.run(h.RECOVER, {}, "bob", approve=False)
    assert nxt.proposal.computed["attempt"] == 2  # a NEW attempt only after a definitive one


def test_wallet_emptied_after_approval_is_stale(h):
    m = bob_market(h, h.usd(0))
    owe_bob(h, m)
    m.set_wallet("bob", h.usd(1000))
    out = m.run(h.RECOVER, {}, "bob", tamper=lambda: m.set_wallet("bob", h.usd(0)))
    assert out.state == "FAILED"  # rule 1: the recheck no longer matches the approval
    assert "changed since the proposal" in out.detail
    assert m.charges_from("bob") == []
    assert m.ledger("bob").receivable == h.usd(300)


def test_ledger_change_after_approval_is_stale(h):
    m = bob_market(h)

    def change():
        st.put_cycle(m.stores["bob"], st.CycleData("c1", h.usd(9000), h.usd(240), h.usd(8000)))

    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob", tamper=change)
    assert out.state == "FAILED"
    assert m.settled("bob", "c1") is None
    assert m.port.executed == []


def test_unknown_outcome_stays_in_flight_and_is_never_paid_twice(h):
    """M1: an empty read-back is not a failure; the next cycle excludes the in-flight money;
    the late landing pays once."""
    m = bob_market(h)
    stall(m, CAT)
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert out.state == "FAILED"
    assert "in flight on request_id" in out.postcondition.detail
    led = m.ledger("bob")
    assert led.payout_pending == h.usd(6960)
    assert led.payable.is_zero()
    m.add_cycle("bob", "c2", 100, 0, 0)
    c2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert c2.state == "SUCCEEDED", c2.postcondition
    assert m.settled("bob", "c2").payout == h.usd(900)  # 100 + 800 reserve released
    rec = m.run(h.RECONCILE, {}, "bob")
    assert rec.proposal.risk_tier is RiskTier.AUTO  # justified agent_min_tier=AUTO
    assert rec.state == "POSTCONDITION_FAILED"
    m.port.deliver()
    sent = len(m.port.executed)
    rec2 = m.run(h.RECONCILE, {}, "bob")
    assert rec2.state == "SUCCEEDED", rec2.postcondition
    assert len(m.port.executed) == sent
    assert sorted(o["amount"] for o in m.transfers_to("bob")) == [900, 6960]
    assert m.ledger("bob").paid_out == h.usd(7860)
    assert m.balanced()


def test_resend_reemits_the_identical_operation_and_lands_once(h):
    m = bob_market(h)
    stall(m, CAT)
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    first = m.port.executed[0]
    again = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob", origin=Origin.SCHEDULE)
    assert again.proposal.computed["mode"] == "resend"
    assert again.state == "SUCCEEDED", (again.detail, again.postcondition)
    last = m.port.executed[-1]
    assert (last.request_id, last.path, last.body) == (first.request_id, first.path, first.body)
    m.port.deliver()  # the original arrives late: a refused duplicate
    assert len(m.transfers_to("bob")) == 1
    assert m.ledger("bob").paid_out == h.usd(6960)
    assert m.balanced()


def test_payout_resend_refused_while_the_first_copy_is_in_transit_pays_once(h):
    m = bob_market(h)
    stall(m, CAT)
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    m.port.script(CAT, *(["reject"] * (MAX_RESENDS + 1)))  # every resend refused at create
    resend = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob", origin=Origin.SCHEDULE)
    # the executor keeps every refusal after an uncertain send OUTCOME_UNKNOWN
    assert resend.results["payout"][-1].status.value == "OUTCOME_UNKNOWN"
    assert "last_error" in (resend.results["payout"][-1].body or {})
    assert resend.proposal.computed["mode"] == "resend"
    led = m.ledger("bob")
    assert led.payable.is_zero()  # a refused RESEND is not definitive (M1)
    assert led.payout_pending == h.usd(6960)
    m.port.deliver()
    m.add_cycle("bob", "c2", 0, 0, 8000)
    c2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert m.settled("bob", "c2").payout.is_zero()
    assert c2.state == "SUCCEEDED", c2.postcondition
    assert m.run(h.RECONCILE, {}, "bob").state == "SUCCEEDED"
    assert [o["amount"] for o in m.transfers_to("bob")] == [6960]
    assert m.balanced()


def test_charge_resend_refused_while_the_first_copy_is_in_transit_collects_once(h):
    m = bob_market(h, h.usd(0))
    owe_bob(h, m)
    m.set_wallet("bob", h.usd(1000))
    stall(m, CHARGE)
    m.run(h.RECOVER, {}, "bob")
    m.port.script(CHARGE, *(["nsf"] * (MAX_RESENDS + 1)))
    resend = m.run(h.RECOVER, {}, "bob", origin=Origin.SCHEDULE)
    assert resend.results["recover"][-1].status.value == "OUTCOME_UNKNOWN"
    assert resend.proposal.computed["mode"] == "resend"
    assert st.recovery_attempts(m.stores["bob"])[0].outcome == "PENDING"
    assert m.ledger("bob").receivable == h.usd(300)
    m.port.deliver()
    again = m.run(h.RECOVER, {}, "bob", origin=Origin.SCHEDULE)
    assert again.proposal.computed["mode"] == "resend"
    assert again.state == "SUCCEEDED", (again.detail, again.postcondition)
    assert len(m.charges_from("bob")) == 1
    assert m.ledger("bob").receivable.is_zero()
    assert m.ledger("bob").payable.is_zero()
    assert m.balanced()


def test_recovery_charge_timeout_lands_late_and_is_not_netted(h):
    m = bob_market(h, h.usd(0))
    owe_bob(h, m)
    m.set_wallet("bob", h.usd(1000))
    stall(m, CHARGE)
    out = m.run(h.RECOVER, {}, "bob")
    assert out.state == "FAILED"
    assert "in flight" in out.postcondition.detail
    m.add_cycle("bob", "c1b", 1000, 0, 0)
    m.run(h.SETTLE, {"cycle_id": "c1b"}, "bob")
    assert m.settled("bob", "c1b").receivable_offset.is_zero()
    m.port.deliver()
    assert m.run(h.RECONCILE, {}, "bob").state == "SUCCEEDED"
    assert len(m.charges_from("bob")) == 1
    led = m.ledger("bob")
    assert led.receivable.is_zero()
    assert led.recovered == h.usd(300)
    assert led.payable.is_zero()
    assert m.balanced()


def test_suspended_payout_is_held_for_a_person(h):
    m = bob_market(h)
    m.port.script(CAT, "suspend")
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert out.state == "FAILED"
    assert "operator action required" in out.postcondition.detail
    assert m.ledger("bob").payout_pending == h.usd(6960)
    flagged = READS["read_seller_ledger"]({}, m.context("bob"))["needs_person"]
    assert [f["ref"] for f in flagged] == ["c1"]
    m.port.set_status(m.port.executed[0].request_id, "SETTLED")
    fix = m.run(h.RECONCILE, {}, "bob")
    assert fix.state == "SUCCEEDED", fix.postcondition
    assert m.ledger("bob").paid_out == h.usd(6960)
    assert READS["read_seller_ledger"]({}, m.context("bob"))["needs_person"] == []


def test_failed_payout_becomes_payable_and_is_re_sent(h):
    m = bob_market(h)
    m.port.script(CAT, "fail")
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert out.state == "FAILED"
    assert m.ledger("bob").payable == h.usd(6960)
    assert m.balanced()
    m.add_cycle("bob", "c2", 0, 0, 8000)
    out2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert m.settled("bob", "c2").payout == h.usd(6960)
    assert out2.state == "SUCCEEDED", out2.postcondition
    assert m.wallet("bob") == h.usd(6960)
    assert m.balanced()


def test_suspended_and_pending_recovery_charges_reconcile(h):
    m = bob_market(h, h.usd(0))
    owe_bob(h, m)
    m.set_wallet("bob", h.usd(1000))
    m.port.script(CHARGE, "suspend")
    out = m.run(h.RECOVER, {}, "bob")
    assert "operator action required" in out.postcondition.detail
    assert m.ledger("bob").receivable == h.usd(300)
    m.port.set_status(m.charges_from("bob")[0]["request_id"], "SETTLED")
    fix = m.run(h.RECOVER, {}, "bob", origin=Origin.SCHEDULE)
    assert fix.proposal.computed["mode"] == "resend"
    assert fix.state == "SUCCEEDED", (fix.detail, fix.postcondition)
    assert m.ledger("bob").receivable.is_zero()
    assert len(m.charges_from("bob")) == 1
    assert m.balanced()


def test_settling_a_cycle_twice_is_blocked(h, market):
    market.run(h.SETTLE, {"cycle_id": "c1"}, "alice")
    assert market.run(h.SETTLE, {"cycle_id": "c1"}, "alice").state == "BLOCKED"


def test_agent_floor_and_scheduled_auto(h, market):
    market.run(h.SETTLE, {"cycle_id": "c1"}, "alice")
    market.add_cycle("alice", "c2", 1000, 0, 10500)
    agent = market.run(h.SETTLE, {"cycle_id": "c2"}, "alice", approve=False)
    assert agent.proposal.risk_tier is RiskTier.SINGLE  # AUTO policy, model-started: floor
    assert agent.state == "EXPIRED"
    sched = market.run(h.SETTLE, {"cycle_id": "c2"}, "alice", approve=False, origin=Origin.SCHEDULE)
    assert sched.state == "SUCCEEDED", sched.postcondition


def test_large_reserve_increase_needs_two_approvers(h, market):
    market.add_cycle("cara", "big", 90000, 0, 90000)
    out = market.run(h.SETTLE, {"cycle_id": "big"}, "cara", approve=False)
    assert out.proposal.risk_tier is RiskTier.DUAL


def test_holds_are_owned_by_the_run_that_took_them_and_abandon_releases_none(h):
    """M4 through the runner."""
    m = bob_market(h, h.usd(0))
    owe_bob(h, m)
    m.set_wallet("bob", h.usd(1000))
    stall(m, CHARGE)
    out = m.run(h.RECOVER, {}, "bob")
    claim = st.recovery_attempts(m.stores["bob"])[0]
    assert claim.owner_proposal_id == out.proposal.proposal_id
    assert claim.outcome == "PENDING"
    sent = m.stores["bob"].get("sent/charge/1").value
    assert sent["owner_proposal_id"] == out.proposal.proposal_id
    before = h.dump(m.stores["bob"])
    rejected = m.run(h.RECOVER, {}, "bob", approve=False)  # a resend nobody approves
    assert rejected.state == "EXPIRED"
    assert h.dump(m.stores["bob"]) == before  # abandon released nothing
    m.port.deliver()
    later = m.run(h.RECONCILE, {}, "bob")
    assert later.state == "SUCCEEDED"
    claim = st.recovery_attempts(m.stores["bob"])[0]
    assert claim.outcome == "SETTLED"
    assert claim.owner_proposal_id == out.proposal.proposal_id  # the owner never changes


def test_reads_are_tenant_scoped(h, market):
    market.set_wallet("bob", h.usd(42))
    ctx = market.context("bob")
    assert READS["read_seller_balance"]({"currency": "USD"}, ctx)["available"] == h.usd(42)
    view = READS["read_carrier_event"]({"carrier_event_id": "evt-bob-1"}, ctx)
    assert view["exposure_computed_by_code"] == h.usd(3000)
    assert READS["read_seller_ledger"]({}, ctx)["identity_residual"].is_zero()
    with pytest.raises(TenantScopeViolation):  # the forged copy filed under alice names bob
        READS["read_carrier_event"]({"carrier_event_id": "evt-bob-1"}, market.context("alice"))
    with pytest.raises(Exception, match="no carrier event"):
        READS["read_carrier_event"]({"carrier_event_id": "evt-bob-1"}, market.context("cara"))


def test_kit_clock_past_the_window_blocks_resends(h):
    m = bob_market(h)
    stall(m, CAT)
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    m.offset_s = timedelta(days=7).total_seconds()
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert out.state == "BLOCKED"
    assert "needs a person" in out.proposal.reasons[0]
    assert len(m.port.executed) == 1 + MAX_RESENDS
