"""Every spine fault through the real runner, kit rules M1 to M3, the uncertain-send reply
shapes, and the retryable-FAILED path.

In every case a seller's payout is paid, and a seller's debt collected, exactly once. Money
whose outcome is not definitive stays in flight on its original request_id.
"""

from datetime import timedelta

import pytest
from awx_common import Origin
from awx_k8_settlement import store as st
from awx_spine_aws.engine import MAX_RESENDS

CAT = "connected_account_transfers.create"
CHARGE = "charges.create"
DAY = 86400.0


def payouts(h, m):
    return [o for o in m.transfers_to("bob") if o["status"] != "FAILED"]


def bob(h):
    return h.Market(h.bob_c1_net())


def bob_owes(h, wallet="1000"):
    m = h.Market(h.usd(0))  # bob's c1 cash is funded only by tests that settle c1
    m.fund(h.usd(300))
    m.own_cash = h.usd(300)
    m.add_cycle("bob", "c0", 0, 300, 0)  # the platform pays 300 of refunds from own cash
    assert m.run(h.SETTLE, {"cycle_id": "c0"}, "bob").state == "SUCCEEDED"
    m.set_wallet("bob", h.usd(wallet))
    return m


def paid_once(h, m, amount="6960"):
    assert [o["amount"] for o in payouts(h, m)] == [int(amount)]
    led = m.ledger("bob")
    assert led.paid_out == h.usd(amount)
    assert led.payable.is_zero()
    assert led.payout_pending.is_zero()
    assert m.balanced()


def collected_once(h, m):
    assert [o["amount"] for o in m.charges_from("bob") if o["status"] != "FAILED"] == [300]
    led = m.ledger("bob")
    assert led.receivable.is_zero()
    assert led.recovered == h.usd(300)
    assert led.payable.is_zero()  # nothing collected twice and handed back
    assert m.balanced()


# --- the fake's own faults -----------------------------------------------------------------


def test_timeout_that_lands_invisibly_stays_in_flight_then_pays_once(h):
    m = bob(h)
    m.port.script(CAT, "timeout:120")
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert out.state == "FAILED"
    assert "in flight" in out.postcondition.detail
    assert m.ledger("bob").payout_pending == h.usd(6960)
    assert m.ledger("bob").payable.is_zero()
    m.advance(121)
    assert m.run(h.RECONCILE, {}, "bob").state == "SUCCEEDED"
    paid_once(h, m)


def test_list_lag_after_a_successful_create_pays_once(h):
    m = bob(h)
    m.port.script(CAT, "lag:20")
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    if out.state != "SUCCEEDED":  # the finality read may not see it yet: in flight, not failed
        assert m.ledger("bob").payable.is_zero()
        m.advance(21)
        assert m.run(h.RECONCILE, {}, "bob").state == "SUCCEEDED"
    paid_once(h, m)


def test_a_500_that_still_applies_pays_once(h):
    m = bob(h)
    m.port.script(CAT, "error500")
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    if out.state != "SUCCEEDED":
        m.advance(5)
        m.run(h.RECONCILE, {}, "bob")
    paid_once(h, m)


def test_a_429_then_a_crash_after_the_send_pays_once(h):
    m = bob(h)
    m.port.script(CAT, "ratelimit", "crash")
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    if out.state != "SUCCEEDED":
        m.advance(5)
        m.run(h.RECONCILE, {}, "bob")
    paid_once(h, m)


def test_a_charge_that_times_out_invisibly_collects_once(h):
    m = bob_owes(h)
    m.port.script(CHARGE, "timeout:120")
    out = m.run(h.RECOVER, {}, "bob")
    assert out.state == "FAILED"
    assert st.recovery_attempts(m.stores["bob"])[0].outcome == "PENDING"
    m.advance(121)
    assert m.run(h.RECONCILE, {}, "bob").state == "SUCCEEDED"
    collected_once(h, m)


# --- a retryable FAILED is money in flight ------------------------------------------------


def test_invisible_landing_then_every_resend_rate_limited_pays_and_collects_once(h):
    """The first send times out and lands invisibly, every resend gets 429, then the original
    becomes visible. The final FAILED RateLimited must not count as NOT_CREATED."""
    m = bob_owes(h)
    m.port.script(CHARGE, "timeout:600", *(["ratelimit"] * (MAX_RESENDS + 2)))
    rec = m.run(h.RECOVER, {}, "bob")
    assert rec.state == "FAILED"
    assert st.recovery_attempts(m.stores["bob"])[0].outcome == "PENDING"  # not FAILED
    assert m.ledger("bob").receivable == h.usd(300)
    m.fund(h.bob_c1_net())
    m.port.script(CAT, "timeout:600", *(["ratelimit"] * (MAX_RESENDS + 2)))
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert out.state == "FAILED"
    led = m.ledger("bob")
    assert led.payable.is_zero()  # NOT payable: it may have landed (it did)
    assert led.payout_pending == h.usd(6960)  # the in-flight charge is not netted
    m.port.clear()
    m.advance(601)
    assert m.run(h.RECONCILE, {}, "bob").state == "SUCCEEDED"
    paid_once(h, m)
    collected_once(h, m)


# --- M1 window and the dedupe expiry --------------------------------------------------------


def test_dedupe_expiry_past_the_window_is_a_person_and_nothing_is_resent(h):
    m = bob(h)
    m.port.script(CAT, "timeout:" + str(9 * DAY))  # landed, invisible for 9 days
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    sends = len(m.port.executed)
    m.advance(3 * DAY)  # inside the window: a resend is refused as a duplicate, in flight
    early = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob", origin=Origin.SCHEDULE)
    assert early.proposal.computed["mode"] == "resend"
    assert m.ledger("bob").payable.is_zero()
    assert len(payouts(h, m)) == 1
    m.advance(4 * DAY)  # day 7: Airwallex has forgotten the request_id
    late = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob", origin=Origin.SCHEDULE)
    assert late.state == "BLOCKED"
    assert "needs a person" in late.proposal.reasons[0]
    rec = m.run(h.RECONCILE, {}, "bob")
    assert "NEEDS_PERSON" in rec.postcondition.detail
    assert len(payouts(h, m)) == 1  # nothing re-sent, so no second object
    flagged = st.needs_person(m.stores["bob"])
    assert [f["ref"] for f in flagged] == ["c1"]
    assert len(m.port.executed) > sends  # the inside-window resend did go out (refused)


def test_without_the_window_guard_a_late_resend_would_pay_twice(h):
    """Why M1 stops at the window: past dedupe expiry the same request_id creates a second
    object on the fake, as it would on Airwallex."""
    m = bob(h)
    m.port.script(CAT, "timeout:" + str(9 * DAY))
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    first = m.port.executed[0]
    m.advance(7.5 * DAY)
    m.fund(h.usd(6960))
    m.fake.execute(first)  # what a kit without the guard would send
    assert len(payouts(h, m)) == 2


# --- uncertain-send reply shapes ----------------------------------------------------------


@pytest.mark.parametrize("op", [CAT, CHARGE])
def test_refused_resend_keeps_money_in_flight(h, op):
    m = bob(h) if op == CAT else bob_owes(h)
    if op == CAT:
        m.port.script(CAT, "late", *(["refused"] * MAX_RESENDS))
        m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
        assert m.ledger("bob").payable.is_zero()
        assert m.ledger("bob").payout_pending == h.usd(6960)
    else:
        m.port.script(CHARGE, "late", *(["refused"] * MAX_RESENDS))
        m.run(h.RECOVER, {}, "bob")
        assert st.recovery_attempts(m.stores["bob"])[0].outcome == "PENDING"
        nxt = m.run(h.RECOVER, {}, "bob", approve=False)
        assert nxt.proposal.computed["mode"] == "resend"  # no new attempt
    m.port.deliver()
    assert m.run(h.RECONCILE, {}, "bob").state == "SUCCEEDED"
    if op == CAT:
        paid_once(h, m)
    else:
        collected_once(h, m)


def test_multiple_objects_keeps_money_in_flight_for_a_person(h):
    m = bob(h)
    m.port.script(CAT, "late", *(["multiple"] * MAX_RESENDS))
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    m.port.deliver()
    m.port.duplicated.add(m.port.executed[0].request_id)
    rec = m.run(h.RECONCILE, {}, "bob")
    assert rec.state == "POSTCONDITION_FAILED"  # undetermined: nothing posted
    led = m.ledger("bob")
    assert led.payout_pending == h.usd(6960)
    assert led.payable.is_zero()
    assert led.paid_out.is_zero()
    m.offset_s = 7 * DAY
    assert "NEEDS_PERSON" in m.run(h.RECONCILE, {}, "bob").postcondition.detail


# --- M1: a refusal at create is definitive only for a first send --------------------------


def test_refusal_on_a_first_send_is_definitive(h):
    m = bob(h)
    m.port.script(CAT, "reject")
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert out.state == "FAILED"
    assert m.ledger("bob").payable == h.usd(6960)  # nothing was ever created: payable
    assert m.ledger("bob").payout_pending.is_zero()


def test_in_run_resend_refused_while_the_first_copy_is_in_transit_pays_once(h):
    """An in-run refusal after an unknown first send keeps the money in flight."""
    m = bob(h)
    m.port.script(CAT, "late", *(["reject"] * MAX_RESENDS))
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    # An in-run refusal after an unknown first send is OUTCOME_UNKNOWN with
    # body["last_error"], so the kit keeps the money in flight.
    assert out.results["payout"][-1].status.value == "OUTCOME_UNKNOWN"
    assert out.results["payout"][-1].body["last_error"]
    assert m.ledger("bob").payable.is_zero()
    m.port.deliver()
    m.fund(h.usd(6960))  # enough cash that a second payout would go through
    m.add_cycle("bob", "c2", 0, 0, 8000)
    m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert [o["amount"] for o in payouts(h, m)] == [6960]


def test_kit_clock_alone_past_the_window_sends_nothing(h):
    m = bob(h)
    m.port.script(CAT, "late", *(["lost"] * MAX_RESENDS))
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    n = len(m.port.executed)
    m.offset_s = timedelta(days=6, hours=1).total_seconds()
    assert m.run(h.SETTLE, {"cycle_id": "c1"}, "bob").state == "BLOCKED"
    assert len(m.port.executed) == n


def test_real_executor_multiple_objects_stays_in_flight(h):
    """Through the real executor (not the KitPort shape): the first send lands
    invisibly, the read-back then shows two objects under the request_id, and the executor
    returns OUTCOME_UNKNOWN multiple_objects. The kit keeps the money in flight."""
    from awx_k8_settlement.operations import payout_request_id

    m = bob(h)
    rid = payout_request_id(st.book_id(m.stores["bob"]), "bob", "c1")
    m.port.duplicated.add(rid)
    m.port.script(CAT, "timeout:3")
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    last = out.results["payout"][-1]
    assert last.status.value == "OUTCOME_UNKNOWN"
    assert last.error_code == "multiple_objects"
    led = m.ledger("bob")
    assert led.payout_pending == h.usd(6960)
    assert led.payable.is_zero()
    assert led.paid_out.is_zero()


# --- final unknown codes reach a person at once ---------------------------------------------


def test_multiple_objects_flags_a_person_in_the_settle_postcondition(h):
    from awx_k8_settlement.operations import payout_request_id

    m = bob(h)
    rid = payout_request_id(st.book_id(m.stores["bob"]), "bob", "c1")
    m.port.duplicated.add(rid)
    m.port.script(CAT, "timeout:3")
    out = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert out.results["payout"][-1].error_code == "multiple_objects"
    assert "needs a person" in out.postcondition.detail
    assert [f["ref"] for f in st.needs_person(m.stores["bob"])] == ["c1"]
    assert m.ledger("bob").payout_pending == h.usd(6960)


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_a_request_id_reused_mismatch_flags_a_person(h, kind):
    """A foreign object sits under our request_id: OUTCOME_UNKNOWN request_id_reused.
    A person is told at once, not when the deduplication window ends."""
    m = bob(h) if kind == "payout" else bob_owes(h)
    op_name = CAT if kind == "payout" else CHARGE
    m.port.script(op_name, "timeout:600")
    spec = h.SETTLE if kind == "payout" else h.RECOVER
    args = {"cycle_id": "c1"} if kind == "payout" else {}
    m.run(spec, args, "bob")
    rid = next(op for op in m.port.executed if op.name == op_name).request_id
    kind_name = "connected_account_transfers" if kind == "payout" else "charges"
    for o in m.fake._state.kind(kind_name).values():
        if o["request_id"] == rid:
            o["amount"] = 1  # no public control for a foreign object (listed in the report)
    m.advance(601)
    rec = m.run(h.RECONCILE, {}, "bob")
    assert "needs a person" in rec.postcondition.detail
    assert rec.state == "POSTCONDITION_FAILED"
    flagged = st.needs_person(m.stores["bob"])
    assert [f["ref"] for f in flagged] == (["c1"] if kind == "payout" else ["1"])
    led = m.ledger("bob")
    if kind == "payout":
        assert led.payout_pending == h.usd(6960)
        assert led.paid_out.is_zero()
    else:
        assert led.receivable == h.usd(300)
        assert led.recovered.is_zero()


def test_a_transient_read_back_does_not_flag_a_person(h):
    from awx_spine.fake import Fault

    m = bob(h)
    m.port.script(CAT, "late", *(["lost"] * MAX_RESENDS))
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    m.fake.inject("connected_account_transfers.list", Fault.SERVER_ERROR, times=20)
    rec = m.run(h.RECONCILE, {}, "bob")
    m.fake.clear_faults()
    assert st.needs_person(m.stores["bob"]) == ()
    assert "needs a person" not in (rec.postcondition.detail if rec.postcondition else "")


# --- an item flagged for a person is counted once -------------------------------------------


def test_reconcile_counts_a_flagged_payout_once_in_its_postcondition(h):
    """A payout whose read-back finds two objects is both still in flight and flagged for a
    person; the postcondition names it once (it used to be counted through both paths)."""
    m = bob(h)
    m.port.script(CAT, "late", *(["multiple"] * MAX_RESENDS))
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    m.port.deliver()
    m.port.duplicated.add(m.port.executed[0].request_id)
    out = m.run(h.RECONCILE, {}, "bob")
    assert st.needs_person(m.stores["bob"]), out.postcondition
    assert out.postcondition.ok is False
    assert out.postcondition.detail.endswith("; 1 item still in flight"), out.postcondition
