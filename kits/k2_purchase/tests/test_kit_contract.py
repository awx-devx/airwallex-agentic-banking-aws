from awx_common import SERVER_OWNED_FIELDS, ToolKind
from awx_common.workflow import KitWorkflowSpec
from awx_k2_purchase import APPROVAL_MATRIX, READS, SPECS, TOOLS


def test_every_propose_tool_has_a_spec_and_every_read_tool_a_read():
    propose = {t.name for t in TOOLS if t.kind is ToolKind.PROPOSE}
    read = {t.name for t in TOOLS if t.kind is ToolKind.READ}
    assert propose == {s.tool_name for s in SPECS.values()}
    assert read == set(READS)
    assert len({t.name for t in TOOLS}) == len(TOOLS)


def test_specs_are_keyed_by_action_and_belong_to_k2():
    for action, spec in SPECS.items():
        assert isinstance(spec, KitWorkflowSpec)
        assert spec.action == action
        assert spec.kit == "k2"
        assert spec.postcondition is not None
    assert set(APPROVAL_MATRIX) == set(SPECS)


def test_no_tool_exposes_a_server_owned_or_computed_field():
    computed = {"risk_tier", "controls", "limits", "plan", "transaction_limits"}

    def names(schema):
        out = set()
        for key, sub in (schema.get("properties") or {}).items():
            out.add(key)
            out |= names(sub)
            if isinstance(sub.get("items"), dict):
                out |= names(sub["items"])
        return out

    for tool in TOOLS:
        got = names(tool.input_schema)
        assert not got & SERVER_OWNED_FIELDS, tool.name
        assert not got & computed, tool.name


def test_allowlists_cover_issuing_reads_only():
    spec = SPECS["issue_card"]
    assert spec.allows_read("/api/v1/issuing/cards/card_1/limits")
    assert spec.allows_read("/api/v1/balances/current")
    assert not spec.allows_read("/api/v1/transfers")
    assert not spec.allows_read("/api/v1/issuing/cards/../../transfers")


def test_every_create_step_that_ignores_or_rejects_request_ids_has_a_reconcile_hook():
    """Cardholder create and simulation/issuing/create ignore
    request_id, and card create rejects a reused one without an id, so each needs its own
    StepSpec.reconcile."""
    hooked = {
        (action, step.name)
        for action, spec in SPECS.items()
        for step in spec.steps
        if step.reconcile is not None
    }
    assert {("issue_card", "cardholder"), ("issue_card", "card")} <= hooked
    for step in SPECS["run_limit_test"].steps:
        assert step.reconcile is not None, step.name


def test_only_revocations_run_at_auto_when_the_model_starts_it():
    """AUTO for a model-started proposal needs a documented justification (DESIGN.md). Only
    freeze and control updates have one, and the update policy is AUTO only for a pure reduction."""
    from awx_common import RiskTier

    auto = {a for a, s in SPECS.items() if s.agent_min_tier is RiskTier.AUTO}
    assert auto == {"freeze_card", "update_card_controls"}
    assert SPECS["unfreeze_card"].agent_min_tier is RiskTier.SINGLE
