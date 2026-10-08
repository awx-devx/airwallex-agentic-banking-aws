import pytest
from awx_common import ToolKind
from awx_common.errors import ValidationFailed
from awx_common.workflow import PolicyContext
from awx_k8_settlement import READS, SPECS, TOOLS


def test_every_propose_tool_has_a_spec_and_every_read_tool_a_read():
    proposes = {t.name for t in TOOLS if t.kind is ToolKind.PROPOSE}
    reads = {t.name for t in TOOLS if t.kind is ToolKind.READ}
    assert proposes == {s.tool_name for s in SPECS.values()}
    assert reads == set(READS)
    assert all(s.kit == "k8" for s in SPECS.values())
    assert all(key == s.action for key, s in SPECS.items())


def test_tool_inputs_never_name_a_seller_account_or_amount():
    for tool in TOOLS:
        props = set(tool.input_schema["properties"])
        assert props <= {
            "cycle_id",
            "carrier_event_id",
            "currency",
            "kind",
            "ref",
            "resolution",
            "evidence_ref",
        }, tool.name
        # no tool takes an amount, an account or a request_id
        assert not props & {"amount", "account", "request_id", "destination", "source"}


def test_tool_schemas_match_validators():
    for tool in TOOLS:
        if tool.kind is ToolKind.PROPOSE:
            required = tool.input_schema["required"]
            spec = SPECS[tool.name.removeprefix("propose_")]
            good = {k: "x1" for k in required}
            if spec.action == "resolve_in_flight_money":
                good = {
                    "kind": "charge",
                    "ref": "1",
                    "resolution": "landed",
                    "evidence_ref": "case-123",
                }
            spec.validate(good)
            with pytest.raises(ValidationFailed):
                spec.validate({**good, "amount": "1"})


def test_only_the_read_only_reconcile_lowers_the_agent_floor():
    from awx_common import RiskTier

    floors = {a: s.agent_min_tier for a, s in SPECS.items()}
    assert floors == {
        "settle_seller_cycle": RiskTier.SINGLE,
        "recalculate_seller_reserve": RiskTier.SINGLE,
        "recover_shortfall": RiskTier.SINGLE,
        "reconcile_seller_money": RiskTier.AUTO,
        "resolve_in_flight_money": RiskTier.SINGLE,
    }


def test_reconcile_never_emits_an_operation():
    spec = SPECS["reconcile_seller_money"]
    assert len(spec.steps) == 1
    assert spec.steps[0].build(None) == ()
    assert spec.steps[0].finality is None


def test_money_moving_specs_declare_platform_reads_for_finality_and_reconcile():
    for action in ("settle_seller_cycle", "recover_shortfall"):
        spec = SPECS[action]
        assert spec.steps[0].reconcile is not None
        assert spec.allows_platform_read("/api/v1/connected_account_transfers/tr_1")
        assert spec.allows_platform_read("/api/v1/charges/ch_1")
        assert spec.allows_platform_read("/api/v1/connected_account_transfers")
        assert spec.allows_platform_read("/api/v1/charges")
        assert spec.allows_read("/api/v1/balances/current")
        assert not spec.allows_platform_read("/api/v1/balances/current")


def test_specs_and_store_reads_fail_closed_without_a_store(h):
    ctx = PolicyContext(h.TenantContext("bob", "acct_bob"), h.NOW, lambda r: {})
    for action, raw in (("settle_seller_cycle", {"cycle_id": "c1"}), ("recover_shortfall", {})):
        assert SPECS[action].policy(raw, ctx).risk_tier.value == "BLOCKED"
    with pytest.raises(Exception, match="store"):
        READS["read_seller_ledger"]({}, ctx)
