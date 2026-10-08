"""A run claim is released on every failure path its owner reaches, and money left in flight
still pays once. All through run_kit_workflow on FakeAirwallex."""

import itertools

from awx_k6_payroll import PAYROLL_SPEC
from awx_k6_payroll import workflow as wf
from awx_k6_payroll.workflow import keys_for
from awx_spine.fake import Fault
from k6_data import PERIOD, employer, us
from k6_fake import children, make_env, run

DAY = 86400


def carl():
    return {"pay_period": PERIOD, "employers": [employer("c", us("carl", "800.00"))]}


def claim(env, intent):
    return wf.read_claim(env.platform_store, keys_for(env.platform_store, intent))


def paid(env, who):
    return [
        t
        for t in env.fake.objects("transfers")
        if t["status"] == "PAID" and str(t["reference"]).endswith(f"-{who}")
    ]


def test_snapshot_unstable_releases_the_claim_and_the_next_run_pays(monkeypatch):
    env = make_env(c="5000")
    counter = itertools.count()
    monkeypatch.setattr(wf, "_txn_ids", lambda read, since: (frozenset({str(next(counter))}), []))
    first = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert first.state == "FAILED"
    assert "SnapshotUnstable" in first.detail
    assert claim(env, first.proposal.intent) is None
    assert env.money_ops() == []
    monkeypatch.undo()
    env.fake.advance(DAY)
    second = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert second.state == "SUCCEEDED", second.postcondition
    assert len(paid(env, "carl")) == 1


def test_a_postcondition_that_raises_releases_the_claim(monkeypatch):
    env = make_env(c="5000")

    def boom(*a, **k):
        raise wf.MissingPrerequisite("read-back blew up")

    monkeypatch.setattr(wf, "record_definitive_failures", boom)
    first = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert first.state == "FAILED"
    assert "postcondition" in first.detail
    assert claim(env, first.proposal.intent) is None
    monkeypatch.undo()
    second = children(run(PAYROLL_SPEC, carl(), env, proposed_by="scheduler-2"))["c"]
    assert second.state == "SUCCEEDED", second.postcondition
    assert len(paid(env, "carl")) == 1


def test_released_claim_with_money_in_flight_still_pays_once(monkeypatch):
    """The payout's outcome is unknown and the postcondition raises: the claim goes, a second
    proposal for the same employer and period runs, and carl is paid exactly once."""
    env = make_env(c="5000")
    env.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=600)

    def boom(*a, **k):
        raise wf.MissingPrerequisite("read-back blew up")

    monkeypatch.setattr(wf, "record_definitive_failures", boom)
    first = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert first.state == "FAILED"
    assert claim(env, first.proposal.intent) is None
    monkeypatch.undo()
    env.fake.advance(700)
    second = children(run(PAYROLL_SPEC, carl(), env, proposed_by="scheduler-2"))["c"]
    assert second.state == "SUCCEEDED", second.postcondition
    assert len({op.request_id for op in env.money_ops("transfers.create")}) == 1
    assert len(env.fake.objects("transfers")) == 1
    assert len(paid(env, "carl")) == 1
