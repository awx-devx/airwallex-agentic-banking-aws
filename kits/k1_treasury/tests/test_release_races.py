"""Releasing a lock while a run may still send: the record-before-claim order, the
owner-run gate, and rule 1's recheck as the last guard."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import awx_k1_treasury.release as release_mod
import awx_k1_treasury.workflow as wf
import pytest
from awx_k1_treasury.locks import LockState, ObligationLocked, operator_set, read_lock
from awx_spine.fake import Fault

SUPPLIER = {"obligation_ids": ["ob-supplier-eur"]}
EPOCH_NOW = __import__("awx_spine.fake", fromlist=["EPOCH"]).EPOCH
OFFICE = {"obligation_ids": ["ob-office-usd"]}


def lock(env, oid):
    return read_lock(env.store, oid)


@pytest.fixture
def no_gate(monkeypatch):
    """Stub the run_status gate out: these tests prove money safety does not rest on it."""
    monkeypatch.setattr(release_mod, "owner_run_gate", lambda ctx, owner: None)


def _ctx(env, proposal, prior):
    from awx_common import TenantContext
    from awx_common.workflow import PolicyContext, PolicyDecision, StepContext

    pc = PolicyContext(TenantContext("platform"), env.now(), env.ctx().read, store=env.store)
    d = PolicyDecision(proposal.risk_tier, proposal.reasons, proposal.computed)
    return StepContext(proposal.intent, d, pc, proposal.intent_hash, prior, proposal.proposal_id)


def _proposal(env, raw):
    from awx_common.approvals import Proposal

    spec = env.specs["execute_plan"]
    intent = spec.validate(raw)
    d = spec.policy(intent, env.ctx())
    return Proposal.create(
        kit="k1",
        action="execute_plan",
        intent=intent,
        computed=d.computed,
        risk_tier=d.risk_tier,
        reasons=d.reasons,
        proposed_by="requester",
        now=env.now(),
        ttl=timedelta(hours=4),
        proposal_id="p-note2",
    )


# --- record before claim; money safety does not rest on the gate ----------------------------------


def test_release_right_after_the_transfer_claim_is_blocked_and_pays_once(env, monkeypatch, no_gate):
    """With the run-status gate stubbed out, a release in the window right after the
    transfer's CAS claim is BLOCKED: the submission record is written before the claim, so the
    release sees the emission, and the obligation is paid once."""
    real = wf.claim_for_emission
    released = []

    def claim_then_release(store, oid, owner, attempt, now, *, conversion_rids=(), request_ids=()):
        real(
            store,
            oid,
            owner,
            attempt,
            now,
            conversion_rids=conversion_rids,
            request_ids=request_ids,
        )
        if not conversion_rids and not released:
            released.append(env.release(oid, "operator release mid run", approvers=["a1"]))

    monkeypatch.setattr(wf, "claim_for_emission", claim_then_release)
    first = env.execute(OFFICE)
    assert released[0].state == "BLOCKED", released[0].proposal.reasons
    assert first.state == "SUCCEEDED", first.detail
    assert env.execute(OFFICE).state == "BLOCKED"
    assert len(env.transfers()) == 1


def test_release_between_the_record_and_the_claim_is_blocked(env, monkeypatch, no_gate):
    real = wf.index_emission
    released = []

    def index_then_release(store, oid, attempt, rid, kind):
        real(store, oid, attempt, rid, kind)
        if kind == "transfer" and not released:
            released.append(env.release(oid, "operator release mid run", approvers=["a1"]))

    monkeypatch.setattr(wf, "index_emission", index_then_release)
    out = env.execute(OFFICE)
    assert released[0].state == "BLOCKED"
    assert out.state == "SUCCEEDED", out.detail
    assert len(env.transfers()) == 1


def test_a_release_written_before_the_claim_stops_the_send(env, monkeypatch, no_gate):
    """The release read the ledger before the record (nothing emitted) and its write lands
    after the record but before the claim: the claim raises and nothing is sent."""
    seen = {}

    def snapshot():
        seen["version"] = lock(env, "ob-office-usd").version  # what the release bound

    env.port.before_read("/api/v1/transfers", snapshot)
    real = wf.index_emission

    def index_then_operator_release(store, oid, attempt, rid, kind):
        real(store, oid, attempt, rid, kind)
        if kind == "transfer":
            operator_set(
                store,
                oid,
                expected_store_version=seen["version"],
                state=LockState.RELEASED,
                note="operator release",
            )

    monkeypatch.setattr(wf, "index_emission", index_then_operator_release)
    out = env.execute(OFFICE)
    assert out.state == "FAILED"
    assert "ObligationLocked" in (out.detail or "")
    assert env.transfers() == []
    monkeypatch.setattr(wf, "index_emission", real)
    retry = env.execute(OFFICE)
    assert retry.state == "SUCCEEDED", retry.detail
    assert len(env.transfers()) == 1


def test_claims_record_the_request_ids_they_cover(env):
    env.lock(SUPPLIER)
    out = env.execute(SUPPLIER)
    assert out.state == "SUCCEEDED", out.detail
    claimed = set(lock(env, "ob-supplier-eur").claimed_rids)
    assert out.results["convert"][0].request_id in claimed
    assert out.results["transfer"][0].request_id in claimed


def test_recorded_conversion_not_visible_blocks_the_release_inside_the_window(env):
    env.lock(SUPPLIER)
    env.fake.inject(
        "fx.conversions.create",
        Fault.TIMEOUT_AFTER_SUBMIT,
        visible_after_s=timedelta(days=30).total_seconds(),
    )
    env.execute(SUPPLIER)
    assert lock(env, "ob-supplier-eur").state is LockState.NEEDS_PERSON
    env.advance(2 * 3600)
    rel = env.release("ob-supplier-eur", "conversion never showed up", approvers=["a1", "a2"])
    assert rel.state == "BLOCKED"
    assert any("may still land" in r for r in rel.proposal.reasons)
    env.advance(timedelta(days=7).total_seconds())
    late = env.release("ob-supplier-eur", "conversion never showed up", approvers=["a1", "a2"])
    assert late.proposal.risk_tier.value == "DUAL_APPROVER"


def test_a_build_between_the_release_reads_is_caught_by_rule_1(env, monkeypatch, make_env):
    """The build records and claims between the release policy's
    lock read and its submission read, and the policy still sees "no submission" (the hook
    returns the pre-build answer). The proposal is a RELEASE; what protects it is rule 1's
    recheck before the apply step: the re-run policy sees the submission and BLOCKS, the hash
    moves, and the run fails STALE with nothing written. Were the recheck missing, operator_set's
    CAS against the pre-claim lock version would still lose (LockMoved)."""
    p = _proposal(env, OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p, {}))  # took the lock, not yet sent
    env.runs.set("k1", "execute_plan", p.proposal_id, "SUCCEEDED")  # run_status gate passes
    real = release_mod.get_submission
    fired = []

    def hook(store, rid):
        before = real(store, rid)
        if not fired:
            fired.append(rid)
            env.specs["execute_plan"].steps[1].build(_ctx(env, p, {"convert": ()}))  # record+claim
        return before

    monkeypatch.setattr(release_mod, "get_submission", hook)
    rel = env.release("ob-office-usd", "operator release while the run records", approvers=["a1"])
    assert fired
    assert rel.proposal.computed["action"] == "RELEASE"  # the policy was fooled
    assert rel.state == "FAILED"
    assert "changed since the proposal" in (rel.detail or "")  # rule 1 STALE
    assert lock(env, "ob-office-usd").state is LockState.LOCKED


# --- the owner-run gate: nothing is released while the owner run may still send -------------------


def test_a_live_owner_run_blocks_the_release(env):
    """While the lock owner's execute_plan run reads RUNNING, nothing is released."""
    p = _proposal(env, OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p, {}))  # took the lock
    env.runs.set("k1", "execute_plan", p.proposal_id, "RUNNING")
    env.advance(10 * 86400)  # no amount of time is a substitute for run_status
    rel = env.release("ob-office-usd", "run looks stuck to the operator", approvers=["a1"])
    assert rel.state == "BLOCKED"
    assert any("RUNNING" in r for r in rel.proposal.reasons)
    assert lock(env, "ob-office-usd").state is LockState.LOCKED


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"])
def test_a_terminal_owner_run_allows_the_release(env, terminal):
    p = _proposal(env, OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p, {}))  # took the lock, then died
    env.runs.set("k1", "execute_plan", p.proposal_id, terminal)
    rel = env.release("ob-office-usd", "run died after taking the lock", approvers=["a1"])
    assert rel.state == "SUCCEEDED", (rel.detail, rel.proposal.reasons)
    assert lock(env, "ob-office-usd").state is LockState.RELEASED
    assert env.execute(OFFICE).state == "SUCCEEDED"
    assert len(env.transfers()) == 1


def test_an_unknown_owner_run_blocks_the_release(env):
    """run_status None (no such execution) is never terminal."""
    p = _proposal(env, OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p, {}))
    rel = env.release("ob-office-usd", "run died after taking the lock", approvers=["a1"])
    assert rel.state == "BLOCKED"
    assert any("no execute_plan run" in r for r in rel.proposal.reasons)


def test_a_none_run_status_defers_at_propose_and_blocks_at_recheck_and_apply(env):
    from awx_common import TenantContext
    from awx_common.workflow import PolicyContext
    from awx_k1_treasury.release import OwnerRunLive, owner_run_gate

    p = _proposal(env, OFFICE)
    env.specs["execute_plan"].steps[0].build(_ctx(env, p, {}))
    owner = lock(env, "ob-office-usd").owner
    propose = PolicyContext(TenantContext("platform"), env.now(), env.ctx().read, store=env.store)
    assert owner_run_gate(propose, owner) is None  # propose time: decide at recheck
    recheck = replace(propose, proposal_id="p-release")
    assert "no run status" in owner_run_gate(recheck, owner)  # recheck: BLOCK
    spec = env.specs["release_obligation"]
    raw = {"obligation_id": "ob-office-usd", "reason": "run died after taking the lock"}
    intent = spec.validate(raw)
    assert spec.policy(intent, recheck).risk_tier.value == "BLOCKED"
    env.runs.set("k1", "execute_plan", p.proposal_id, "SUCCEEDED")
    d = spec.policy(intent, propose)
    from awx_common.workflow import StepContext

    ctx = StepContext(intent, d, recheck, "a" * 64, {}, "p-release")
    with pytest.raises(OwnerRunLive):  # apply re-checks, and a None callable never passes
        spec.steps[0].build(ctx)
    assert lock(env, "ob-office-usd").state is LockState.LOCKED


def test_an_unreadable_owner_run_blocks():
    from awx_common import TenantContext
    from awx_common.workflow import PolicyContext
    from awx_k1_treasury.release import owner_run_gate

    def boom(pid, action):
        raise ValueError("throttled")

    ctx = PolicyContext(
        TenantContext("platform"), EPOCH_NOW, lambda r: {}, proposal_id="p", run_status=boom
    )
    assert "unreadable" in owner_run_gate(ctx, "p-owner")


# --- a RELEASED lock this run held is never retaken -----------------------------------------------


def test_released_lock_of_this_run_is_not_retaken_on_redelivery(env, monkeypatch):
    """A crash between acquire and recording the run's attempts, then a person's release:
    the redelivered first step must not retake the lock at attempt 2."""
    p = _proposal(env, OFFICE)

    def crash(*a, **k):
        raise RuntimeError("Lambda died after acquire")

    monkeypatch.setattr(wf, "record_run_attempts", crash)
    with pytest.raises(RuntimeError):
        env.specs["execute_plan"].steps[0].build(_ctx(env, p, {}))
    cur = lock(env, "ob-office-usd")
    assert cur.state is LockState.LOCKED
    operator_set(
        env.store,
        "ob-office-usd",
        expected_store_version=cur.version,
        state=LockState.RELEASED,
        note="operator",
    )
    monkeypatch.undo()
    with pytest.raises(ObligationLocked):
        env.specs["execute_plan"].steps[0].build(_ctx(env, p, {}))
    assert lock(env, "ob-office-usd").attempt == 1
