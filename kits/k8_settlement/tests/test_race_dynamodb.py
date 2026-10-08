"""Netting versus recovery on DynamoDbKitStore (moto), through the real runner: a settlement
that nets a seller's receivable and a recovery charge that collects it race on the ledger
item's versioned update, and the debt is taken exactly once, on the spine's DynamoDB
store."""

import threading

from awx_common import Money
from awx_common.store import VersionConflict
from awx_k8_settlement import store as st

CHARGE = "charges.create"


def dyn_market(h, dynamodb):
    """bob owes 300 and has 1000 in his wallet; cycle c1 (1000 earned) is ready to settle.
    bob's ledger is on DynamoDB, behind a HookStore that can interleave a second run."""
    make = h.dynamo_factory(dynamodb)
    m = h.Market(h.usd(0), store_factory=lambda s: h.HookStore(make(s)))
    m.fund(h.usd(300))
    m.own_cash = h.usd(300)
    m.add_cycle("bob", "c0", 0, 300, 0)
    assert m.run(h.SETTLE, {"cycle_id": "c0"}, "bob").state == "SUCCEEDED"
    m.set_wallet("bob", h.usd(1000))
    m.add_cycle("bob", "c1", 1000, 0, 0)  # replaces the seeded c1
    return m


def taken_once(h, m):
    netted = m.settled("bob", "c1").receivable_offset
    collected = sum(
        (Money.of(o["amount"], "USD") for o in m.charges_from("bob") if o["status"] == "SETTLED"),
        h.usd(0),
    )
    assert netted + collected == h.usd(300)
    led = m.ledger("bob")
    assert led.receivable.is_zero()
    assert led.payable.is_zero()  # nothing was over-collected and handed back
    assert m.balanced()


def test_store_is_dynamodb(h, dynamodb):
    from awx_spine_aws.store import DynamoDbKitStore

    m = dyn_market(h, dynamodb)
    assert isinstance(m.stores["bob"].inner, DynamoDbKitStore)
    assert m.stores["bob"].get("ledger").version >= 1


def test_settle_nets_first_then_the_recovery_claim_fails_closed(h, dynamodb):
    m = dyn_market(h, dynamodb)
    seen = {}

    def settle_between():
        seen["settle"] = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")

    m.stores["bob"].arm(settle_between)  # fires at recover's claim write
    out = m.run(h.RECOVER, {}, "bob")
    assert seen["settle"].state == "SUCCEEDED", seen["settle"].postcondition
    assert m.settled("bob", "c1").receivable_offset == h.usd(300)
    assert out.state == "FAILED"
    assert "StateConflict" in (out.detail or "")
    assert m.charges_from("bob") == []
    taken_once(h, m)


def test_recover_claims_first_then_the_settlement_write_fails_and_reruns(h, dynamodb):
    m = dyn_market(h, dynamodb)
    seen = {}

    def recover_between():
        seen["recover"] = m.run(h.RECOVER, {}, "bob")

    m.stores["bob"].arm(recover_between)  # fires at the settlement's ledger post
    first = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert seen["recover"].state == "SUCCEEDED", seen["recover"].postcondition
    assert first.state == "FAILED"
    assert m.settled("bob", "c1") is None
    again = m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    assert again.state == "SUCCEEDED", again.postcondition
    assert m.settled("bob", "c1").receivable_offset.is_zero()
    assert len(m.charges_from("bob")) == 1
    taken_once(h, m)


def test_claim_then_crash_before_the_send_keeps_the_receivable_out_of_netting(
    h, dynamodb, monkeypatch
):
    m = dyn_market(h, dynamodb)
    real = st.mark_sent

    def crash(store, kind, *a, **k):
        if kind == "charge":
            raise RuntimeError("process died")
        return real(store, kind, *a, **k)

    monkeypatch.setattr(st, "mark_sent", crash)
    out = m.run(h.RECOVER, {}, "bob")
    monkeypatch.undo()
    assert out.state == "FAILED"
    assert all(op.name != CHARGE for op in m.port.executed)
    assert st.recovery_attempts(m.stores["bob"])[0].outcome == "PENDING"
    assert m.run(h.SETTLE, {"cycle_id": "c1"}, "bob").state == "SUCCEEDED"
    assert m.settled("bob", "c1").receivable_offset.is_zero()
    resend = m.run(h.RECOVER, {}, "bob")
    assert resend.proposal.computed["mode"] == "resend"
    assert resend.state == "SUCCEEDED", (resend.detail, resend.postcondition)
    taken_once(h, m)


def test_two_threads_writing_the_claim_and_the_netting_from_one_version(h, dynamodb):
    """The race at the store itself: from one ledger version, exactly one of a recovery claim
    and a netting post lands on DynamoDB."""
    m = dyn_market(h, dynamodb)
    kstore = m.stores["bob"].inner
    state = st.ledger_state(kstore, "bob", "USD")
    from awx_k8_settlement.common import compute_settlement
    from awx_k8_settlement.reserve import DEFAULT_POLICY

    settlement, new = compute_settlement(kstore, state, DEFAULT_POLICY, "bob", "c1")
    assert settlement.receivable_offset == h.usd(300)
    claim = st.RecoveryAttempt(1, h.usd(300), "rid-1", "PENDING", "proposal-a")
    barrier = threading.Barrier(2)
    results = {}

    def do(name, fn):
        barrier.wait()
        try:
            fn()
            results[name] = "won"
        except (st.StateConflict, VersionConflict):
            results[name] = "lost"

    threads = [
        threading.Thread(
            target=do, args=("claim", lambda: st.claim_recovery(kstore, state, claim))
        ),
        threading.Thread(
            target=do,
            args=("net", lambda: st.post(kstore, state, "settle:c1", new, settlement.to_record())),
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ["lost", "won"]
    fresh = st.ledger_state(kstore, "bob", "USD")
    netted = st.settlement(fresh, "c1") is not None
    claimed = any(a.attempt == 1 for a in st.attempts_in(fresh))
    assert netted != claimed  # exactly one took the receivable
