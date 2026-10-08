"""local_runner end to end with a toy spec against awx_spine's real MutationExecutor and store:
every approval tier, recovery, fan_out, and each runner rule (0-11) in the awx_common.workflow
module docstring."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest
from awx_common import Money, Origin, RiskTier, derive_proposal_id
from awx_common.approvals import ApprovalState, Decision
from awx_common.errors import TenantScopeViolation
from awx_common.intent import derive_request_id
from awx_common.ops import Operation, OperationStatus, ReadRequest
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext, bind_platform_operation
from awx_common.workflow import KitWorkflowSpec, PolicyDecision, ReconcileUndetermined, StepSpec
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import engine
from awx_spine_aws.local_runner import _KnownTenants, run_kit_workflow

import toy

PLATFORM = TenantContext("platform")
TENANT_A = TenantContext("tenant-a", "acct_A")


def run(
    spec,
    *,
    port=None,
    decide=toy.no_decision,
    tenant=PLATFORM,
    raw=None,
    store=None,
    kit_store=None,
    tenants=None,
    origin=Origin.SCHEDULE,
    sandbox=True,
    clock=None,
    platform_store=None,
):
    port = port or toy.ToyPort()
    clock = clock or toy.Clock()
    audits: list = []
    store = store if store is not None else InMemoryOperationStore()
    outcome = run_kit_workflow(
        spec,
        raw or toy.RAW,
        proposed_by="requester-sub",
        tenant=tenant,
        port=port,
        store=store,
        decide=decide,
        now=clock.now,
        sleep=clock.sleep,
        audit=audits.append,
        kit_store=kit_store,
        platform_store=platform_store,
        tenants=tenants,
        origin=origin,
        sandbox=sandbox,
    )
    return outcome, port, clock, audits


def names(calls):
    return [c[0] for c in calls]


def replace_spec(spec: KitWorkflowSpec, **changes) -> KitWorkflowSpec:
    return type(spec)(**{**spec.__dict__, **changes})


def pay_sends(port):
    return [op for op in port.sends if op.body.get("reference") == "poc-s2-pay"]


# --- tiers and approval ---------------------------------------------------------------------


def test_auto_runs_every_step_then_finality_then_postcondition():
    calls: list = []
    outcome, port, clock, audits = run(
        toy.toy_spec(RiskTier.AUTO, calls=calls), port=toy.ToyPort(reads_until_final=3)
    )
    assert outcome.state == "SUCCEEDED"
    assert [op.body["reference"] for op in port.sends] == ["poc-s2-fee", "poc-s2-pay"]
    assert outcome.results["pay"][0].provider_status == "PAID"
    assert outcome.postcondition.ok
    assert clock.sleeps == [5, 5, 5]
    assert names(calls) == ["policy", "policy", "build:fee", "build:pay", "postcondition"]
    assert audits[-1]["state"] == "SUCCEEDED"
    assert audits[-1]["approval"] == "NOT_REQUIRED"
    assert audits[-1]["origin"] == "SCHEDULE"


def test_amounts_are_chosen_by_code_and_bound_into_the_proposal_hash():
    outcome, port, _, _ = run(toy.toy_spec(RiskTier.AUTO))
    assert outcome.proposal.computed["total"] == Money.of("101.50", "AUD")
    assert port.sends[1].body["transfer_amount"] == toy.DEC("101.50")
    assert all(op.intent_hash == outcome.proposal.intent_hash for op in port.sends)


def test_single_approved():
    outcome, port, _, _ = run(toy.toy_spec(RiskTier.SINGLE), decide=toy.approve_by("approver"))
    assert outcome.state == "SUCCEEDED"
    assert len(port.sends) == 2


@pytest.mark.parametrize(
    ("tier", "decide", "state"),
    [
        (RiskTier.SINGLE, toy.approve_by("approver", decision=Decision.REJECT), "REJECTED"),
        (RiskTier.SINGLE, toy.no_decision, "EXPIRED"),
        (RiskTier.DUAL, toy.approve_by("a1"), "EXPIRED"),
        (RiskTier.DUAL, toy.approve_by("a1", "a1"), "EXPIRED"),
    ],
)
def test_unapproved_sends_nothing_and_abandons(tier, decide, state):
    """Rule 8: on_abandon runs with the reason when no step executed."""
    calls: list = []
    outcome, port, _, audits = run(toy.toy_spec(tier, calls=calls), decide=decide)
    assert outcome.state == state
    assert port.sends == []
    assert names(calls)[-1] == f"abandon:{state}"
    assert "postcondition" not in names(calls)
    assert audits[-1]["abandon_reason"] == state


def test_dual_with_two_distinct_approvers_runs():
    outcome, port, _, _ = run(toy.toy_spec(RiskTier.DUAL), decide=toy.approve_by("a1", "a2"))
    assert outcome.state == "SUCCEEDED"
    assert len(port.sends) == 2


@pytest.mark.parametrize(
    ("decide", "why"),
    [
        (toy.approve_by("approver", at=toy.T0 + timedelta(hours=2)), "validity window"),
        (toy.approve_by("requester-sub"), "proposer"),
    ],
)
def test_late_or_self_approval_fails_closed(decide, why):
    outcome, port, _, _ = run(toy.toy_spec(RiskTier.SINGLE), decide=decide)
    assert outcome.state == "REJECTED"
    assert why in outcome.detail
    assert port.sends == []


def test_decision_for_a_different_hash_fails_closed():
    def tampered(p):
        [d] = toy.approve_by("approver")(p)
        return [type(d)(d.proposal_id, "0" * 64, d.approver, d.decision, d.decided_at)]

    outcome, port, _, _ = run(toy.toy_spec(RiskTier.SINGLE), decide=tampered)
    assert outcome.state == "REJECTED"
    assert port.sends == []


def test_blocked_never_sends_never_asks_and_abandons():
    calls: list = []
    asked: list = []
    port = toy.ToyPort(balance=Money.of("10.00", "AUD"))
    outcome, port, _, audits = run(
        toy.toy_spec(RiskTier.SINGLE, calls=calls),
        port=port,
        decide=lambda p: asked.append(p) or [],
    )
    assert outcome.state == "BLOCKED"
    assert port.sends == []
    assert asked == []
    assert names(calls) == ["policy", "abandon:BLOCKED"]
    assert audits[-1]["risk_tier"] == "BLOCKED"


# --- rule 11: origin, the agent floor, derived proposal IDs ---------------------------------


def test_rule11_an_agent_started_auto_proposal_needs_one_approval():
    asked: list = []

    def decide(p):
        asked.append(p.risk_tier)
        return toy.approve_by("approver")(p)

    outcome, port, _, audits = run(toy.toy_spec(RiskTier.AUTO), origin=Origin.AGENT, decide=decide)
    assert asked == [RiskTier.SINGLE]
    assert outcome.state == "SUCCEEDED"
    assert audits[-1]["origin"] == "AGENT"
    nobody, port, _, _ = run(toy.toy_spec(RiskTier.AUTO), origin=Origin.AGENT)
    assert nobody.state == "EXPIRED"
    assert port.sends == []


def test_rule11_a_spec_may_lower_the_floor_to_auto():
    spec = toy.toy_spec(RiskTier.AUTO, agent_min_tier=RiskTier.AUTO)
    outcome, _, _, _ = run(spec, origin=Origin.AGENT)
    assert outcome.state == "SUCCEEDED"
    assert outcome.proposal.risk_tier is RiskTier.AUTO


def test_rule11_the_proposal_id_is_derived_so_a_retry_reaches_the_same_run():
    a, _, _, _ = run(toy.toy_spec(), clock=toy.Clock())
    b, _, _, _ = run(toy.toy_spec(), clock=toy.Clock())
    assert a.proposal.proposal_id == b.proposal.proposal_id
    p = a.proposal
    assert p.proposal_id == derive_proposal_id(
        kit=p.kit,
        action=p.action,
        intent=p.intent,
        tenant_id="platform",
        proposed_by=p.proposed_by,
        now=p.created_at,
        ttl=p.expires_at - p.created_at,
        computed=p.computed,
    )


def test_rule11_start_refuses_a_proposal_id_that_is_not_derived():
    env = _env()
    spec = toy.toy_spec()
    proposal, decision = engine.propose(
        spec,
        (),
        toy.RAW,
        proposed_by="u",
        tenant=PLATFORM,
        env=env,
        origin=Origin.SCHEDULE,
        proposal_id="chosen-by-someone",
    )
    execution = engine.execution_input(proposal, decision, toy.RAW, "platform")
    with pytest.raises(engine.ApprovalBindingError, match="derived"):
        engine.start(spec, execution, tenant=PLATFORM)


def test_rule11_start_refuses_an_origin_that_disagrees_with_the_proposal():
    env = _env()
    spec = toy.toy_spec()
    proposal, decision = engine.propose(
        spec, (), toy.RAW, proposed_by="u", tenant=PLATFORM, env=env, origin=Origin.AGENT
    )
    execution = engine.execution_input(proposal, decision, toy.RAW, "platform")
    execution["origin"] = "SCHEDULE"
    with pytest.raises(engine.ApprovalBindingError, match="origin"):
        engine.start(spec, execution, tenant=PLATFORM)


def _env():
    from awx_spine_aws.local_runner import _LocalEnv

    return _LocalEnv(
        port=toy.ToyPort(),
        clock=toy.Clock().now,
        kit_store=InMemoryKitStore(),
        platform_store=InMemoryKitStore(),
        tenants=_KnownTenants({"platform": PLATFORM}),
        sandbox=True,
    )


# --- rule 1: re-run before the first step ---------------------------------------------------


def test_rule1_a_moved_hash_fails_closed_as_stale():
    calls: list = []
    port = toy.ToyPort()

    def decide_then_drain(p):  # the balance falls while the proposal waits for approval
        port.balance = Money.of("50.00", "AUD")
        return toy.approve_by("approver")(p)

    outcome, port, _, audits = run(
        toy.toy_spec(RiskTier.SINGLE, calls=calls), port=port, decide=decide_then_drain
    )
    assert outcome.state == "FAILED"
    assert "propose again" in outcome.detail
    assert port.sends == []
    assert names(calls)[-1] == "abandon:STALE"
    assert "postcondition" not in names(calls)
    assert audits[-1]["abandon_reason"] == "STALE"


def test_rule1_a_raise_in_the_rerun_fails_closed_as_stale():
    calls: list = []
    spec = toy.toy_spec(calls=calls)
    first = [True]

    def flaky(intent, ctx):
        if first.pop() if first else False:
            return spec.policy(intent, ctx)
        raise RuntimeError("read failed")

    outcome, port, _, audits = run(replace_spec(spec, policy=flaky))
    assert outcome.state == "FAILED"
    assert "RuntimeError: read failed" in audits[-1]["detail"]
    assert port.sends == []
    assert names(calls)[-1] == "abandon:STALE"


# --- rule 0: pure callables cannot write the store -----------------------------------------


def test_rule0_a_policy_that_writes_the_store_fails_closed():
    spec = toy.toy_spec()

    def writing(intent, ctx):
        ctx.store.put("x", {"n": 1}, expected_version=None)

    with pytest.raises(engine.PurityViolation):
        run(replace_spec(spec, policy=writing))


def test_rule0_policy_runs_twice_and_builds_once_per_step():
    calls: list = []
    run(toy.toy_spec(RiskTier.SINGLE, calls=calls), decide=toy.approve_by("approver"))
    assert names(calls).count("policy") == 2
    assert names(calls).count("build:fee") == names(calls).count("build:pay") == 1
    assert names(calls).count("postcondition") == 1


# --- rule 2: the postcondition always runs once steps stop ---------------------------------


@pytest.mark.parametrize(
    "port",
    [
        toy.ToyPort(fail_ops={"transfers.create"}),
        toy.ToyPort(final_status="CANCELLED"),
        toy.ToyPort(reads_until_final=100),
    ],
    ids=["fee-failed", "finality-cancelled", "finality-timeout"],
)
def test_rule2_postcondition_runs_after_failure_and_the_run_stays_failed(port):
    calls: list = []
    store = InMemoryKitStore()
    outcome, _, _, _ = run(
        toy.toy_spec(calls=calls, finality_timeout_s=20), port=port, kit_store=store
    )
    assert outcome.state == "FAILED"
    assert names(calls).count("postcondition") == 1
    assert outcome.postcondition is not None
    assert not outcome.postcondition.ok
    assert store.get("runs").value == {"count": 1}  # the postcondition wrote the store


def test_rule2_postcondition_runs_after_a_build_raises():
    calls: list = []

    def broken(ctx):
        raise ValueError("bad build")

    outcome, port, _, audits = run(toy.toy_spec(calls=calls, fee_build=broken))
    assert outcome.state == "FAILED"
    assert port.sends == []
    assert "postcondition" in names(calls)
    assert "ValueError: bad build" in audits[-1]["detail"]  # rule 7


def test_postcondition_failure_on_success_path():
    outcome, port, _, _ = run(toy.toy_spec(postcondition_ok=False))
    assert outcome.state == "POSTCONDITION_FAILED"
    assert len(port.sends) == 2


def test_rule7_a_raising_postcondition_fails_closed_and_is_audited():
    def boom(ctx):
        raise KeyError("ledger")

    outcome, _, _, audits = run(replace_spec(toy.toy_spec(), postcondition=boom))
    assert outcome.state == "FAILED"
    assert "postcondition KeyError" in audits[-1]["detail"]


def test_rule7_a_raising_on_abandon_is_audited():
    def boom(ctx, reason):
        raise RuntimeError("release failed")

    outcome, _, _, audits = run(replace_spec(toy.toy_spec(RiskTier.SINGLE), on_abandon=boom))
    assert outcome.state == "FAILED"
    assert "on_abandon RuntimeError: release failed" in audits[-1]["detail"]


def test_a_kit_bug_fails_the_run_closed_like_the_lambda_does():
    """A WorkflowDefinitionError fails the run closed; it never escapes local_runner."""
    calls: list = []
    spec = toy.toy_spec(calls=calls, fee_build=toy.transfer_ops("fee", intent_hash="f" * 64))
    outcome, port, _, audits = run(spec)
    assert outcome.state == "FAILED"
    assert "WorkflowDefinitionError" in outcome.detail
    assert port.sends == []
    assert "postcondition" in names(calls)
    assert audits[-1]["state"] == "FAILED"


# --- rule 9: unknown outcomes, crashes, reconcile, resend -----------------------------------


def test_rule9_unknown_reads_back_and_never_sends_twice():
    port = toy.ToyPort(unknown_times=Counter({"transfers.create": 1}))
    store = InMemoryOperationStore()
    outcome, port, _, _ = run(toy.toy_spec(), port=port, store=store)
    assert outcome.state == "SUCCEEDED"
    assert len(port.sends) == 2  # the fee's unknown was settled by read-back, not a resend
    assert len(port.created) == 2
    rec = store.get(outcome.results["fee"][0].request_id)
    assert rec.status == "SUCCEEDED"  # written back


def test_rule9_unknown_on_an_endpoint_that_ignores_request_id_is_never_resent():
    port = toy.ToyPort(unknown_times=Counter({"beneficiaries.create": 1}))
    spec = toy.toy_spec(fee_build=toy.transfer_ops("fee", request_id_in_body=False))
    store = InMemoryOperationStore()
    outcome, port, _, _ = run(spec, port=port, store=store)
    assert outcome.state == "FAILED"
    assert len(port.sends) == 1
    assert outcome.results["fee"][-1].status is OperationStatus.OUTCOME_UNKNOWN
    assert "not resent" in outcome.results["fee"][-1].error_message
    assert store.get(port.sends[0].request_id).status == "OUTCOME_UNKNOWN"


def test_rule9_an_adapter_crash_after_the_send_reconciles_instead_of_failing():
    """Locally too, a crash goes to reconcile, never to FAILED."""
    port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    store = InMemoryOperationStore()
    outcome, port, _, _ = run(toy.toy_spec(reconcile=True), port=port, store=store)
    assert outcome.state == "SUCCEEDED"
    assert len(port.sends) == 2  # the crashed fee was found, not resent
    fee = outcome.results["fee"][0]
    assert fee.body["_awx_recovered_from"] == "reconcile"
    assert store.get(fee.request_id).status == "SUCCEEDED"  # written back to the store


def test_rule9_a_crash_without_reconcile_resends_only_a_deduplicating_endpoint():
    port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    outcome, port, clock, _ = run(toy.toy_spec(), port=port)
    assert outcome.state == "SUCCEEDED"
    assert len(port.created) == 2  # the resend was read back, nothing was created twice
    assert 5 in clock.sleeps  # the resend wait


def test_rule9_a_crash_on_a_non_deduplicating_endpoint_stops_unknown_and_writes_back():
    port = toy.ToyPort(crash_times=Counter({"beneficiaries.create": 1}))
    spec = toy.toy_spec(fee_build=toy.transfer_ops("fee", request_id_in_body=False))
    store = InMemoryOperationStore()
    outcome, port, _, audits = run(spec, port=port, store=store)
    assert outcome.state == "FAILED"
    assert len(port.sends) == 1
    rid = port.sends[0].request_id
    assert store.get(rid).status == "OUTCOME_UNKNOWN"  # not left PENDING
    assert audits[-1]["results"]["fee"][0]["request_id"] == rid  # named in the audit


def test_rule9_reconcile_undetermined_stops_without_a_resend():
    def undetermined(op, read):
        raise ReconcileUndetermined("listing is eventually consistent")

    spec = toy.toy_spec()
    build = toy.transfer_ops("fee", request_id_in_body=False)  # no generic read-back match
    steps = (type(spec.steps[0])("fee", build, reconcile=undetermined), spec.steps[1])
    port = toy.ToyPort(unknown_times=Counter({"beneficiaries.create": 99}))
    outcome, port, _, _ = run(replace_spec(spec, steps=steps), port=port)
    assert outcome.state == "FAILED"
    assert "undetermined" in outcome.results["fee"][-1].error_message


def test_rule9_rate_limited_is_resent_with_backoff_and_the_store_records_the_success():
    port = toy.ToyPort(rate_limit_times=Counter({"transfers.create": 2}))
    store = InMemoryOperationStore()
    outcome, port, clock, _ = run(toy.toy_spec(), port=port, store=store)
    assert outcome.state == "SUCCEEDED"
    assert clock.sleeps[:2] == [5, 10]
    assert store.get(outcome.results["fee"][0].request_id).status == "SUCCEEDED"


def test_a_rerun_of_the_same_business_object_creates_nothing_new():
    store = InMemoryOperationStore()
    port = toy.ToyPort()
    first, port, _, _ = run(toy.toy_spec(), port=port, store=store)
    again, port, _, _ = run(toy.toy_spec(), port=port, store=store)
    assert first.state == again.state == "SUCCEEDED"
    assert len(port.created) == 2
    assert len(port.sends) == 2  # the store answered the second run


# --- rule 10: sandbox advances --------------------------------------------------------------


def test_rule10_sandbox_mode_drives_the_transfer_with_simulation_calls():
    port = toy.ToyPort(auto_move=False)
    outcome, port, _, audits = run(toy.toy_spec(sandbox_finality=True), port=port)
    assert outcome.state == "SUCCEEDED"
    sims = [op for op in port.sends if op.name == "transfers.simulate_transition"]
    assert [op.body["next_status"] for op in sims] == ["SENT", "PAID"]
    assert [r.status.value for r in outcome.results["pay:sandbox"]] == ["SUCCEEDED"] * 2
    assert len(audits[-1]["results"]["pay:sandbox"]) == 2


def test_rule10_a_failed_advance_is_not_resent_and_the_next_poll_decides():
    port = toy.ToyPort(auto_move=False, transition_fails=1)
    outcome, port, _, _ = run(toy.toy_spec(sandbox_finality=True), port=port)
    assert outcome.state == "SUCCEEDED"
    sims = [op for op in port.sends if op.name == "transfers.simulate_transition"]
    assert len({op.request_id for op in sims}) == len(sims)  # never the same advance twice
    assert outcome.results["pay:sandbox"][0].status is OperationStatus.FAILED


def test_rule10_advances_stop_at_max_sandbox_advances():
    spec = toy.toy_spec(sandbox_finality=True, finality_timeout_s=200)
    steps = (
        spec.steps[0],
        StepSpec(
            "pay", spec.steps[1].build, toy.transfer_finality(200, sandbox=True, max_advances=1)
        ),
    )
    port = toy.ToyPort(auto_move=False)
    outcome, port, _, _ = run(replace_spec(spec, steps=steps), port=port)
    assert outcome.state == "FAILED"
    assert "finality timeout" in outcome.detail
    assert len(outcome.results["pay:sandbox"]) == 1


def test_rule10_outside_sandbox_mode_advance_is_never_called():
    port = toy.ToyPort(auto_move=False)
    outcome, port, _, _ = run(
        toy.toy_spec(sandbox_finality=True, finality_timeout_s=20), port=port, sandbox=False
    )
    assert outcome.state == "FAILED"
    assert not any(op.name == "transfers.simulate_transition" for op in port.sends)


# --- rule 3: fan_out ------------------------------------------------------------------------


def test_fan_out_runs_one_child_per_intent_under_the_parents_approval():
    asked: list = []

    def decide(p):
        asked.append(p.action)
        return toy.approve_by("approver")(p)

    raw = {"payees": ["ben_1", "ben_2", "ben_3"], "amount": "10.00"}
    outcome, port, _, audits = run(toy.fan_out_spec(), decide=decide, raw=raw, origin=Origin.AGENT)
    assert outcome.state == "SUCCEEDED"
    assert asked == ["pay_many"]  # children are covered by the parent's approval
    assert [c.state for c in outcome.child_outcomes] == ["SUCCEEDED"] * 3
    assert len(port.created) == 6
    assert set(outcome.results) == {f"child:{i}:{s}" for i in range(3) for s in ("fee", "pay")}
    assert {a["parent_proposal_id"] for a in audits[:-1]} == {outcome.proposal.proposal_id}


def test_fan_out_children_must_be_listed_in_the_approved_computed():
    raw = {"payees": ["ben_1"], "amount": "10.00"}
    outcome, port, _, _ = run(
        toy.fan_out_spec(list_children=False), decide=toy.approve_by("approver"), raw=raw
    )
    assert outcome.state == "FAILED"
    assert "not listed" in outcome.detail
    assert port.sends == []


def test_fan_out_child_that_needs_approval_fails_closed():
    raw = {"payees": ["ben_1", "ben_2"], "amount": "10.00"}
    spec = toy.fan_out_spec(child_tiers={"ben_2": RiskTier.SINGLE})
    outcome, port, _, _ = run(spec, decide=toy.approve_by("approver"), raw=raw)
    assert [c.state for c in outcome.child_outcomes] == ["SUCCEEDED", "FAILED"]
    assert "AUTO or BLOCKED" in outcome.child_outcomes[1].detail
    assert outcome.state == "FAILED"
    assert len(port.created) == 2


def test_fan_out_blocked_child_is_skipped_and_recorded():
    calls: list = []
    raw = {"payees": ["ben_1", "ben_2"], "amount": "10.00"}
    spec = toy.fan_out_spec(child_tiers={"ben_2": RiskTier.BLOCKED}, calls=calls)
    outcome, _, _, audits = run(spec, decide=toy.approve_by("approver"), raw=raw)
    assert outcome.state == "SUCCEEDED"
    assert [c.state for c in outcome.child_outcomes] == ["SUCCEEDED", "BLOCKED"]
    assert "abandon:BLOCKED" in names(calls)
    assert audits[-1]["child_states"] == ["SUCCEEDED", "BLOCKED"]


def test_fan_out_fails_when_a_child_fails():
    raw = {"payees": ["ben_1", "ben_2"], "amount": "10.00"}
    outcome, _, _, _ = run(
        toy.fan_out_spec(),
        port=toy.ToyPort(final_status="CANCELLED"),
        decide=toy.approve_by("approver"),
        raw=raw,
    )
    assert outcome.state == "FAILED"
    assert [c.state for c in outcome.child_outcomes] == ["FAILED", "FAILED"]


def test_fan_out_children_run_as_their_resolved_tenants():
    calls: list = []
    tenants = _KnownTenants(
        {
            "platform": PLATFORM,
            "emp-1": TenantContext("emp-1", "acct_1"),
            "emp-2": TenantContext("emp-2", "acct_2"),
        }
    )
    spec = toy.fan_out_spec(
        child_tenant=lambda intent: {"ben_1": "emp-1", "ben_2": "emp-2"}[intent["payee"]],
        calls=calls,
    )
    raw = {"payees": ["ben_1", "ben_2"], "amount": "10.00"}
    outcome, port, _, audits = run(
        spec, decide=toy.approve_by("approver"), raw=raw, tenants=tenants
    )
    assert outcome.state == "SUCCEEDED"
    assert {op.on_behalf_of for op in port.sends} == {"acct_1", "acct_2"}
    assert {a["tenant_id"] for a in audits[:-1]} == {"emp-1", "emp-2"}
    assert audits[-1]["tenant_id"] == "platform"
    assert ("parent_policy", True) in calls


def test_fan_out_unknown_child_tenant_fails_closed():
    spec = toy.fan_out_spec(child_tenant=lambda intent: "nobody")
    outcome, port, _, audits = run(
        spec, decide=toy.approve_by("approver"), raw={"payees": ["ben_1"], "amount": "10.00"}
    )
    assert outcome.state == "FAILED"
    assert port.sends == []
    assert "TenantScopeViolation" in audits[-1]["detail"]


def test_fan_out_parent_must_run_as_the_platform():
    outcome, port, _, _ = run(
        toy.fan_out_spec(), tenant=TENANT_A, raw={"payees": ["ben_1"], "amount": "10.00"}
    )
    assert outcome.state == "FAILED"
    assert "platform" in outcome.detail
    assert port.sends == []


def test_fan_out_is_capped():
    raw = {"payees": [f"b{i}" for i in range(engine.MAX_FAN_OUT + 1)], "amount": "1.00"}
    outcome, port, _, _ = run(toy.fan_out_spec(), decide=toy.approve_by("approver"), raw=raw)
    assert outcome.state == "FAILED"
    assert port.sends == []


def test_read_tenant_is_only_for_platform_contexts_of_child_tenant_specs():
    seen: dict = {}

    def policy(intent, ctx):
        seen["read_tenant"] = ctx.read_tenant
        fee = Money.of("1.50", "AUD")
        return PolicyDecision(RiskTier.AUTO, ("x",), {"fee": fee, "total": fee})

    run(replace_spec(toy.toy_spec(), policy=policy))
    assert seen["read_tenant"] is None


# --- rules 4 and 5: tenant scope and read routing ------------------------------------------


def test_rule4_an_unbound_operation_in_a_connected_tenant_fails_closed():
    spec = toy.toy_spec(fee_build=toy.transfer_ops("fee", bind=False))
    outcome, port, _, _ = run(spec, tenant=TENANT_A)
    assert outcome.state == "FAILED"
    assert "TenantScopeViolation" in outcome.detail
    assert port.sends == []


def test_rule4_an_operation_naming_another_account_is_refused():
    spec = toy.toy_spec(fee_build=toy.transfer_ops("fee", bind=False, on_behalf_of="acct_B"))
    outcome, port, _, _ = run(spec, tenant=TENANT_A)
    assert outcome.state == "FAILED"
    assert "TenantScopeViolation" in outcome.detail
    assert port.sends == []


def _cat_build(*, destination=None, on_behalf_of=None, bind=True):
    def build(ctx):
        rid = derive_request_id("k3", f"payout:{ctx.intent['payee']}", "cat")
        body = {
            "request_id": rid,
            "amount": ctx.decision.computed["fee"].amount,
            "currency": "AUD",
            "reason": "goods_purchased",
        }
        if destination:
            body["destination"] = destination
        op = Operation(
            kit="k3",
            name="connected_account_transfers.create",
            method="POST",
            path="/api/v1/connected_account_transfers/create",
            body=body,
            request_id=rid,
            business_key=f"payout:{ctx.intent['payee']}",
            intent_hash=ctx.intent_hash,
            on_behalf_of=on_behalf_of,
        )
        if bind:
            op = bind_platform_operation(op, ctx.policy.tenant)
        return (op,)

    return build


@pytest.mark.parametrize(
    ("tenant", "build"),
    [
        (PLATFORM, _cat_build(destination="acct_A", bind=False)),  # platform, body-bound
        (TENANT_A, _cat_build(destination="acct_A", on_behalf_of="acct_A", bind=False)),
        (TENANT_A, _cat_build(destination="acct_B", bind=False)),
    ],
    ids=["platform-body-bound", "cat-with-on-behalf-of", "cat-other-destination"],
)
def test_rule4_body_bound_endpoints_are_scope_checked(tenant, build):
    outcome, port, _, _ = run(toy.toy_spec(fee_build=build), tenant=tenant)
    assert outcome.state == "FAILED"
    assert "TenantScopeViolation" in outcome.detail
    assert port.sends == []


def test_rule5_finality_reads_inherit_the_operations_account():
    outcome, port, _, _ = run(toy.toy_spec(), tenant=TENANT_A)
    finality = [r for r in port.reads if r.path.startswith("/api/v1/transfers/")]
    assert finality
    assert {r.on_behalf_of for r in finality} == {"acct_A"}
    assert outcome.state == "SUCCEEDED"


def test_reads_outside_the_allowlist_or_non_canonical_are_refused():
    for path in (
        "/api/v1/beneficiaries",
        "/api/v1/transfers/../beneficiaries",
        "/api/v1/transfers/x/",
        "/api/v1/transfers/%2e%2e",
    ):

        def nosy(intent, ctx, path=path):
            ctx.read(ReadRequest(path))

        with pytest.raises(TenantScopeViolation):
            run(replace_spec(toy.toy_spec(), policy=nosy))


def test_float_amounts_never_reach_policy():
    with pytest.raises(TypeError):
        run(toy.toy_spec(), raw={"payee": "ben_1", "amount": 100.0})


# --- rule 6: fresh now per call -------------------------------------------------------------


def test_rule6_each_build_and_postcondition_gets_a_fresh_now():
    calls: list = []
    run(toy.toy_spec(calls=calls), port=toy.ToyPort(reads_until_final=2))
    times = {name: t for name, t in calls if name in ("build:fee", "build:pay", "postcondition")}
    assert times["build:fee"] < times["build:pay"] < times["postcondition"]
    assert times["postcondition"] - times["build:pay"] >= timedelta(seconds=10)


def test_rule6_on_abandon_gets_a_fresh_now():
    calls: list = []
    run(toy.toy_spec(RiskTier.SINGLE, calls=calls))
    (policy_at,) = [t for n, t in calls if n == "policy"]
    (abandon_at,) = [t for n, t in calls if n.startswith("abandon")]
    assert abandon_at > policy_at


def test_approval_state_enum_values_match_outcomes():
    assert ApprovalState.EXPIRED.value == "EXPIRED"


# --- the kit's platform-wide store -----------------------------------------------------------


def test_platform_store_is_shared_by_every_tenant_and_read_only_in_policy():
    shared = InMemoryKitStore()
    spec = toy.toy_spec()
    base_build = spec.steps[0].build
    seen: list = []

    def build(ctx):
        item = ctx.policy.platform_store.get("pool")
        seen.append((ctx.policy.tenant.tenant_id, None if item is None else item.value["n"]))
        ctx.policy.platform_store.put(
            "pool",
            {"n": 1 if item is None else item.value["n"] + 1},
            expected_version=None if item is None else item.version,
        )
        return base_build(ctx)

    def writing_policy(intent, ctx):
        ctx.platform_store.put("pool", {"n": 99}, expected_version=None)

    steps = (type(spec.steps[0])("fee", build), spec.steps[1])
    spec = replace_spec(spec, steps=steps)
    tenants = _KnownTenants(
        {
            "platform": PLATFORM,
            "tenant-a": TENANT_A,
            "tenant-b": TenantContext("tenant-b", "acct_B"),
        }
    )
    for tenant in (TENANT_A, TenantContext("tenant-b", "acct_B")):
        out, _, _, _ = run(spec, tenant=tenant, tenants=tenants, platform_store=shared)
        assert out.state == "SUCCEEDED"
    assert seen == [("tenant-a", None), ("tenant-b", 1)]
    assert shared.get("pool").value == {"n": 2}
    with pytest.raises(engine.PurityViolation):
        run(replace_spec(spec, policy=writing_policy), platform_store=shared)


# --- kit rule M1: a re-emitted operation over an OUTCOME_UNKNOWN record ---------------------


def _fee_sends(port, rid):
    return [op for op in port.sends if op.request_id == rid]


def test_reemitted_unknown_that_landed_later_is_not_sent_again():
    store = InMemoryOperationStore()
    port = toy.ToyPort()
    op = toy.seed_unknown(store, port, landed=True)
    outcome, port, _, _ = run(toy.toy_spec(), port=port, store=store)
    assert outcome.state == "SUCCEEDED"
    assert _fee_sends(port, op.request_id) == []  # settled by read-back, no second write
    assert outcome.results["fee"][0].body["_awx_recovered_from"] == "read_back"
    assert store.get(op.request_id).status == "SUCCEEDED"


def test_reemitted_unknown_that_landed_is_found_by_kit_reconcile_on_any_endpoint():
    store = InMemoryOperationStore()
    port = toy.ToyPort()
    op = toy.seed_unknown(store, port, landed=True, request_id_in_body=False)
    spec = toy.toy_spec(reconcile=True, fee_build=toy.transfer_ops("fee", request_id_in_body=False))
    port.created[op.request_id]["request_id"] = op.request_id  # the kit's lookup key
    outcome, port, _, _ = run(spec, port=port, store=store)
    assert outcome.state == "SUCCEEDED"
    assert _fee_sends(port, op.request_id) == []


def test_reemitted_unknown_that_never_landed_is_resent_exactly_once():
    store = InMemoryOperationStore()
    port = toy.ToyPort()
    op = toy.seed_unknown(store, port, landed=False)
    outcome, port, _, _ = run(toy.toy_spec(), port=port, store=store)
    assert outcome.state == "SUCCEEDED"
    assert len(_fee_sends(port, op.request_id)) == 1  # the identical Operation, once
    assert _fee_sends(port, op.request_id)[0].body == op.body
    assert len(port.created) == 2
    assert store.get(op.request_id).status == "SUCCEEDED"


def test_reemitted_unknown_on_an_endpoint_that_ignores_request_id_is_never_resent():
    store = InMemoryOperationStore()
    port = toy.ToyPort()
    op = toy.seed_unknown(store, port, landed=False, request_id_in_body=False)
    spec = toy.toy_spec(fee_build=toy.transfer_ops("fee", request_id_in_body=False))
    outcome, port, _, _ = run(spec, port=port, store=store)
    assert outcome.state == "FAILED"
    assert _fee_sends(port, op.request_id) == []  # M1: in flight, a person decides
    assert outcome.results["fee"][0].status is OperationStatus.OUTCOME_UNKNOWN


# --- proposal IDs, StepContext.proposal_id, child tiers and kit 5's empty build ------------


def test_a_new_action_with_different_computed_gets_a_new_proposal_id():
    a, _, _, _ = run(toy.toy_spec(), clock=toy.Clock())
    b, _, _, _ = run(
        toy.toy_spec(), clock=toy.Clock(), port=toy.ToyPort(balance=Money.of("10.00", "AUD"))
    )
    assert a.proposal.intent == b.proposal.intent
    assert a.proposal.computed != b.proposal.computed
    assert a.proposal.proposal_id != b.proposal.proposal_id


def test_proposal_id_reaches_every_build_postcondition_and_on_abandon():
    seen: list = []
    spec = toy.toy_spec()

    def wrap(name, fn):
        def inner(ctx, *rest):
            seen.append((name, ctx.proposal_id))
            return fn(ctx, *rest)

        return inner

    steps = tuple(
        type(s)(s.name, wrap(f"build:{s.name}", s.build), s.finality, s.reconcile)
        for s in spec.steps
    )
    spec = replace_spec(
        spec,
        steps=steps,
        postcondition=wrap("post", spec.postcondition),
        on_abandon=wrap("abandon", spec.on_abandon),
    )
    ok, _, _, _ = run(spec)
    blocked, _, _, _ = run(spec, port=toy.ToyPort(balance=Money.of("10.00", "AUD")))
    assert seen == [
        ("build:fee", ok.proposal.proposal_id),
        ("build:pay", ok.proposal.proposal_id),
        ("post", ok.proposal.proposal_id),
        ("abandon", blocked.proposal.proposal_id),
    ]


def test_children_keep_their_own_tier_under_an_agent_parent():
    raw = {"payees": ["ben_1", "ben_2"], "amount": "10.00"}
    spec = toy.fan_out_spec(child_tiers={"ben_2": RiskTier.BLOCKED})
    outcome, _, _, _ = run(spec, decide=toy.approve_by("approver"), raw=raw, origin=Origin.AGENT)
    assert outcome.proposal.risk_tier is RiskTier.SINGLE  # the parent is floored
    assert [c.proposal.risk_tier for c in outcome.child_outcomes] == [
        RiskTier.AUTO,
        RiskTier.BLOCKED,
    ]  # children are not
    assert [c.state for c in outcome.child_outcomes] == ["SUCCEEDED", "BLOCKED"]
    assert outcome.state == "SUCCEEDED"


def test_kit5_a_step_with_no_operations_completes_empty_and_the_run_continues():
    calls: list = []
    spec = toy.toy_spec(calls=calls, fee_build=lambda ctx: ())
    outcome, port, _, _ = run(spec)
    assert outcome.state == "SUCCEEDED"
    assert outcome.results["fee"] == ()
    assert [op.body["reference"] for op in port.sends] == ["poc-s2-pay"]
    assert "postcondition" in names(calls)


def test_kit5_an_empty_step_with_finality_completes_too():
    outcome, port, _, _ = run(toy.toy_spec(pay_build=lambda ctx: ()))
    assert outcome.results["pay"] == ()
    assert outcome.state == "POSTCONDITION_FAILED"  # the toy postcondition wants a PAID leg
    assert len(port.sends) == 1


# --- a kit reconcile result is checked like a read-back -------------------------------------


def test_a_reconciled_object_for_another_amount_is_unknown_not_succeeded():
    # Never a definitive FAILED: it may be this operation's earlier body.
    port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))
    store = InMemoryOperationStore()
    outcome, port, _, _ = run(
        toy.toy_spec(reconcile=toy.reconcile_wrong_amount), port=port, store=store
    )
    fee = outcome.results["fee"][0]
    assert outcome.state == "FAILED"
    assert fee.status is OperationStatus.OUTCOME_UNKNOWN
    assert fee.error_code == "request_id_reused"
    assert fee.body["last_error"]["error_type"] == "StateConflict"
    rec = store.get(fee.request_id)
    assert rec.status == "OUTCOME_UNKNOWN"  # recorded through COMPLETE
    assert rec.may_have_landed
    assert len(port.sends) == 1  # nothing resent


def test_a_reconciled_object_in_a_failure_state_is_failed():
    def cancelled(op, read):
        found = toy.reconcile_by_request_id(op, read)
        return None if found is None else {**found, "status": "CANCELLED"}

    spec = toy.toy_spec(reconcile=cancelled)
    port = toy.ToyPort(crash_times=Counter({"transfers.create": 2}))
    outcome, _, _, _ = run(spec, port=port)
    assert outcome.state == "FAILED"
    assert outcome.results["fee"][0].status is OperationStatus.SUCCEEDED  # no finality on fee
    assert outcome.results["pay"][0].error_code == "provider_failure_state"


# --- a refused resend after an unknown outcome is never definitive --------------------------


def test_late_landing_with_lagging_read_back_and_refused_resend_stays_in_flight():
    seen: list = []
    spec = toy.toy_spec()
    base_post = spec.postcondition

    def post(ctx):
        seen.append([r.status for r in ctx.prior.get("fee", ())])
        return base_post(ctx)

    spec = replace_spec(spec, postcondition=post)
    port = toy.ToyPort(
        unknown_times=Counter({"transfers.create": 1}),  # the first fee send lands, unanswered
        hide_list_reads=99,  # ...and its read-back lags
        refuse_duplicates=Counter({"transfers.create": 1}),  # the resend is refused
    )
    store = InMemoryOperationStore()
    outcome, port, _, _ = run(spec, port=port, store=store)
    fee = outcome.results["fee"][-1]
    assert fee.status is OperationStatus.OUTCOME_UNKNOWN
    assert seen == [[OperationStatus.OUTCOME_UNKNOWN]]  # the kit never saw FAILED
    rec = store.get(fee.request_id)
    assert rec.status == "OUTCOME_UNKNOWN"
    assert rec.may_have_landed
    assert len(port.created) == 1  # only the original fee transfer exists
    assert "pay" not in outcome.results
    port.hide_list_reads = 0  # the original becomes visible
    again, port, _, _ = run(spec, port=port, store=store)
    assert again.state == "SUCCEEDED"
    fee2 = again.results["fee"][0]
    assert fee2.status is OperationStatus.SUCCEEDED
    assert fee2.provider_id == port.created[fee.request_id]["id"]
    assert len(port.created) == 2  # the fee once, the pay once


def test_unknown_then_429_on_every_resend_stays_in_flight():
    """Once MAX_RESENDS is spent the step result is OUTCOME_UNKNOWN, never
    the resend's retryable FAILED, so a kit never reads it as definitive."""
    seen: list = []
    spec = toy.toy_spec()
    base_post = spec.postcondition

    def post(ctx):
        seen.append([(r.status, r.error_type) for r in ctx.prior.get("fee", ())])
        return base_post(ctx)

    spec = replace_spec(spec, postcondition=post)
    port = toy.ToyPort(
        unknown_times=Counter({"transfers.create": 1}),
        hide_list_reads=99,
        rate_limit_duplicates=Counter({"transfers.create": 99}),
    )
    store = InMemoryOperationStore()
    outcome, port, _, _ = run(spec, port=port, store=store)
    fee = outcome.results["fee"][-1]
    assert fee.status is OperationStatus.OUTCOME_UNKNOWN
    assert all(status is OperationStatus.OUTCOME_UNKNOWN for status, _ in seen[0])
    assert fee.body["last_error"]["error_type"] == "RateLimited"
    rec = store.get(fee.request_id)
    assert (rec.status, rec.may_have_landed) == ("OUTCOME_UNKNOWN", True)
    assert len(port.created) == 1
    port.hide_list_reads = 0
    port.rate_limit_duplicates.clear()
    again, port, _, _ = run(spec, port=port, store=store)
    assert again.state == "SUCCEEDED"
    assert again.results["fee"][0].provider_id == port.created[fee.request_id]["id"]
    assert len(port.created) == 2  # the fee once, the pay once


def test_a_certain_operation_rate_limited_keeps_the_engine_path():
    """Control: with no unknown first, a 429 takes the retryable FAILED path (resend waits)."""
    port = toy.ToyPort(rate_limit_times=Counter({"transfers.create": 2}))
    store = InMemoryOperationStore()
    outcome, port, clock, _ = run(toy.toy_spec(), port=port, store=store)
    assert outcome.state == "SUCCEEDED"
    assert clock.sleeps[:2] == [5, 10]
    assert not store.get(outcome.results["fee"][0].request_id).may_have_landed
