"""Run a kit workflow in-process, in the same order as the KitWorkflow state machine.

Kits use this to test end to end without AWS. The phase functions are awx_spine_aws.engine's,
the same ones the kit-step Lambda calls, and every adapter call goes through
awx_spine_aws.adapter_core.dispatch, the same code the adapter Lambda runs, over awx_spine's
MutationExecutor with one attempt per call. tests/test_asl_equivalence drives the
synthesised state machine definition and checks it reaches the same outcome as this runner.

Differences from Step Functions are only in transport: decisions come from `decide` instead of
the approval API, waits call `sleep`, reads go straight to `port`, audit entries go to `audit`
instead of the audit table, and fan-out children run inline instead of as child executions.

The run starts the way the propose tool starts an execution: validate, policy, the tier floor
for `origin` (rule 11, default AGENT, so an AUTO policy still needs one approval), a Proposal
with the derived proposal ID. `decide` is called once per proposal that needs approval and
returns every decision the approvers will ever make; a proposal still PENDING after them has
EXPIRED. Fan-out children are covered by the parent's approval and are never passed to
`decide`.

`kit_store` (PolicyContext.store) and `platform_store` (PolicyContext.platform_store) each
default to a fresh InMemoryKitStore; a run in the platform tenant gets the platform store as
its `store` too, as in AWS where both are the ledger partition <kit>#platform. `tenants`
resolves child tenants and read_tenant reads; by default it knows only the run's own tenant
and the platform.
`evidence_store` goes to the MutationExecutor for files.upload operations. `sandbox` (on by
default, as the deployment runs against the sandbox) turns on rule 10's sandbox advances. A
kit-code bug (WorkflowDefinitionError) fails the run closed exactly as the Lambda does; check
`detail`.

`now` is also the MutationExecutor's clock, so the executor's attempt lease and deduplication
margin run in the test's time, and a store still on awx_spine's default (wall) clock is switched to
`now` too, so its attempt timestamps and the executor agree. Pass a store built on another
clock and it is left alone. `runs` (a RunRegistry) answers PolicyContext.run_status: the root
run is RUNNING from its start until its audit, then SUCCEEDED (as its execution ends at Done),
or FAILED if the runner itself raised. Share one RunRegistry across calls to let a later run
see an earlier one; by default each call has its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from awx_common.approvals import ApprovalDecision, Origin, Proposal, RiskTier
from awx_common.errors import TenantScopeViolation
from awx_common.evidence import EvidenceStore
from awx_common.ops import AirwallexPort, OperationResult, ReadRequest
from awx_common.store import InMemoryKitStore, KitStore
from awx_common.tenancy import (
    StaticTenantResolver,
    TenantContext,
    TenantResolver,
    check_tenant_kit,
    resolve_in_kit,
)
from awx_common.workflow import RUN_RUNNING, KitWorkflowSpec, PostconditionResult

from . import adapter_core, engine
from .engine import Action


@dataclass(frozen=True)
class WorkflowOutcome:
    proposal: Proposal
    state: str  # APPROVED path: "SUCCEEDED" | "FAILED" | "POSTCONDITION_FAILED"
    # otherwise the ApprovalState value
    results: Mapping[str, Sequence[OperationResult]]
    postcondition: PostconditionResult | None
    detail: str | None = None  # why a run failed, for test assertions; never a credential
    child_outcomes: tuple[WorkflowOutcome, ...] = ()


class RunRegistry:
    """In-process root run statuses, what DescribeExecution gives in AWS.

    Keyed (kit, action, proposal_id), as each action is its own root machine, so a lookup
    under the wrong action reads None here exactly as it does deployed. `actions` declares a
    kit's actions up front; every action that has run is known too. Child proposal IDs are
    recorded so run_status can refuse them. A local run has nothing in flight after its audit,
    so it is terminal at once (deployed, terminal waits out the adapter timeout).
    """

    def __init__(self, actions: Mapping[str, Sequence[str]] | None = None) -> None:
        self._status: dict[tuple[str, str, str], str] = {}
        self._actions: dict[str, set[str]] = {k: set(v) for k, v in (actions or {}).items()}
        self._children: set[tuple[str, str]] = set()
        self._root_action: dict[tuple[str, str], str] = {}
        self._tenant: dict[tuple[str, str], str] = {}

    def set(
        self, kit: str, action: str, proposal_id: str, status: str, tenant_id: str | None = None
    ) -> None:
        """A test may seed a run without its tenant; run_kit_workflow always records it."""
        self._actions.setdefault(kit, set()).add(action)
        self._root_action[(kit, proposal_id)] = action
        if tenant_id is not None:
            self._tenant[(kit, proposal_id)] = tenant_id
        self._status[(kit, action, proposal_id)] = status

    def add_child(self, kit: str, proposal_id: str) -> None:
        self._children.add((kit, proposal_id))

    def declare(self, kit: str, actions: Sequence[str]) -> None:
        self._actions.setdefault(kit, set()).update(actions)

    def actions(self, kit: str) -> frozenset[str]:
        return frozenset(self._actions.get(kit, ()))

    def status(self, kit: str, action: str, proposal_id: str, tenant_id: str) -> str | None:
        if (kit, proposal_id) in self._children:
            raise ValueError("run_status: that is a child proposal's ID; ask for its root run")
        other = self._root_action.get((kit, proposal_id))
        if other is not None and other != action:  # as deployed: the proposal row says so
            raise ValueError(f"run_status: {proposal_id} is a {kit}.{other} proposal")
        if not tenant_id:
            raise ValueError("run_status needs the calling run's tenant")
        owner = self._tenant.get((kit, proposal_id))
        if owner is not None and owner != tenant_id:
            raise ValueError("run_status: that proposal belongs to another tenant")
        return self._status.get((kit, action, proposal_id))


@dataclass(frozen=True)
class _LocalEnv:
    port: AirwallexPort
    clock: Callable[[], datetime]
    kit_store: KitStore
    platform_store: KitStore
    tenants: TenantResolver
    sandbox: bool

    sleeper: Callable[[float], None] = time.sleep  # run_kit_workflow passes its sleep
    runs: RunRegistry | None = None
    kit: str | None = None  # run_kit_workflow sets the spec's kit

    def run_status(self, kit: str, action: str, proposal_id: str, tenant_id: str) -> str | None:
        if self.runs is None:
            return None
        return self.runs.status(kit, action, proposal_id, tenant_id)

    def kit_actions(self, kit: str) -> frozenset[str]:
        return frozenset() if self.runs is None else self.runs.actions(kit)

    def now(self) -> datetime:
        return self.clock()

    def sleep(self, seconds: float) -> None:
        self.sleeper(seconds)

    def read_budget_s(self) -> float | None:
        return None  # in-process: no Lambda timeout

    def read(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        return engine.read_once(self.port, req)

    def read_platform(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        return engine.read_once(self.port, req)

    def store(self, kit: str, tenant: TenantContext) -> KitStore:
        if tenant.tenant_id == engine.PLATFORM_TENANT_ID:
            return self.platform_store
        return self.kit_store

    def resolve_tenant(self, tenant_id: str) -> TenantContext:
        """In this run's kit; a kit test's own kit-less resolver binds to it. An env a test
        builds by hand without a kit resolves the tenant id as the resolver has it."""
        if self.kit is None:
            return self.tenants.resolve(tenant_id)
        return resolve_in_kit(self.tenants, tenant_id, self.kit)


class _KnownTenants(StaticTenantResolver):
    """For tests that build a _LocalEnv by hand: tenant_id -> TenantContext, each bound to
    whichever kit asks; without a kit, the entry is returned as it is."""

    def __init__(self, known: Mapping[str, TenantContext]) -> None:
        super().__init__(dict(known))

    def resolve(self, tenant_id: str, *, kit: str | None = None) -> TenantContext:
        if kit is None:
            try:
                return self.tenants[tenant_id]
            except KeyError:
                raise TenantScopeViolation("unknown tenant") from None
        return super().resolve(tenant_id, kit=kit)


@dataclass(frozen=True)
class _Ctx:
    root: KitWorkflowSpec
    env: _LocalEnv
    executor: Any
    store: Any
    decide: Callable[[Proposal], Sequence[ApprovalDecision]]
    sleep: Callable[[float], None]
    audit: Callable[[Mapping[str, Any]], None] | None


def run_kit_workflow(
    spec: KitWorkflowSpec,
    raw_input: Mapping[str, Any],
    *,
    proposed_by: str,
    tenant: TenantContext,
    port: AirwallexPort,
    store: Any,
    decide: Callable[[Proposal], Sequence[ApprovalDecision]],
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    audit: Callable[[Mapping[str, Any]], None] | None = None,
    kit_store: KitStore | None = None,
    platform_store: KitStore | None = None,
    tenants: TenantResolver | None = None,
    evidence_store: EvidenceStore | None = None,
    origin: Origin = Origin.AGENT,
    sandbox: bool = True,
    runs: RunRegistry | None = None,
    actions: Sequence[str] = (),
) -> WorkflowOutcome:
    """store is an awx_spine OperationStore. `actions` declares the kit's other actions for
    PolicyContext.run_status (the spec's own action is always known)."""
    restore = _share_clock(store, now)
    try:
        return _run_kit_workflow(
            spec,
            raw_input,
            proposed_by=proposed_by,
            tenant=tenant,
            port=port,
            store=store,
            decide=decide,
            now=now,
            sleep=sleep,
            audit=audit,
            kit_store=kit_store,
            platform_store=platform_store,
            tenants=tenants,
            evidence_store=evidence_store,
            origin=origin,
            sandbox=sandbox,
            runs=runs,
            actions=actions,
        )
    finally:
        restore()


def _run_kit_workflow(
    spec: KitWorkflowSpec,
    raw_input: Mapping[str, Any],
    *,
    proposed_by: str,
    tenant: TenantContext,
    port: AirwallexPort,
    store: Any,
    decide: Callable[[Proposal], Sequence[ApprovalDecision]],
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    audit: Callable[[Mapping[str, Any]], None] | None,
    kit_store: KitStore | None,
    platform_store: KitStore | None,
    tenants: TenantResolver | None,
    evidence_store: EvidenceStore | None,
    origin: Origin,
    sandbox: bool,
    runs: RunRegistry | None,
    actions: Sequence[str],
) -> WorkflowOutcome:
    from awx_spine.executor import MutationExecutor

    runs = runs if runs is not None else RunRegistry()
    runs.declare(spec.kit, (spec.action, *actions))
    check_tenant_kit(tenant, spec.kit)  # a resolved tenant of another kit never runs
    if tenants is None:  # the run's own tenant, bound to this kit; "platform" for kits 1-4
        tenants = StaticTenantResolver({tenant.tenant_id: tenant}, kit=spec.kit)
    kit_store = kit_store or InMemoryKitStore()
    if platform_store is None:  # a platform run's own store IS the platform store
        platform = tenant.tenant_id == engine.PLATFORM_TENANT_ID
        platform_store = kit_store if platform else InMemoryKitStore()
    env = _LocalEnv(
        port=port,
        clock=now,
        kit_store=kit_store,
        platform_store=platform_store,
        tenants=tenants,
        sandbox=sandbox,
        sleeper=sleep,
        runs=runs,
        kit=spec.kit,
    )
    executor = MutationExecutor(
        port,
        store,
        sleep=sleep,
        max_attempts=adapter_core.MAX_ATTEMPTS_PER_CALL,
        evidence_store=evidence_store,
        clock=now,  # the attempt lease and dedup margin in the run's own time
    )
    ctx = _Ctx(spec, env, executor, store, decide, sleep, audit)
    proposal, decision = engine.propose(
        spec, (), raw_input, proposed_by=proposed_by, tenant=tenant, env=env, origin=origin
    )
    execution = engine.execution_input(proposal, decision, raw_input, tenant.tenant_id)
    return _run(ctx, execution, tenant)


def _share_clock(store: Any, now: Callable[[], datetime]) -> Callable[[], None]:
    """A store still on its package's default wall clock gets the run's clock for this call
    only; the returned callable puts the default back, so a store reused by a later call
    with another clock is switched again rather than keeping this call's clock."""
    from awx_spine import store as spine_store

    from . import store as aws_store

    for attr, default in (("_now", spine_store._utcnow), ("_clock", aws_store._utcnow)):
        if getattr(store, attr, None) is default:
            setattr(store, attr, now)

            def restore(attr: str = attr, default: Any = default) -> None:
                setattr(store, attr, default)

            return restore
    return lambda: None


def _run(ctx: _Ctx, execution: Mapping[str, Any], tenant: TenantContext) -> WorkflowOutcome:
    root, env = ctx.root, ctx.env
    runs, proposal = env.runs, execution["proposal"]
    assert runs is not None
    if execution.get("spec_path"):  # a child: its tenant is resolved server-side
        runs.add_child(root.kit, proposal.proposal_id)
        tenant = env.resolve_tenant(engine.resolve_tenant_id(execution["tenant_id"]))
        return _run_body(ctx, execution, tenant)
    # A root run: its status for run_status, RUNNING until its audit. As deployed, an
    # engine error still ends the execution at Done (SUCCEEDED); only a raise out of the
    # runner itself reads FAILED here (an Audit raise is FAILED deployed).
    key = (root.kit, proposal.action, proposal.proposal_id)
    runs.set(*key, RUN_RUNNING, tenant.tenant_id)
    try:
        outcome = _run_body(ctx, execution, tenant)
    except BaseException:
        runs.set(*key, "FAILED", tenant.tenant_id)
        raise
    runs.set(*key, "SUCCEEDED", tenant.tenant_id)
    return outcome


def _run_body(ctx: _Ctx, execution: Mapping[str, Any], tenant: TenantContext) -> WorkflowOutcome:
    root, env = ctx.root, ctx.env
    run = engine.start(root, execution, tenant=tenant)
    spec = engine.resolve(root, run.spec_path)
    children: list[WorkflowOutcome] = []

    # The state machine's RiskTier choice and approval wait (root machines only).
    if run.state is None and run.proposal.risk_tier in (RiskTier.SINGLE, RiskTier.DUAL):
        run = engine.binding(run, ctx.decide(run.proposal), now=env.now())

    if run.state is None:
        try:
            run = engine.recheck(root, run, env)
            if run.executing and spec.fan_out is not None:
                run = engine.fan_out(root, run, env)
                if run.state is None:
                    children = [_child(ctx, child, tenant) for child in run.children]
                    run = engine.children_done(
                        run, [None if c is None else _as_run(c, tenant) for c in children]
                    )
                    children = [c for c in children if c is not None]
            for step in spec.steps:
                if run.state is not None or not run.executing:
                    break
                run = engine.build(root, run, step.name, env)
                run = _route(ctx, run)
        except Exception as exc:
            run = engine.error(run, f"{type(exc).__name__}: {exc}")
        if engine.postcondition_due(run):
            run = engine.postcondition_guarded(root, run, env)

    run = engine.abandon(root, run, env)
    run, entry = engine.audit(run, now=env.now())
    if ctx.audit is not None:
        ctx.audit(entry)
    return WorkflowOutcome(
        proposal=run.proposal,
        state=run.state or run.approval,
        results={k: tuple(v) for k, v in run.results.items()},
        postcondition=run.postcondition,
        detail=run.detail,
        child_outcomes=tuple(children),
    )


def _route(ctx: _Ctx, run: engine.Run) -> engine.Run:
    """The state machine's Route/Dispatch loop for one step.

    An error anywhere in the loop fails the run closed from the run as it stands, so the
    step's results so far (every send that already left) reach the postcondition and the
    audit (rule 2). In Step Functions the same holds: a failed task's Catch keeps
    $.run, and the kit-step Lambda fails closed from the run it was handed.
    """
    root, env = ctx.root, ctx.env
    while run.state is None:
        st = run.step
        assert st is not None
        try:
            if st.pending is not None:
                pending = st.pending
                if pending.action is Action.RESEND:
                    ctx.sleep(pending.wait_s)
                try:
                    outcome = adapter_core.dispatch(
                        ctx.executor, ctx.store, pending.op, pending.action, pending.result
                    )
                except Exception as exc:
                    if pending.action is Action.COMPLETE or _refused(exc):
                        raise
                    run = engine.crashed(root, run, f"{type(exc).__name__}: {exc}", env)
                    continue
                if pending.action is Action.COMPLETE:
                    run = engine.completed(root, run, outcome, env)
                else:
                    run = engine.result(root, run, outcome, env)
            elif st.stage == "finality":
                ctx.sleep(st.wait_s)
                run = engine.finality_check(root, run, env)
            else:
                return run
        except Exception as exc:  # mirrors the state machine's Error catch
            return engine.error(
                run,
                f"{type(exc).__name__}: {exc}",
                refused=type(exc).__name__ in engine.REFUSAL_ERRORS,
                reused=type(exc).__name__ == engine.REUSE_ERROR,
            )
    return run


def _refused(exc: Exception) -> bool:
    """Refusals (the state machine's REFUSALS) go to Error, never to the crash path; error()
    then decides whether the operation was dispatched before."""
    name = type(exc).__name__
    return name in engine.REFUSAL_ERRORS or name == engine.REUSE_ERROR


def _child(ctx: _Ctx, child: Mapping[str, Any], tenant: TenantContext) -> WorkflowOutcome | None:
    """A child run, or None when it failed as an execution: the state machine's StartChild
    catches that into ChildLost (States.ALL), and the parent records it."""
    try:
        return _run(ctx, child, tenant)
    except Exception:
        return None


def _as_run(outcome: WorkflowOutcome, tenant: TenantContext) -> engine.Run:
    return engine.Run(
        spec_path=(),
        proposal=outcome.proposal,
        decision=engine.PolicyDecision(outcome.proposal.risk_tier, outcome.proposal.reasons),
        tenant=tenant,
        raw_input={},
        approval=outcome.state,
        results={k: tuple(v) for k, v in outcome.results.items()},
        state=outcome.state,
    )
