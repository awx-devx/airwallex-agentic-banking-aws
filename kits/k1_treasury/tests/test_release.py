"""The release tool (propose_release_obligation) through the real runner."""

from __future__ import annotations

from datetime import timedelta

import pytest
from awx_common import RiskTier
from awx_k1_treasury import release as release_mod
from awx_k1_treasury.locks import LockState, read_lock
from awx_spine.fake import Fault

SUPPLIER = {"obligation_ids": ["ob-supplier-eur"]}
OFFICE = {"obligation_ids": ["ob-office-usd"]}


def lock(env, oid):
    return read_lock(env.store, oid)


def test_converted_but_refused_transfer_is_released_by_one_person_and_repaid(env):
    env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    env.pay(SUPPLIER)
    assert lock(env, "ob-supplier-eur").state is LockState.NEEDS_PERSON
    rel = env.release(
        "ob-supplier-eur",
        "transfer refused at create; EUR is in the wallet",
        approvers=["approver"],
    )
    assert rel.proposal.risk_tier is RiskTier.SINGLE
    assert rel.state == "SUCCEEDED", rel.detail
    assert lock(env, "ob-supplier-eur").state is LockState.RELEASED
    assert env.port.names().count("transfers.create") == 1  # the release sent nothing
    # The EUR already bought sits in the wallet: the next plan converts nothing and pays.
    again = env.execute(SUPPLIER)
    assert again.state == "SUCCEEDED", again.detail
    assert again.proposal.computed["conversions"] == []
    assert lock(env, "ob-supplier-eur").attempt == 2
    assert len(env.conversions()) == 1
    assert [t["status"] for t in env.transfers()] == ["PAID"]


def test_in_flight_transfer_cannot_be_released(make_env):
    env = make_env(advance=False)
    env.execute(OFFICE)
    assert lock(env, "ob-office-usd").state is LockState.SUBMITTED
    rel = env.release("ob-office-usd", "operator thinks it is stuck", approvers=["a1", "a2"])
    assert rel.state == "BLOCKED"
    assert lock(env, "ob-office-usd").state is LockState.SUBMITTED


def test_a_transfer_that_paid_is_marked_paid(make_env):
    env = make_env(advance=False)
    env.execute(OFFICE)
    (t,) = env.transfers()
    env.fake.simulate("transfer_transition", transfer_id=t["id"], next_status="SENT")
    env.fake.simulate("transfer_transition", transfer_id=t["id"], next_status="PAID")
    rel = env.release("ob-office-usd", "the provider shows it paid", approvers=["approver"])
    assert rel.state == "SUCCEEDED", rel.detail
    assert lock(env, "ob-office-usd").state is LockState.PAID
    assert env.execute(OFFICE).state == "BLOCKED"


def test_invisible_transfer_needs_two_people_past_the_dedup_window(env):
    env.fake.inject(
        "transfers.create",
        Fault.TIMEOUT_AFTER_SUBMIT,
        visible_after_s=timedelta(days=30).total_seconds(),
    )
    env.execute(OFFICE)
    early = env.release("ob-office-usd", "it never showed up", approvers=["a1", "a2"])
    assert early.state == "BLOCKED"  # it may still land inside the window
    env.advance(timedelta(days=8).total_seconds())
    late = env.release("ob-office-usd", "it never showed up after 8 days", approvers=["a1", "a2"])
    assert late.proposal.risk_tier is RiskTier.DUAL
    assert late.state == "SUCCEEDED", late.detail
    assert lock(env, "ob-office-usd").state is LockState.RELEASED


def test_a_lock_that_moved_after_the_proposal_fails_closed(env):
    env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    env.pay(SUPPLIER)

    def someone_else_releases(p):
        from awx_k1_treasury.locks import operator_set

        cur = read_lock(env.store, "ob-supplier-eur")
        operator_set(
            env.store,
            "ob-supplier-eur",
            expected_store_version=cur.version,
            state=LockState.RELEASED,
            note="other operator",
        )

    rel = env.release(
        "ob-supplier-eur",
        "transfer refused at create",
        approvers=["approver"],
        during_approval=someone_else_releases,
    )
    assert rel.state == "FAILED"


def test_nothing_to_release(env):
    assert env.release("ob-office-usd", "no lock exists for this one").state == "BLOCKED"


# --- a release racing a run that is sending -----------------------------------------------


def test_release_inside_the_transfer_window_pays_once(env):
    """A person's release runs inside the hook before execute_plan's first /transfers read,
    while the run holds the LOCKED lock and has emitted nothing. The release is BLOCKED, run 1
    pays, and run 2 is BLOCKED, so the obligation is paid once."""
    released = []
    env.port.before_read(
        "/api/v1/transfers",
        lambda: released.append(
            env.release("ob-office-usd", "looks stuck to the operator", approvers=["approver"])
        ),
    )
    first = env.execute(OFFICE)
    (rel,) = released
    assert rel.state == "BLOCKED"  # the owner run is RUNNING (run_status)
    assert any("may still send" in r for r in rel.proposal.reasons)
    assert first.state == "SUCCEEDED", first.detail
    again = env.execute(OFFICE)
    assert again.state == "BLOCKED"
    assert len(env.transfers()) == 1


@pytest.fixture
def gate_off(monkeypatch):
    """Switch the run_status gate off: these tests prove the CAS backstop on its own."""
    import awx_k1_treasury.release as release_mod

    monkeypatch.setattr(release_mod, "owner_run_gate", lambda ctx, owner: None)


def test_a_release_that_wins_the_race_stops_the_emission(env, gate_off):
    """With the run_status gate off, the release and the emission still conflict on the
    lock and exactly one wins. Here the release wins, so the run sends no transfer, and the
    next run pays once."""
    released = []

    def late_release():
        env.advance(6 * 3600)  # six hours into the run; the gate is off
        released.append(
            env.release("ob-office-usd", "operator release after six hours", approvers=["approver"])
        )

    env.port.before_read("/api/v1/transfers", late_release)
    first = env.execute(OFFICE)
    assert released[0].state == "SUCCEEDED", released[0].detail
    assert first.state == "FAILED"
    assert "ObligationLocked" in (first.detail or "")
    assert env.transfers() == []
    retry = env.execute(OFFICE)
    assert retry.state == "SUCCEEDED", retry.detail
    assert len(env.transfers()) == 1


def test_a_claim_that_wins_the_race_makes_the_release_lose():
    from awx_common.store import InMemoryKitStore
    from awx_k1_treasury.locks import LockMoved, acquire, claim_for_emission, operator_set
    from awx_spine.fake import EPOCH

    store = InMemoryKitStore()
    acquire(store, "ob-a", "1", "p-1", EPOCH)
    seen = read_lock(store, "ob-a").version  # what a release approval would bind
    claim_for_emission(store, "ob-a", "p-1", 1, EPOCH)
    import pytest

    with pytest.raises(LockMoved):
        operator_set(
            store, "ob-a", expected_store_version=seen, state=LockState.RELEASED, note="too late"
        )
    assert read_lock(store, "ob-a").state is LockState.LOCKED


def test_release_between_convert_and_transfer_fails_the_run_closed(env, gate_off):
    """A person releases the lock after the conversion landed and before the transfer step.
    The run never retakes it at attempt 2: it fails closed and sends no transfer."""
    env.lock(SUPPLIER)
    released = []

    def release_after_convert(op, result):
        env.advance(6 * 3600)
        released.append(
            env.release("ob-supplier-eur", "operator release mid run", approvers=["approver"])
        )

    env.port.after_execute("fx.conversions.create", release_after_convert)
    out = env.execute(SUPPLIER)
    assert released[0].state == "SUCCEEDED", released[0].detail
    assert out.state == "FAILED"
    assert "ObligationLocked" in (out.detail or "")
    assert env.transfers() == []
    assert len(env.conversions()) == 1
    assert lock(env, "ob-supplier-eur").state is LockState.RELEASED
    assert lock(env, "ob-supplier-eur").attempt == 1  # the run never retook it


def test_release_evidence_names_owner_body_and_conversions(env):
    env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    env.pay(SUPPLIER)
    rel = env.release("ob-supplier-eur", "transfer refused at create", approvers=["approver"])
    ev = rel.proposal.computed["evidence"]
    assert ev["lock_owner"]
    assert ev["transfer"]["reference"].startswith("poc-i1-")
    assert ev["transfer"]["reference"].endswith("-ob-supplier-eur")
    assert len(ev["conversions"]) == 1
    assert ev["conversions"][0]["provider"][0]["status"] == "SETTLED"
    assert "lock age" in rel.proposal.reasons[0]
    # An approver is told the tier is not clearance.
    assert rel.proposal.reasons[1] == release_mod.OWNER_RUN_CHECKED_AT_EXECUTION
