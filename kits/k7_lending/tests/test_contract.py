"""Every propose tool has a SPECS entry and every read tool a READS entry."""

from __future__ import annotations

from datetime import timedelta

from awx_common import SERVER_OWNED_FIELDS, RiskTier, ToolKind
from awx_k7_lending import READS, SPECS, TOOLS


def test_every_tool_has_its_spec_or_read():
    propose = {t.name for t in TOOLS if t.kind is ToolKind.PROPOSE}
    reads = {t.name for t in TOOLS if t.kind is ToolKind.READ}
    assert propose == {s.tool_name for s in SPECS.values()}
    assert reads == set(READS)
    assert all(k == s.action and s.kit == "k7" for k, s in SPECS.items())


def test_tool_inputs_carry_no_amount_or_server_owned_field():
    for tool in TOOLS:
        props = set(tool.input_schema["properties"])
        assert props <= {"loan_id", "week"}
        assert not props & SERVER_OWNED_FIELDS


def test_spec_shape_and_approval_floors():
    for spec in SPECS.values():
        assert spec.approval_ttl <= timedelta(hours=24)
        assert not spec.allows_read("/api/v1/transfers")
    money_moving = ("collect_weekly_repayment", "disburse_advance")
    for action in money_moving:
        spec = SPECS[action]
        assert spec.agent_min_tier is RiskTier.SINGLE  # agent floor: a model start is approved
        assert spec.allows_platform_read("/api/v1/charges/ch_1")
        assert spec.allows_platform_read("/api/v1/connected_account_transfers/tr_1")
        assert not spec.allows_read("/api/v1/charges")  # never read with the header
        assert spec.steps[0].reconcile is not None
    assert SPECS["reconcile_portfolio"].agent_min_tier is RiskTier.AUTO  # README justifies


def test_reads_never_expose_account_ids(k7, world):
    world.fake.move_to(k7.AFTER_W42)
    ctx = world.ctx(SPECS["collect_weekly_repayment"], world.borrowers["loan-a"])
    platform = world.ctx(SPECS["reconcile_portfolio"], k7.PLATFORM)
    outputs = [
        READS["read_borrower_position"]({"loan_id": "loan-a"}, ctx),
        READS["read_weekly_collection_preview"]({"loan_id": "loan-a", "week": k7.W42}, ctx),
        READS["read_portfolio_position"]({}, platform),
    ]
    assert "acct_" not in repr(outputs)
    assert outputs[2]["reconciled"] is True
