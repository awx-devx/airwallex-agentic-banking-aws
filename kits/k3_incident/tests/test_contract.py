"""Contract: exports, tool/spec/read coverage, validation and the closure record."""

from __future__ import annotations

from decimal import Decimal

import pytest
from awx_common import SERVER_OWNED_FIELDS, RiskTier, ToolKind, transport
from awx_k3_incident import READS, SPECS, TOOLS, ledger
from awx_k3_incident.closure import IncidentNotClosable, closure_record
from awx_k3_incident.intent import IntentInvalid, validate_replace, validate_send
from k3_fixtures import Kit3


def test_every_propose_tool_has_a_spec_and_every_read_tool_a_read():
    propose = {t.name for t in TOOLS if t.kind is ToolKind.PROPOSE}
    read = {t.name for t in TOOLS if t.kind is ToolKind.READ}
    assert propose == {s.tool_name for s in SPECS.values()}
    assert read == set(READS)
    assert all(action == spec.action and spec.kit == "k3" for action, spec in SPECS.items())


def test_replace_tool_cannot_carry_an_amount_account_or_request_id():
    (tool,) = [t for t in TOOLS if t.name == "propose_replace_failed_transfer"]
    props = set(tool.input_schema["properties"])
    assert props == {"original_transfer_id", "incident_ref"}
    assert not props & SERVER_OWNED_FIELDS
    with pytest.raises(IntentInvalid, match="unexpected"):
        validate_replace({"original_transfer_id": "t1", "incident_ref": "I", "amount": "1.00"})


@pytest.mark.parametrize(
    "bad",
    [
        {"amount": 100.0},
        {"amount": "100.001"},
        {"amount": "-1"},
        {"amount": "0"},
        {"currency": "eur"},
        {"transfer_method": "WIRE"},
        {"beneficiary_id": "../x"},
        {"request_id": "abc"},
    ],
)
def test_send_validation_refuses(bad):
    raw = {
        "payment_key": "inv-1",
        "beneficiary_id": "ben_1",
        "amount": "100.00",
        "currency": "EUR",
        "transfer_method": "LOCAL",
        **bad,
    }
    with pytest.raises(IntentInvalid):
        validate_send(raw)


def test_policy_computed_amounts_survive_transport_exactly():
    kit = Kit3.new()
    oid = kit.failed_original()
    spec = SPECS["replace_failed_transfer"]
    d = spec.policy({"original_transfer_id": oid, "incident_ref": "I"}, kit.context())
    back = transport.loads(transport.dumps(dict(d.computed)))
    assert back == dict(d.computed)
    assert back["amount"].amount == Decimal("10.00")
    assert back["attempt"] == 1


def test_read_transfer_projection():
    kit = Kit3.new()
    oid = kit.failed_original()
    body = kit.read("read_transfer", {"transfer_id": oid})
    assert body["status"] == "CANCELLED"
    assert body["failure_code"] == "90802"
    assert body["failure"]["details"]["type"] == "INCORRECT_ROUTING"


ORIGINAL = {
    "id": "o",
    "status": "CANCELLED",
    "failure": {
        "code": "90802",
        "details": {"type": "INCORRECT_ROUTING"},
        "message": "Beneficiary bank returned",
    },
}


def test_closure_records_both_dispositions_with_the_failure_object():
    c = closure_record(
        ORIGINAL, {"id": "r", "status": "PAID", "request_id": "rid"}, replacement_request_id="rid"
    )
    assert c.resolved
    assert c.original_failure_code == "90802"
    assert c.original_failure == {"code": "90802", "message": "Beneficiary bank returned"}


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ({**ORIGINAL, "status": "SENT"}, {"id": "r", "status": "PAID"}),
        ({"id": "o", "status": "CANCELLED"}, {"id": "r", "status": "PAID"}),
        (
            {"id": "o", "status": "CANCELLED", "failure_type": "BENEFICIARY_BANK_RETURNED"},
            {"id": "r", "status": "PAID"},
        ),
        (ORIGINAL, {"id": "r", "status": "SENT"}),
        (ORIGINAL, {"id": "o", "status": "PAID"}),
        (ORIGINAL, {"id": "r", "status": "PAID", "request_id": "other"}),
    ],
)
def test_closure_refuses_incomplete_dispositions(original, replacement):
    with pytest.raises(IncidentNotClosable):
        closure_record(original, replacement, replacement_request_id="rid")


def test_agent_floor_per_action():
    """Every model-started action needs a person except close_incident (README)."""
    floors = {action: spec.agent_min_tier for action, spec in SPECS.items()}
    assert floors == {
        "replace_failed_transfer": RiskTier.SINGLE,
        "simulate_transfer_failure": RiskTier.SINGLE,
        "close_incident": RiskTier.AUTO,
    }


def test_every_write_step_has_a_reconcile():
    """Rule 9 needs a reconcile hook for every step that can emit an operation."""
    for spec in SPECS.values():
        for step in spec.steps:
            if spec.action != "close_incident":
                assert step.reconcile is not None, (spec.action, step.name)


def test_proposal_id_covers_computed():
    """A second payment with a different amount in the same window is a new run."""
    kit = Kit3.new()
    a = kit.send("inv-a", amount="10.00")
    b = kit.send("inv-a2", amount="11.00")
    assert a.proposal.proposal_id != b.proposal.proposal_id


def test_lock_take_is_a_conditional_create():
    """Of two concurrent takes of one lock, exactly one wins."""
    kit = Kit3.new()
    take = dict(original_transfer_id="t", attempt=1, request_id="r", now=kit.now(), over=None)
    ledger.take_lock(kit.store, owner="p-1", **take)
    with pytest.raises(ledger.LedgerConflict):
        ledger.take_lock(kit.store, owner="p-2", **take)
    assert ledger.get_lock(kit.store, "t").owner == "p-1"


def test_identical_proposals_in_different_windows_share_a_hash_not_an_owner():
    """M4: the lock records the proposal_id; identical proposals in two approval windows
    share intent_hash, so only the proposal_id can tell the owner apart."""
    from datetime import timedelta

    from awx_spine.fake import Fault

    kit = Kit3.new()
    oid = kit.failed_original()
    kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=10 * 3600)
    first = kit.replace(oid, "INC-1")
    kit.advance(timedelta(hours=5).total_seconds())  # next 4-hour approval window
    second = kit.replace(oid, "INC-1")  # identical intent and computed
    assert first.proposal.intent_hash == second.proposal.intent_hash
    assert first.proposal.proposal_id != second.proposal.proposal_id
    assert ledger.get_lock(kit.store, oid).owner == first.proposal.proposal_id
