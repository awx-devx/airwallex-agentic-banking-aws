"""Rule 0: every build is idempotent. Each spec runs through the real runner with every step's
build called twice for the same proposal, the first result thrown away (as a crashed and
redelivered build task would). One send per operation, a clean postcondition, and no stranded
lock, claim or book record."""

from __future__ import annotations

from dataclasses import replace

from awx_common.approvals import Origin
from awx_common.workflow import KitWorkflowSpec
from awx_k3_incident import SPECS, ledger
from awx_k3_incident.workflow import STAGING_SPECS
from k3_fixtures import BANK_RETURN, Kit3


def built_twice(spec: KitWorkflowSpec) -> KitWorkflowSpec:
    def twice(build):
        def run(ctx):
            build(ctx)  # thrown away
            return build(ctx)

        return run

    return replace(spec, steps=tuple(replace(s, build=twice(s.build)) for s in spec.steps))


def creates(kit: Kit3) -> int:
    return len(kit.sends())


def test_send_built_twice():
    kit = Kit3.new()
    raw = {
        "payment_key": "inv-r0",
        "beneficiary_id": kit.beneficiary_id,
        "amount": "10.00",
        "currency": "USD",
        "transfer_method": "LOCAL",
        "stage_for_incident": True,
    }
    out = kit.run(
        "send_supplier_transfer",
        raw,
        spec=built_twice(STAGING_SPECS["send_supplier_transfer"]),
        origin=Origin.OPERATOR,
    )
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition.ok
    assert creates(kit) == 1
    assert [i.key for i in kit.store.query("")] == ["book", "sent:inv-r0"]


def test_simulate_built_twice():
    kit = Kit3.new()
    oid = kit.staged_original()
    out = kit.run(
        "simulate_transfer_failure",
        {"transfer_id": oid, "failure_type": BANK_RETURN},
        spec=built_twice(SPECS["simulate_transfer_failure"]),
    )
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition.ok
    transitions = [r for r in kit.fake.requests if r[1].endswith("/transition")]
    assert len(transitions) == 2  # SENT once, FAILED once


def test_replace_built_twice():
    kit = Kit3.new()
    oid = kit.failed_original()
    sends = creates(kit)
    out = kit.run(
        "replace_failed_transfer",
        {"original_transfer_id": oid, "incident_ref": "INC-1"},
        spec=built_twice(SPECS["replace_failed_transfer"]),
    )
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert out.postcondition.ok
    assert creates(kit) == sends + 1
    held = ledger.get_lock(kit.store, oid)
    assert (held.state, held.owner, held.joined_by) == (
        ledger.LANDED,
        out.proposal.proposal_id,
        (),
    )


def test_replace_refused_then_built_twice_leaves_no_stranded_lock():
    from awx_spine.fake import Fault

    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    first = kit.run(
        "replace_failed_transfer",
        {"original_transfer_id": oid, "incident_ref": "INC-1"},
        spec=built_twice(SPECS["replace_failed_transfer"]),
    )
    assert first.state == "FAILED"
    assert ledger.get_lock(kit.store, oid).state == ledger.RELEASED


def test_close_built_twice():
    kit = Kit3.new()
    oid = kit.failed_original()
    assert kit.replace(oid).state == "SUCCEEDED"
    sends = creates(kit)
    out = kit.run(
        "close_incident",
        {"original_transfer_id": oid},
        spec=built_twice(SPECS["close_incident"]),
    )
    assert out.state == "SUCCEEDED", out.postcondition
    assert creates(kit) == sends
    assert len(kit.store.query("closure:")) == 1


class CrashOnEveryReplacementCreate:
    """An adapter that dies on every replacement create (after the call may have left), so
    run A ends with its attempt in flight (rule 9) and the lock IN_FLIGHT."""

    def __init__(self, fake) -> None:
        self.fake = fake

    def execute(self, op):
        if op.name == "transfers.create" and "replaces_transfer_id" in op.body["metadata"]:
            raise RuntimeError("adapter Lambda died")
        return self.fake.execute(op)

    def read(self, req):
        return self.fake.read(req)

    def __getattr__(self, name):
        return getattr(self.fake, name)


def test_joiner_built_twice_after_the_owner_crashed_before_sending():
    """A's adapter dies on
    every replacement create, so A ends with the lock IN_FLIGHT and nothing sent. B joins and
    runs its build twice: one create in total, B recorded as the one joiner, lock LANDED."""
    kit = Kit3.new()
    oid = kit.failed_original()
    sends = creates(kit)
    real = kit.fake
    kit.fake = CrashOnEveryReplacementCreate(real)  # type: ignore[assignment]
    try:
        first = kit.replace(oid, "INC-A")
    finally:
        kit.fake = real
    assert first.state == "FAILED"
    assert ledger.get_lock(kit.store, oid).state == ledger.IN_FLIGHT
    assert creates(kit) == sends  # the crash came before the call reached the fake
    kit.advance(200)  # past the 180 s attempt lease

    calls: list[str] = []
    spec = SPECS["replace_failed_transfer"]

    def counted(build):
        def run(ctx):
            calls.append(ctx.proposal_id)
            return build(ctx)

        return run

    counting = replace(spec, steps=tuple(replace(s, build=counted(s.build)) for s in spec.steps))
    second = kit.run(
        "replace_failed_transfer",
        {"original_transfer_id": oid, "incident_ref": "INC-B"},
        spec=built_twice(counting),
    )
    b = second.proposal.proposal_id
    assert calls == [b, b]  # B's build ran twice
    held = ledger.get_lock(kit.store, oid)
    assert held.joined_by == (b,)
    assert held.owner == first.proposal.proposal_id
    assert creates(kit) == sends + 1
    assert len(kit.replacements_of(oid)) == 1
    assert held.state == ledger.LANDED, (held, second.postcondition)
