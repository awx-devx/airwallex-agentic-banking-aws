"""The kit's tables: every propose tool has a SPECS entry and every read tool a READS entry."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from awx_common import KIT_DIRS, SERVER_OWNED_FIELDS, ToolKind
from awx_common.tools import _property_names
from awx_common.workflow import KitWorkflowSpec
from awx_k4_dispute import READS, SPECS, TOOLS
from awx_k4_dispute.provider import (
    READ_ALLOWLIST,
    dispute_read,
    disputes_list_read,
    payment_intent_read,
    refunds_read,
)
from awx_k4_dispute.simulation import (
    DEMO_CASES,
    create_payment_intent,
    is_simulation,
    stage_dispute,
)
from k4_harness import NOW, World, challenge_input, make_ctx


def test_every_propose_tool_has_a_spec_and_every_read_tool_a_read():
    propose = {t.name for t in TOOLS if t.kind is ToolKind.PROPOSE}
    reads = {t.name for t in TOOLS if t.kind is ToolKind.READ}
    assert propose == {s.tool_name for s in SPECS.values()}
    assert reads == set(READS)
    assert len({t.name for t in TOOLS}) == len(TOOLS)
    for action, spec in SPECS.items():
        assert isinstance(spec, KitWorkflowSpec)
        assert spec.action == action
        assert spec.kit == "k4"
    assert KIT_DIRS["k4"] == "kits/k4_dispute"


def test_no_tool_exposes_a_server_owned_field():
    for t in TOOLS:
        assert not _property_names(t.input_schema) & SERVER_OWNED_FIELDS


def test_reads_used_by_the_kit_are_allowlisted():
    for req in (
        dispute_read("dsp_1"),
        disputes_list_read("REQUIRES_RESPONSE"),
        refunds_read("int_1"),
        payment_intent_read("int_1"),
    ):
        assert any(re.match(p, req.path) for p in READ_ALLOWLIST)
    for spec in SPECS.values():
        assert not spec.allows_read("/api/v1/transfers")
        assert not spec.allows_read("/api/v1/pa/payment_disputes/dsp_1/../../balances")
        assert spec.approval_ttl > timedelta(0)


def test_simulation_ops_are_not_agent_tools_and_carry_poc_labels():
    names = {t.name for t in TOOLS}
    for case in DEMO_CASES:
        pi = create_payment_intent(case, "bk_00ff00ff00ff00ff")
        assert pi.body["merchant_order_id"].startswith("poc-k4-")
        # A staged dispute is labelled through its poc-k4- payment intent.
        staged = stage_dispute(case, "bk_00ff00ff00ff00ff", "int_x", NOW)
        for op in (pi, staged):
            assert is_simulation(op)
            assert op.business_key.startswith("poc-k4-")
            assert op.request_id == op.body["request_id"]
    assert not any("simul" in n for n in names)


@pytest.mark.parametrize("name", sorted(READS))
def test_reads_are_pure_and_use_ctx_read_only(name):
    port = World()
    port.add_dispute("dsp_1", amount="900.00")
    ctx = make_ctx(port, SPECS["challenge_dispute"], NOW)
    raw = {
        "read_dispute_queue": {},
        "read_dispute": {"dispute_id": "dsp_1"},
        "read_refunds": {"dispute_id": "dsp_1"},
        "read_dispute_economics": {
            k: v
            for k, v in challenge_input("dsp_1").items()
            if k in ("dispute_id", "evidence_ids", "facts")
        },
    }[name]
    out = READS[name](raw, ctx)
    assert port.writes() == []
    import json

    json.dumps(out)  # plain JSON for the model, no Money or datetime objects
    assert READS[name](raw, ctx) == out


def test_read_economics_reports_code_computed_numbers():
    port = World()
    port.add_dispute("dsp_1", amount="900.00")
    ctx = make_ctx(port, SPECS["challenge_dispute"], NOW)
    raw = {
        k: v
        for k, v in challenge_input("dsp_1").items()
        if k in ("dispute_id", "evidence_ids", "facts")
    }
    out = READS["read_dispute_economics"](raw, ctx)
    assert out["recommendation"] == "CONTEST"
    assert out["band"] == "STRONG"
    assert out["cost_contest_expected"] == {"amount": "270.00", "currency": "AUD"}
    assert out["prior_undisputed_orders"] == 3
    queue = READS["read_dispute_queue"]({}, ctx)
    assert queue["count"] == 1
    assert queue["disputes"][0]["contest_cannot_pay"] is False
