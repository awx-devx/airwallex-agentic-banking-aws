"""The operator tool for money in flight past the deduplication window, and the
watch on its not_landed decisions, through the real runner."""

from dataclasses import replace

import pytest
from awx_common import RiskTier
from awx_k8_settlement import SPECS
from awx_k8_settlement import store as st
from awx_k8_settlement import watch as wt
from awx_spine_aws.engine import MAX_RESENDS

CAT = "connected_account_transfers.create"
CHARGE = "charges.create"
DAY = 86400.0


def resolve(m, kind, ref, resolution, spec=None, **kw):
    raw = {"kind": kind, "ref": ref, "resolution": resolution, "evidence_ref": "awx-case:4711"}
    return m.run(spec or SPECS["resolve_in_flight_money"], raw, "bob", **kw)


def stuck_payout(h, first="lost"):
    """bob's c1 payout with no answer; the kit's clock is 7 days on. first="late" keeps the
    request in transit so the test can deliver it later."""
    m = h.Market(h.bob_c1_net())
    m.port.script(CAT, first, *(["lost"] * MAX_RESENDS))
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    m.offset_s = 7 * DAY
    m.run(h.RECONCILE, {}, "bob")  # flags it for a person
    return m


def stuck_charge(h, first="lost"):
    m = h.Market(h.usd(0))
    m.fund(h.usd(300))
    m.own_cash = h.usd(300)
    m.add_cycle("bob", "c0", 0, 300, 0)
    m.run(h.SETTLE, {"cycle_id": "c0"}, "bob")
    m.set_wallet("bob", h.usd(1000))
    m.port.script(CHARGE, first, *(["lost"] * MAX_RESENDS))
    m.run(h.RECOVER, {}, "bob")
    m.offset_s = 7 * DAY
    return m


def ruled(h, kind, first="late"):
    """A not_landed decision on an item still in transit."""
    m = stuck_payout(h, first) if kind == "payout" else stuck_charge(h, first)
    out = resolve(m, kind, "c1" if kind == "payout" else "1", "not_landed")
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    return m, out


def confirm(h, m):
    """A reconcile a day after the decision that reads the watch empty: confirms it."""
    m.offset_s += DAY + 60
    rec = m.run(h.RECONCILE, {}, "bob")
    assert all(w.get("confirmed_at") for w in wt.open_watches(m.stores["bob"])), rec
    return rec


def twice(spec):
    """Runner rule 0: every build runs twice for the same proposal (a crash and retry after
    the build); the first result is thrown away, the second is the one sent."""
    steps = tuple(replace(s, build=lambda ctx, b=s.build: (b(ctx), b(ctx))[1]) for s in spec.steps)
    return replace(spec, steps=steps)


# --- the tool --------------------------------------------------------------------------------


def test_not_landed_payout_is_re_paid_once_only_after_a_confirming_read(h):
    m = stuck_payout(h)
    assert [f["ref"] for f in st.needs_person(m.stores["bob"])] == ["c1"]
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.proposal.risk_tier is RiskTier.DUAL
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert "in flight past the deduplication window" in out.proposal.reasons[0]  # says why
    assert m.ledger("bob").payable == h.usd(6960)
    assert st.needs_person(m.stores["bob"]) == ()
    rec = st.resolution(m.stores["bob"], "payout", "c1")
    assert rec["owner_proposal_id"] == out.proposal.proposal_id
    m.add_cycle("bob", "c2", 0, 0, 8000)
    early = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert early.state == "BLOCKED"  # no re-pay until a second clean read a day on
    assert "not confirmed yet" in early.proposal.reasons[0]
    confirm(h, m)
    c2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert c2.state == "SUCCEEDED", c2.postcondition
    assert any("watching 1 not_landed" in r for r in c2.proposal.reasons)
    assert [o["amount"] for o in m.transfers_to("bob")] == [6960]
    assert m.balanced()


def test_landed_payout_is_posted_settled_and_nothing_is_sent(h):
    m = stuck_payout(h)
    sends = len(m.port.executed)
    out = resolve(m, "payout", "c1", "landed")
    assert out.state == "SUCCEEDED", out.postcondition
    assert len(m.port.executed) == sends
    assert m.ledger("bob").paid_out == h.usd(6960)
    assert wt.open_watches(m.stores["bob"]) == ()


def test_needs_two_approvers(h):
    m = stuck_payout(h)
    out = resolve(m, "payout", "c1", "not_landed", approve=False)
    assert out.proposal.risk_tier is RiskTier.DUAL
    assert out.state == "EXPIRED"
    assert m.ledger("bob").payout_pending == h.usd(6960)


def test_blocked_inside_the_window(h):
    m = stuck_payout(h)
    m.offset_s = 2 * DAY
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.state == "BLOCKED"
    assert "inside the deduplication window" in out.proposal.reasons[0]


def test_blocked_when_airwallex_shows_the_object(h):
    m = stuck_payout(h, "late")
    m.port.deliver()
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.state == "BLOCKED"
    assert "reconcile_seller_money records it" in out.proposal.reasons[0]


def test_object_appearing_after_approval_fails_closed(h):
    m = stuck_payout(h, "late")
    out = resolve(m, "payout", "c1", "not_landed", tamper=m.port.deliver)
    assert out.state in ("FAILED", "BLOCKED")
    assert m.ledger("bob").payable.is_zero()


def test_blocked_for_an_item_not_in_flight(h, market):
    market.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    market.offset_s = 7 * DAY
    assert resolve(market, "payout", "c1", "not_landed").state == "BLOCKED"


def test_charge_not_landed_keeps_the_receivable_and_allows_a_new_attempt_once_confirmed(h):
    m = stuck_charge(h)
    out = resolve(m, "charge", "1", "not_landed")
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert st.recovery_attempts(m.stores["bob"])[0].outcome == "FAILED"
    assert m.ledger("bob").receivable == h.usd(300)
    assert m.run(h.RECOVER, {}, "bob").state == "BLOCKED"  # not confirmed yet
    confirm(h, m)
    nxt = m.run(h.RECOVER, {}, "bob")
    assert nxt.proposal.computed["attempt"] == 2  # its own key segment
    assert nxt.state == "SUCCEEDED", (nxt.detail, nxt.postcondition)
    assert [o["amount"] for o in m.charges_from("bob")] == [300]
    assert m.ledger("bob").receivable.is_zero()
    assert m.balanced()


def test_an_unconfirmed_charge_watch_is_never_netted_off_a_payout(h):
    m = stuck_charge(h)
    resolve(m, "charge", "1", "not_landed")
    m.add_cycle("bob", "c1b", 1000, 0, 0)
    m.run(h.SETTLE, {"cycle_id": "c1b"}, "bob")
    assert m.settled("bob", "c1b").receivable_offset.is_zero()
    assert m.ledger("bob").receivable == h.usd(300)


def test_charge_landed_clears_the_receivable(h):
    m = stuck_charge(h)
    out = resolve(m, "charge", "1", "landed")
    assert out.state == "SUCCEEDED", out.postcondition
    assert m.ledger("bob").receivable.is_zero()
    assert m.ledger("bob").recovered == h.usd(300)


# --- the watch finds a late landing ---------------------------------------------------


def test_probe_payout_ruled_not_landed_then_landing_is_not_paid_twice(h):
    m, decision = ruled(h, "payout")
    m.port.deliver()  # the original arrives after all
    rec = m.run(h.RECONCILE, {}, "bob")
    assert rec.state == "POSTCONDITION_FAILED"
    assert decision.proposal.proposal_id in rec.postcondition.detail
    flagged = st.needs_person(m.stores["bob"])
    assert [f["ref"] for f in flagged] == ["c1"]
    assert m.ledger("bob").payable.is_zero()
    m.add_cycle("bob", "c2", 0, 0, 8000)
    assert m.run(h.SETTLE, {"cycle_id": "c2"}, "bob").state == "SUCCEEDED"
    assert [o["amount"] for o in m.transfers_to("bob")] == [6960]
    assert m.ledger("bob").paid_out == h.usd(6960)
    assert m.balanced()


def test_settlement_holds_while_a_watched_payout_shows_at_airwallex(h):
    m, _ = ruled(h, "payout")
    m.port.deliver()
    m.add_cycle("bob", "c2", 0, 0, 8000)
    c2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert c2.state == "BLOCKED"
    assert "shows at Airwallex (SETTLED)" in c2.proposal.reasons[0]
    assert len(m.transfers_to("bob")) == 1


def test_open_watches_show_in_every_later_settlement(h):
    m, decision = ruled(h, "payout")
    confirm(h, m)
    m.add_cycle("bob", "c2", 0, 0, 8000)
    c2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob", approve=False)
    assert any(
        "watching 1 not_landed" in r and decision.proposal.proposal_id in r
        for r in c2.proposal.reasons
    )


def test_watch_catches_a_landing_after_a_confirmed_repay_and_books_the_overpayment(h):
    """Past the confirming read nothing more can be prevented: if the original then lands,
    the watch finds it with no window limit, books it as owed back and flags a person."""
    m, _ = ruled(h, "payout")
    confirm(h, m)
    m.add_cycle("bob", "c2", 0, 0, 8000)
    assert m.run(h.SETTLE, {"cycle_id": "c2"}, "bob").state == "SUCCEEDED"
    m.offset_s = 60 * DAY
    m.fund(h.usd(6960))
    m.own_cash = m.own_cash + h.usd(6960)
    m.port.deliver()
    assert m.run(h.RECONCILE, {}, "bob").state == "POSTCONDITION_FAILED"
    assert m.ledger("bob").receivable == h.usd(6960)
    assert [f["ref"] for f in st.needs_person(m.stores["bob"])] == ["c1"]
    assert m.balanced()


def test_probe_charge_ruled_not_landed_then_landing_is_not_taken_twice(h):
    m, decision = ruled(h, "charge")
    m.port.deliver()
    held = m.run(h.RECOVER, {}, "bob")
    assert held.state == "BLOCKED"
    assert "shows at Airwallex (SETTLED)" in held.proposal.reasons[0]
    rec = m.run(h.RECONCILE, {}, "bob")
    assert decision.proposal.proposal_id in rec.postcondition.detail
    assert m.ledger("bob").receivable.is_zero()
    assert m.ledger("bob").recovered == h.usd(300)
    assert [o["amount"] for o in m.charges_from("bob")] == [300]
    assert m.balanced()


# --- prevented, not just detected ------------------------------------------------------


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_a_landing_between_decision_and_confirmation_is_never_moved_twice(h, kind):
    """The reviewer's re-pay-before-landing probe: on 3748b2e c2 re-paid at once and then the
    original landed, [6960, 6960]. Now nothing moves again until a day-later clean read."""
    m, _ = ruled(h, kind)
    m.offset_s += 3600
    if kind == "payout":
        m.add_cycle("bob", "c2", 0, 0, 8000)
        assert m.run(h.SETTLE, {"cycle_id": "c2"}, "bob").state == "BLOCKED"
    else:
        assert m.run(h.RECOVER, {}, "bob").state == "BLOCKED"
    m.port.deliver()
    m.offset_s += DAY
    m.run(h.RECONCILE, {}, "bob")
    if kind == "payout":
        m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
        assert [o["amount"] for o in m.transfers_to("bob")] == [6960]
    else:
        m.run(h.RECOVER, {}, "bob")
        assert [o["amount"] for o in m.charges_from("bob")] == [300]
    assert m.balanced()


def test_counterparty_scan_blocks_the_decision_on_an_unexplained_movement(h):
    """The ?request_id= read is clean but Airwallex lists an unexplained 6960 to bob since
    the first send (e.g. the list lag fooled the request_id read): the decision is BLOCKED."""
    m = stuck_payout(h, "late")
    m.port.deliver()
    rid = m.port.executed[0].request_id
    for o in m.fake._state.kind("connected_account_transfers").values():
        if o["request_id"] == rid:
            o["request_id"] = "someone-elses"
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.state == "BLOCKED"
    assert "Airwallex lists" in out.proposal.reasons[0]


# --- the watch cannot be lost --------------------------------------------------------------


def _kill_between_the_two_writes(monkeypatch, kind):
    """Whichever of the decision record and the ledger move comes second raises: the
    Lambda dies between the two writes, whatever their order."""
    import awx_k8_settlement.resolve as rs

    target = "apply_payout_outcome" if kind == "payout" else "apply_charge_outcome"
    calls = []

    def wrap(fn):
        def inner(*a, **k):
            calls.append(fn)
            if len(calls) == 2:
                raise RuntimeError("Lambda killed between the two writes")
            return fn(*a, **k)

        return inner

    monkeypatch.setattr(rs, target, wrap(getattr(rs, target)))
    monkeypatch.setattr(st, "record_resolution", wrap(st.record_resolution))


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_crash_between_the_decision_record_and_the_ledger_move_loses_nothing(h, kind, monkeypatch):
    """The record (with the watch) is written before the ledger move. In the other order a
    crash between them would leave no watch, and a late landing would be paid or charged
    twice ([6960, 6960] / [300, 300])."""
    m = stuck_payout(h, "late") if kind == "payout" else stuck_charge(h, "late")
    ref = "c1" if kind == "payout" else "1"
    _kill_between_the_two_writes(monkeypatch, kind)
    first = resolve(m, kind, ref, "not_landed")
    monkeypatch.undo()
    assert first.state == "FAILED"
    m.port.deliver()  # the original lands
    m.add_cycle("bob", "c2", 1000, 0, 0)
    for _ in range(2):  # whatever a busy operator does next
        m.run(h.RECONCILE, {}, "bob")
        m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
        if kind == "charge":
            m.run(h.RECOVER, {}, "bob")
    if kind == "payout":
        amounts = [o["amount"] for o in m.transfers_to("bob")]
        assert amounts.count(6960) == 1  # one c1 payout, whatever c2 paid
        assert m.ledger("bob").paid_out == h.usd(sum(amounts))
    else:
        assert [o["amount"] for o in m.charges_from("bob")] == [300]
        assert m.ledger("bob").recovered == h.usd(300)
    assert m.balanced()


def crashed_decision(h, kind, monkeypatch):
    """A not_landed decision whose run died after its record and before its ledger move."""
    import awx_k8_settlement.resolve as rs

    m = stuck_payout(h) if kind == "payout" else stuck_charge(h)
    ref = "c1" if kind == "payout" else "1"
    target = "apply_payout_outcome" if kind == "payout" else "apply_charge_outcome"
    monkeypatch.setattr(rs, target, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    first = resolve(m, kind, ref, "not_landed")
    monkeypatch.undo()
    assert first.state == "FAILED"
    assert wt.decision_applied(m.stores["bob"], st.resolution(m.stores["bob"], kind, ref)) == (
        "missing"
    )
    return m, ref, first


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_a_re_proposal_inside_the_ttl_is_blocked_at_policy_not_failed_at_build(
    h, kind, monkeypatch
):
    """Approvers never sign a re-proposal that would then fail at build."""
    m, ref, first = crashed_decision(h, kind, monkeypatch)
    m.offset_s += 3600  # one hour on: the original's run may still finish it
    again = resolve(m, kind, ref, "not_landed")
    assert again.state == "BLOCKED"
    assert first.proposal.proposal_id in again.proposal.reasons[0]
    assert "may adopt it after" in again.proposal.reasons[0]


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_a_stranded_decision_is_adopted_past_the_ttl_and_applied_once(h, kind, monkeypatch):
    """A decision whose run died is never stranded for good."""
    m, ref, first = crashed_decision(h, kind, monkeypatch)
    m.offset_s += 5 * 3600  # past decided_at + APPROVAL_TTL (4 h)
    other = resolve(m, kind, ref, "landed")
    assert other.state == "BLOCKED"  # a different resolution is still refused
    again = resolve(m, kind, ref, "not_landed")
    assert again.proposal.risk_tier is RiskTier.DUAL
    assert first.proposal.proposal_id in again.proposal.reasons[0]
    assert "Adopts the decision" in again.proposal.reasons[0]
    assert again.state == "SUCCEEDED", (again.detail, again.postcondition)
    recs = m.stores["bob"].query("resolution/")
    assert len(recs) == 1
    rec = recs[0].value
    assert rec["owner_proposal_id"] == again.proposal.proposal_id
    assert rec["previous_owner_proposal_id"] == first.proposal.proposal_id
    if kind == "payout":
        assert m.ledger("bob").payable == h.usd(6960)
        assert m.ledger("bob").payout_pending.is_zero()
    else:
        assert st.recovery_attempts(m.stores["bob"])[0].outcome == "FAILED"
        assert m.ledger("bob").receivable == h.usd(300)
    assert m.balanced()


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_the_adopting_build_twice_for_one_proposal(h, kind, monkeypatch):
    m, ref, _ = crashed_decision(h, kind, monkeypatch)
    m.offset_s += 5 * 3600
    out = resolve(m, kind, ref, "not_landed", spec=twice(SPECS["resolve_in_flight_money"]))
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert len(m.stores["bob"].query("resolution/")) == 1
    if kind == "payout":
        assert m.ledger("bob").payable == h.usd(6960)
    else:
        assert m.ledger("bob").receivable == h.usd(300)


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_a_late_resuming_original_cannot_win_after_adoption(h, kind, monkeypatch):
    """Mutual exclusion: once adopted, the original proposal's retried build is refused."""
    m, ref, first = crashed_decision(h, kind, monkeypatch)
    m.offset_s += 5 * 3600
    assert resolve(m, kind, ref, "not_landed").state == "SUCCEEDED"
    with pytest.raises(st.StateConflict):
        st.adopt_resolution(
            m.stores["bob"], kind, ref, first.proposal.proposal_id, "proposal-late", m.now()
        )


# --- only Airwallex's final states close a watch -------------------------


def _deliver_then(m, status, kind):
    m.port.deliver()
    rid = (
        m.port.executed[0]
        if kind == "payout"
        else next(op for op in m.port.executed if op.name == CHARGE)
    ).request_id
    if status:
        m.port.set_status(rid, status)
    return rid


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_pending_then_failed_keeps_the_watch_open_then_closes_it(h, kind):
    m, _ = ruled(h, kind)
    rid = _deliver_then(m, "PENDING", kind)
    rec = m.run(h.RECONCILE, {}, "bob")
    assert "shows at Airwallex as PENDING" in rec.postcondition.detail
    assert len(wt.open_watches(m.stores["bob"])) == 1
    led = m.ledger("bob")
    assert led.paid_out.is_zero()
    assert led.recovered.is_zero()
    if kind == "payout":
        m.add_cycle("bob", "c2", 0, 0, 8000)
        assert m.run(h.SETTLE, {"cycle_id": "c2"}, "bob").state == "BLOCKED"
    else:
        assert m.run(h.RECOVER, {}, "bob").state == "BLOCKED"
    m.port.set_status(rid, "FAILED")
    rec2 = m.run(h.RECONCILE, {}, "bob")
    assert "own failure state (FAILED)" in rec2.postcondition.detail
    assert wt.open_watches(m.stores["bob"]) == ()
    if kind == "payout":
        assert m.ledger("bob").payable == h.usd(6960)
    else:
        assert m.ledger("bob").receivable == h.usd(300)
    assert m.balanced()


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_pending_then_settled_posts_the_late_landing(h, kind):
    m, _ = ruled(h, kind)
    rid = _deliver_then(m, "PENDING", kind)
    m.run(h.RECONCILE, {}, "bob")
    m.port.set_status(rid, "SETTLED")
    rec = m.run(h.RECONCILE, {}, "bob")
    assert "LANDED after all" in rec.postcondition.detail
    if kind == "payout":
        assert m.ledger("bob").paid_out == h.usd(6960)
        assert m.ledger("bob").payable.is_zero()
    else:
        assert m.ledger("bob").recovered == h.usd(300)
        assert m.ledger("bob").receivable.is_zero()
    assert m.balanced()


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_cancelled_with_a_failure_block_closes_as_confirmed_failed(h, kind):
    m, _ = ruled(h, kind)
    rid = _deliver_then(m, "FAILED", kind)  # money returned by the fake
    for k in h.KINDS:
        for o in m.fake._state.kind(k).values():
            if o["request_id"] == rid:
                o["status"] = "CANCELLED"
                o["failure"] = {"code": "x"}
    rec = m.run(h.RECONCILE, {}, "bob")
    assert "own failure state (CANCELLED)" in rec.postcondition.detail
    assert wt.open_watches(m.stores["bob"]) == ()
    led = m.ledger("bob")
    assert led.paid_out.is_zero()
    assert led.recovered.is_zero()
    assert m.balanced()


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_cancelled_without_a_failure_block_stays_watched(h, kind):
    m, _ = ruled(h, kind)
    rid = _deliver_then(m, "PENDING", kind)
    for k in h.KINDS:
        for o in m.fake._state.kind(k).values():
            if o["request_id"] == rid:
                o["status"] = "CANCELLED"
    rec = m.run(h.RECONCILE, {}, "bob")
    assert "shows at Airwallex as CANCELLED" in rec.postcondition.detail
    assert len(wt.open_watches(m.stores["bob"])) == 1


# --- only an empty read-back may be resolved ----------------------------------------------


def test_two_objects_under_one_request_id_blocks_the_decision(h):
    m = stuck_payout(h, "late")
    m.port.deliver()
    rid = m.port.executed[0].request_id
    m.port.duplicated.add(rid)
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.state == "BLOCKED"
    assert rid in out.proposal.reasons[0]


def test_a_mismatched_object_blocks_the_decision(h):
    m = stuck_payout(h, "late")
    m.port.deliver()
    rid = m.port.executed[0].request_id
    for o in m.fake._state.kind("connected_account_transfers").values():
        if o["request_id"] == rid:
            o["amount"] = 1
    out = resolve(m, "payout", "c1", "landed")
    assert out.state == "BLOCKED"
    assert rid in out.proposal.reasons[0]


def test_a_reply_with_no_items_list_blocks_the_decision(h, monkeypatch):
    m = stuck_payout(h)
    real = m.port.read
    monkeypatch.setattr(m.port, "read", lambda req: {"oops": 1} if req.query else real(req))
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.state == "BLOCKED"
    assert "no items list" in out.proposal.reasons[0]


def test_a_429_on_the_read_back_blocks_the_decision(h):
    from awx_spine.fake import Fault

    m = stuck_payout(h)
    # the engine retries a transient read 3 times, so 3 faults
    # are absorbed and the decision is made from a clean read. Inject more than it retries.
    m.fake.inject("connected_account_transfers.list", Fault.RATE_LIMIT, times=20)
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.state == "BLOCKED"
    assert m.ledger("bob").payable.is_zero()
    m.fake.clear_faults()


def test_a_429_the_engine_absorbs_still_resolves_on_a_clean_read(h):
    """The other half: 3 transient faults are retried away and the read that decides is
    a real, empty one."""
    from awx_spine.fake import Fault

    m = stuck_payout(h)
    m.fake.inject("connected_account_transfers.list", Fault.RATE_LIMIT, times=3)
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)


# --- runner rule 0: every changed build run twice -----------------------


@pytest.mark.parametrize(
    ("kind", "resolution"),
    [
        ("payout", "not_landed"),
        ("payout", "landed"),
        ("charge", "not_landed"),
        ("charge", "landed"),
    ],
)
def test_resolve_build_twice_for_one_proposal(h, kind, resolution):
    m = stuck_payout(h) if kind == "payout" else stuck_charge(h)
    ref = "c1" if kind == "payout" else "1"
    once = stuck_payout(h) if kind == "payout" else stuck_charge(h)
    resolve(once, kind, ref, resolution)
    out = resolve(m, kind, ref, resolution, spec=twice(SPECS["resolve_in_flight_money"]))
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert len(m.stores["bob"].query("resolution/")) == 1
    assert len(wt.open_watches(m.stores["bob"])) == (1 if resolution == "not_landed" else 0)
    assert m.ledger("bob").to_record() | {"version": 0} == (
        once.ledger("bob").to_record() | {"version": 0}
    )


def test_settle_build_twice_new_cycle_and_resend(h):
    m = h.Market(h.bob_c1_net())
    out = m.run(twice(h.SETTLE), {"cycle_id": "c1"}, "bob")
    assert out.state == "SUCCEEDED", out.postcondition
    assert [o["amount"] for o in m.transfers_to("bob")] == [6960]
    m2 = h.Market(h.bob_c1_net())
    m2.port.script(CAT, "late", *(["lost"] * MAX_RESENDS))
    m2.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    from awx_common import Origin

    again = m2.run(twice(h.SETTLE), {"cycle_id": "c1"}, "bob", origin=Origin.SCHEDULE)
    assert again.state == "SUCCEEDED", again.postcondition
    m2.port.deliver()
    assert len(m2.transfers_to("bob")) == 1


def test_settle_build_twice_with_a_confirmed_watch_open(h):
    m, _ = ruled(h, "payout", first="lost")
    confirm(h, m)
    m.add_cycle("bob", "c2", 0, 0, 8000)
    out = m.run(twice(h.SETTLE), {"cycle_id": "c2"}, "bob")
    assert out.state == "SUCCEEDED", out.postcondition
    assert [o["amount"] for o in m.transfers_to("bob")] == [6960]
    assert len(wt.open_watches(m.stores["bob"])) == 1


def test_recover_build_twice_new_attempt_and_resend(h):
    m = stuck_charge(h, "ok")
    m.offset_s = 0
    assert len(m.charges_from("bob")) == 1  # attempt 1 collected
    m2 = h.Market(h.usd(0))
    m2.fund(h.usd(300))
    m2.own_cash = h.usd(300)
    m2.add_cycle("bob", "c0", 0, 300, 0)
    m2.run(h.SETTLE, {"cycle_id": "c0"}, "bob")
    m2.set_wallet("bob", h.usd(1000))
    out = m2.run(twice(h.RECOVER), {}, "bob")
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert [o["amount"] for o in m2.charges_from("bob")] == [300]
    m3 = stuck_charge(h, "late")
    m3.offset_s = 0
    from awx_common import Origin

    again = m3.run(twice(h.RECOVER), {}, "bob", origin=Origin.SCHEDULE)
    assert again.state == "SUCCEEDED", (again.detail, again.postcondition)
    m3.port.deliver()
    assert [o["amount"] for o in m3.charges_from("bob")] == [300]


def test_recover_build_twice_with_a_confirmed_charge_watch_open(h):
    m, _ = ruled(h, "charge", first="lost")
    confirm(h, m)
    out = m.run(twice(h.RECOVER), {}, "bob")
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert [o["amount"] for o in m.charges_from("bob")] == [300]


def test_reconcile_build_twice_with_a_watched_landing(h):
    m, _ = ruled(h, "payout")
    m.port.deliver()
    out = m.run(twice(h.RECONCILE), {}, "bob")
    state = st.ledger_state(m.stores["bob"], "bob", "USD")
    assert sum(1 for k in state.postings if k.startswith("payout-late:")) == 1
    assert out.state == "POSTCONDITION_FAILED"  # a person must look, once
    assert len(st.needs_person(m.stores["bob"])) == 1


# --- a showing charge watch also holds settlement --------------------------


def test_settlement_does_not_net_a_receivable_a_watched_charge_has_collected(h):
    """The reviewer's probe: on 8c811a1 c2 netted 200 off its payout while the original 300
    charge had collected, so bob paid 500 against 300 owed."""
    m, _ = ruled(h, "charge")
    confirm(h, m)
    m.port.deliver()  # the original charge lands and is visible
    m.add_cycle("bob", "c2", 1000, 0, 0)
    c2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert c2.state == "BLOCKED"
    assert "charge 1 ruled not_landed" in c2.proposal.reasons[0]
    assert m.settled("bob", "c2") is None
    m.run(h.RECONCILE, {}, "bob")
    c2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert c2.state == "SUCCEEDED", c2.postcondition
    assert m.settled("bob", "c2").receivable_offset.is_zero()  # the charge already paid it
    assert [o["amount"] for o in m.charges_from("bob")] == [300]
    assert m.ledger("bob").payable.is_zero()  # nothing over-collected
    assert m.balanced()


# --- the counterparty scan fails closed on replies it cannot read ---------


def _unexplained_transfer(m, **fields):
    rid = m.port.executed[0].request_id
    for o in m.fake._state.kind("connected_account_transfers").values():
        if o["request_id"] == rid:
            o["request_id"] = "someone-elses"
            o.update(fields)


@pytest.mark.parametrize(
    "created_at", ["2026-10-06T00:00:01.123+0000", "2026-10-06T00:00:01", None]
)
def test_an_unreadable_created_at_counts_as_a_match(h, created_at):
    m = stuck_payout(h, "late")
    m.port.deliver()
    _unexplained_transfer(m, created_at=created_at)
    if created_at is None:
        for o in m.fake._state.kind("connected_account_transfers").values():
            o.pop("created_at", None)
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.state == "BLOCKED"
    assert "Airwallex lists" in out.proposal.reasons[0]


def test_a_reply_without_has_more_keeps_paging(h, monkeypatch):
    """The match is on page 3 behind 250 fillers and the reply carries no has_more."""
    m = stuck_payout(h, "late")
    m.port.deliver()
    _unexplained_transfer(m)
    real = m.port.read
    match = next(
        o
        for o in m.fake._state.kind("connected_account_transfers").values()
        if o["request_id"] == "someone-elses"
    )
    filler = {**match, "id": "tr_f", "request_id": "f", "destination": "acct_someone"}
    rows = [dict(filler, id=f"tr_f{i}") for i in range(250)] + [m.fake._public(match)]

    def read(req):
        q = req.query or {}
        if req.path == "/api/v1/connected_account_transfers" and "page_num" in q:
            n, size = int(q["page_num"]), int(q["page_size"])
            return {"items": rows[n * size : (n + 1) * size]}  # no has_more at all
        return real(req)

    monkeypatch.setattr(m.port, "read", read)
    out = resolve(m, "payout", "c1", "not_landed")
    assert out.state == "BLOCKED"
    assert "Airwallex lists" in out.proposal.reasons[0]


# --- a rebuilt request_id that differs reads undetermined ----------------


def test_a_watch_whose_request_id_no_longer_matches_holds_settlement(h):
    m, _ = ruled(h, "payout", first="lost")
    confirm(h, m)
    key = "resolution/payout/c1"
    item = m.stores["bob"].get(key)
    m.stores["bob"].put(
        key, {**item.value, "request_id": "a-different-id"}, expected_version=item.version
    )
    m.add_cycle("bob", "c2", 0, 0, 8000)
    c2 = m.run(h.SETTLE, {"cycle_id": "c2"}, "bob")
    assert c2.state == "BLOCKED"
    assert "rebuilt request_id differs" in c2.proposal.reasons[0]


# --- adoption timing ----------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["payout", "charge"])
def test_a_live_adopter_cannot_be_pre_empted_by_a_second_adopter(h, kind, monkeypatch):
    """Adoption opens at max(decided_at, adopted_at) + the wait: a record adopted at +5 h is
    not adoptable again at +5 h 1 min, even though decided_at + 4 h is long past."""
    import awx_k8_settlement.resolve as rs

    m, ref, _first = crashed_decision(h, kind, monkeypatch)
    m.offset_s += 5 * 3600
    target = "apply_payout_outcome" if kind == "payout" else "apply_charge_outcome"
    monkeypatch.setattr(rs, target, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("y")))
    adopter = resolve(m, kind, ref, "not_landed")  # adopts, then its own run dies too
    monkeypatch.undo()
    assert adopter.state == "FAILED"
    rec = st.resolution(m.stores["bob"], kind, ref)
    assert rec["owner_proposal_id"] == adopter.proposal.proposal_id
    m.offset_s += 60
    m.port.executed.clear()
    second = m.run(
        SPECS["resolve_in_flight_money"],
        {"kind": kind, "ref": ref, "resolution": "not_landed", "evidence_ref": "awx-case:4712"},
        "bob",
    )
    assert second.state == "BLOCKED"
    assert adopter.proposal.proposal_id in second.proposal.reasons[0]


def test_the_adoption_wait_outlasts_the_build_tasks_retry_budget():
    """6 retries are 7 attempts, 546 s. The
    synthesised Retry lists are checked against these constants in test_synth_k8.py."""
    from datetime import timedelta

    from awx_k8_settlement.common import ADOPTION_WAIT, BUILD_RETRY_BUDGET, BUILD_TASK_ATTEMPTS

    assert BUILD_TASK_ATTEMPTS == 7
    assert timedelta(seconds=546) == BUILD_RETRY_BUDGET
    assert BUILD_RETRY_BUDGET < ADOPTION_WAIT


def test_recalculate_build_twice_for_one_proposal(h, market):
    """Rule 0 for the last build without a twice test: one hold, nothing sent."""
    out = market.run(twice(h.RECALC), {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"}, "bob")
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert len(st.overrides(market.stores["bob"])) == 1
    assert market.port.executed == []
    assert market.run(h.SETTLE, {"cycle_id": "c1"}, "bob").state == "SUCCEEDED"
    assert market.ledger("bob").reserve_held == h.usd(3000)
