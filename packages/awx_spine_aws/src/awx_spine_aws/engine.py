"""The phases of one kit workflow run, shared by Step Functions and local_runner.

KitWorkflowSpec is the only description of a kit action. The KitWorkflow construct lays its
state machine out from the spec and every Lambda state calls one phase function here (through
handlers.kit_step); local_runner calls the same functions in the same order in-process. So a
kit tested with local_runner exercises the code that runs in AWS. The rules are numbered 0 to
11 in the awx_common.workflow module docstring.

Sequence, per (sub)spec:

    start       accept the proposal the propose tool (or the parent's fan_out) made: intact,
                for this spec, named after the execution, with a derived proposal ID (rule 11)
    risk tier   AUTO continues; BLOCKED goes to audit; SINGLE/DUAL wait for an approval
                signal (waitForTaskToken, timeout approval_ttl) and then `binding` recomputes
                approval_state from the stored decisions. A child is covered by its parent's
                approval, so a child tier other than AUTO or BLOCKED fails closed (rule 3).
    recheck     re-run validate and policy, apply effective_tier; a raise or a moved hash
                fails closed as STALE (rules 1 and 11)
    fan_out     one child proposal per child intent; each child intent must be listed in the
                parent's approved computed amounts (rule 3)
    steps       build, then per operation: check_tenant_scope (rule 4), the adapter, and on an
                unknown outcome or an adapter crash reconcile before any resend (rule 9); then
                the finality loop, with sandbox advances in sandbox mode (rule 10)
    postcondition  runs once steps stop, also after a failure; the outcome stays FAILED (rule 2)
    abandon     when no step executed (REJECTED, EXPIRED, BLOCKED or STALE), on_abandon runs
                just before the audit record (rule 8)

Per step the run carries a StepState. Its `pending` field names the next adapter call:
"send" (a new operation), "resend" (the same operation after reconcile found nothing, only for
endpoints that deduplicate on request_id), "advance" (a sandbox simulation, never resent) or
"complete" (write a result the runner established into the operation store, no send). Its
`stage` is "ops" while operations remain, "finality" while the loop polls, then "done".
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from awx_common.approvals import (
    ApprovalBindingError,
    ApprovalDecision,
    ApprovalState,
    Origin,
    Proposal,
    RiskTier,
    approval_state,
    derive_proposal_id,
    effective_tier,
)
from awx_common.errors import (
    AwxError,
    NotFound,
    RateLimited,
    TenantScopeViolation,
    TransientServerError,
)
from awx_common.intent import canonical_json
from awx_common.ops import Operation, OperationResult, OperationStatus, ReadRequest
from awx_common.store import KitStore, StoredItem
from awx_common.tenancy import TenantContext, bind_read, check_tenant_kit, check_tenant_scope
from awx_common.transport import encode
from awx_common.workflow import (
    RUN_RUNNING,
    RUN_TERMINAL,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    ReconcileUndetermined,
    StepContext,
    StepSpec,
)
from awx_spine.endpoints import endpoint_for, extract_ids, mismatched_fields
from awx_spine.executor import (
    DEFAULT_ATTEMPT_LEASE_S,
    FIRST_ATTEMPT_UNKNOWN_CODE,
    IN_PROGRESS_CODE,
    MISMATCH_CODE,
    PAST_MARGIN_CODE,
)

from . import codec

log = logging.getLogger("awx_spine_aws.engine")

PLATFORM_TENANT_ID = "platform"  # tenant_id of a run on the platform's own Airwallex account
CHILD_NAMESPACE = uuid.UUID("0b8e7f52-3c1d-4a9e-8f60-2d5c7a1e9b34")

# Resends after an unknown outcome or a retryable rejection, with backoff. The executor itself
# runs one attempt per adapter call, so the runner owns every resend (rule 9).
MAX_RESENDS = 3
RESEND_INTERVAL_S = 5
RESEND_BACKOFF = 2
# A resend after a crash, or after in_progress_elsewhere, waits out the executor's attempt
# lease (180 s) plus slack, so the executor treats the attempt as crashed and reconciles it
# instead of answering in_progress_elsewhere to every quick resend.
LEASE_RESEND_WAIT_S = int(DEFAULT_ATTEMPT_LEASE_S) + 15

# A NotFound on a finality read counts as list lag only this long after the loop starts
# (the idea of awx_spine.finality.poll_until_terminal's not_found_grace_s). The sandbox showed
# 0.65 to 1.9 s of lag; after the grace a 404 means a wrong read path or object, and fails
# closed rather than polling to the deadline.
NOT_FOUND_GRACE_S = 30

MAX_FAN_OUT = 50  # children per fan_out; more fails closed (bounds the root's run timeout)
MAP_CONCURRENCY = 5

# Paths reads may use: /api/v1/ then segments of safe characters, no dot segments or encodings.
_CANONICAL_PATH = re.compile(r"^/api/v1(/[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*)+$")


class Outcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    POSTCONDITION_FAILED = "POSTCONDITION_FAILED"


class Phase(StrEnum):
    START = "start"
    REQUEST_APPROVAL = "request_approval"
    BINDING = "binding"
    RECHECK = "recheck"
    FAN_OUT = "fan_out"
    CHILDREN_DONE = "children_done"
    BUILD = "build"
    RESULT = "result"  # the adapter returned a result
    CRASHED = "crashed"  # the adapter Lambda failed or timed out after it may have sent
    COMPLETED = "completed"  # the adapter wrote a runner-established result to the store
    FINALITY_CHECK = "finality_check"
    POSTCONDITION = "postcondition"
    ERROR = "error"
    AUDIT = "audit"


class Action(StrEnum):
    SEND = "send"
    RESEND = "resend"
    ADVANCE = "advance"
    COMPLETE = "complete"


class WorkflowDefinitionError(Exception):
    """Kit code broke a spine rule (wrong kit, unbound hash, duplicate request_id)."""


class PurityViolation(Exception):
    """validate, policy, fan_out or child_tenant tried to write the kit store (rule 0)."""


class Env(Protocol):
    sandbox: bool  # rule 10: drive sandbox objects with simulation calls

    def sleep(self, seconds: float) -> None:
        """Wait between read retries."""
        ...

    def read_budget_s(self) -> float | None:
        """Seconds the caller can still spend waiting on read retries; None = unbounded."""
        ...

    def now(self) -> datetime: ...

    def read(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        """Tenant GET through the reader. The request is already allowlisted and bound."""
        ...

    def read_platform(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        """Platform GET (no x-on-behalf-of) made in `tenant`'s run; the reader derives the
        platform scope from the missing header."""
        ...

    def store(self, kit: str, tenant: TenantContext) -> KitStore | None: ...

    def resolve_tenant(self, tenant_id: str) -> TenantContext:
        """Server-side tenant lookup; raises TenantScopeViolation for an unknown tenant."""
        ...

    # Optional: run_status(kit, action, proposal_id) -> RUN_RUNNING, a RUN_TERMINAL
    # status or None, and kit_actions(kit) -> the kit's actions. An env without them hands kits
    # PolicyContext.run_status = None ("decide at recheck").


@dataclass(frozen=True)
class Pending:
    op: Operation
    kind: str  # "step" or "advance"
    action: Action
    resends: int = 0
    wait_s: int = 0
    result: OperationResult | None = None  # for COMPLETE: what to write to the store


@dataclass(frozen=True)
class StepState:
    name: str
    ops: tuple[Operation, ...]
    index: int = 0  # next operation to send
    stage: str = "ops"  # "ops" | "finality" | "done"
    pending: Pending | None = None
    unfinal: tuple[int, ...] = ()  # operations still waiting for a terminal status
    deadline: datetime | None = None
    advances: tuple[int, ...] = ()  # sandbox advances made per operation
    queued: tuple[Operation, ...] = ()  # sandbox advances still to send
    wait_s: int = 0  # finality interval, for the Wait state
    # A send of this step failed after earlier sends of it left: those earlier sends still run
    # the finality loop (and sandbox advances), then the step fails with this detail.
    failed: str | None = None


@dataclass(frozen=True)
class Run:
    spec_path: tuple[str, ...]
    proposal: Proposal
    decision: PolicyDecision
    tenant: TenantContext
    raw_input: Mapping[str, Any]
    approval: str
    results: Mapping[str, tuple[OperationResult, ...]] = field(default_factory=dict)
    state: str | None = None  # set once the run has an outcome
    detail: str | None = None
    executing: bool = False  # recheck passed; steps may have started
    stale: bool = False  # recheck raised or the proposal hash moved
    step: StepState | None = None
    children: tuple[Mapping[str, Any], ...] = ()  # child execution inputs (see execution_input)
    child_states: tuple[str, ...] = ()
    # Child proposal IDs whose execution was lost (StartChild caught it as ChildLost).
    # Such a child may still be RUNNING, so run_status keeps the root live past its child
    # timeout unless the child itself reads terminal.
    lost_children: tuple[str, ...] = ()
    postcondition: PostconditionResult | None = None
    parent_proposal_id: str | None = None
    # The ROOT run of this run's fan-out tree (None on the root itself: its own).
    root_proposal_id: str | None = None
    root_action: str | None = None

    @property
    def root(self) -> tuple[str, str]:
        """(root proposal ID, root action): this run's own for a root run."""
        if self.root_proposal_id is None:
            return self.proposal.proposal_id, self.proposal.action
        return self.root_proposal_id, self.root_action or self.proposal.action

    @property
    def intent(self) -> Mapping[str, Any]:
        return self.proposal.intent

    @property
    def is_child(self) -> bool:
        return bool(self.spec_path)


# --- spec helpers ---------------------------------------------------------------------------


def resolve(root: KitWorkflowSpec, path: Sequence[str]) -> KitWorkflowSpec:
    spec = root
    for part in path:
        if part != "child" or spec.child is None:
            raise WorkflowDefinitionError(f"spec path {list(path)!r} does not exist")
        spec = spec.child
    return spec


def _union(root: KitWorkflowSpec, attr: str) -> tuple[str, ...]:
    patterns: list[str] = []
    spec: KitWorkflowSpec | None = root
    while spec is not None:
        patterns.extend(p for p in getattr(spec, attr) if p not in patterns)
        spec = spec.child
    return tuple(patterns)


def allowlist(root: KitWorkflowSpec) -> tuple[str, ...]:
    """Every tenant GET pattern a spec and its child specs may read."""
    return _union(root, "read_allowlist")


def platform_allowlist(root: KitWorkflowSpec) -> tuple[str, ...]:
    """Every platform GET pattern a spec and its child specs may read."""
    return _union(root, "platform_read_allowlist")


def step_named(spec: KitWorkflowSpec, name: str) -> StepSpec:
    for step in spec.steps:
        if step.name == name:
            return step
    raise WorkflowDefinitionError(f"{spec.kit}.{spec.action} has no step {name!r}")


def resolve_tenant_id(tenant_id: str | None) -> str:
    return PLATFORM_TENANT_ID if tenant_id is None else tenant_id


def path_allowed(patterns: Sequence[str], path: str) -> bool:
    """Shared by the runners and the reader Lambda: a canonical path fully matching a pattern."""
    if not _CANONICAL_PATH.match(path):
        return False
    return any(re.fullmatch(p, path) for p in patterns)


def dedupes(op: Operation) -> bool:
    """Rule 9: may this operation be resent after an unknown outcome? Only when awx_spine's
    endpoint table has sandbox evidence that the endpoint deduplicates (and the body carries
    the request_id), or a duplicate is inert (files.upload). Unknown endpoints deny."""
    spec = endpoint_for(op.name, op.path)
    if spec is None:
        return False
    if spec.resend_safe:
        return True
    return spec.dedupes and op.body.get("request_id") == op.request_id


def retryable(result: OperationResult) -> bool:
    err = result.error()
    return result.status is OperationStatus.FAILED and err is not None and err.retryable


def own_account_patterns(
    tenant_patterns: Sequence[str], platform_patterns: Sequence[str]
) -> tuple[str, ...]:
    """Paths a run in the PLATFORM tenant may read on the platform's own account. A kit that
    declares a platform_read_allowlist (a connected-account kit) is held to it, so a step
    running for a connected tenant cannot widen its reach by naming the platform tenant.
    A kit with none (kits 1 to 4, platform only) uses read_allowlist,
    which for it already names the platform's own account. Shared with the reader Lambda."""
    return tuple(platform_patterns) if platform_patterns else tuple(tenant_patterns)


def _has_child_tenant(root: KitWorkflowSpec) -> bool:
    spec: KitWorkflowSpec | None = root
    while spec is not None:
        if spec.child_tenant is not None:
            return True
        spec = spec.child
    return False


class _ReadOnlyStore:
    """Rule 0: the store pure callables see. Reads pass through; a write fails closed."""

    def __init__(self, inner: KitStore) -> None:
        self._inner = inner

    def get(self, key: str) -> StoredItem | None:
        return self._inner.get(key)

    def query(self, prefix: str) -> Sequence[StoredItem]:
        return self._inner.query(prefix)

    def put(self, key: str, value: Mapping[str, Any], *, expected_version: int | None) -> Any:
        raise PurityViolation("validate, policy, fan_out and child_tenant may not write the store")


def read_once(port: Any, req: ReadRequest) -> Mapping[str, Any]:
    """The engine is the one retry layer on reads (`retrying`), so awx_spine's client is
    asked for a single attempt. Any other port (a fake) is read as it is."""
    from awx_spine.client import AirwallexClient

    if isinstance(port, AirwallexClient):
        return port.read(req, retries=0)
    return port.read(req)


def policy_context(
    root: KitWorkflowSpec,
    env: Env,
    tenant: TenantContext,
    *,
    pure: bool = False,
    proposal_id: str | None = None,
) -> PolicyContext:
    """What kit policy, step and read code may see: allowlisted reads and the kit's store.
    Built fresh for every call, so `now` is the time of that call (rule 6). pure=True hands
    out a read-only store (rule 0)."""
    tenant_patterns = allowlist(root)
    platform_patterns = platform_allowlist(root)

    def read_tenant(tenant_id: str, req: ReadRequest) -> Mapping[str, Any]:
        other = env.resolve_tenant(tenant_id)
        if not path_allowed(tenant_patterns, req.path):
            raise TenantScopeViolation(f"{root.kit} may not read {req.path}")
        bound = bind_read(req, other)
        return retrying(env, lambda: env.read(root.kit, bound, other), req.path)

    own = (
        own_account_patterns(tenant_patterns, platform_patterns)
        if tenant.connected_account_id is None
        else tenant_patterns
    )

    def read(req: ReadRequest) -> Mapping[str, Any]:
        if not path_allowed(own, req.path):
            raise TenantScopeViolation(f"{root.kit} may not read {req.path}")
        bound = bind_read(req, tenant)
        return retrying(env, lambda: env.read(root.kit, bound, tenant), req.path)

    def read_platform(req: ReadRequest) -> Mapping[str, Any]:
        if req.on_behalf_of is not None:
            raise TenantScopeViolation("a platform read must not send x-on-behalf-of")
        if not path_allowed(platform_patterns, req.path):
            raise TenantScopeViolation(f"{root.kit} may not read {req.path} as the platform")
        return retrying(env, lambda: env.read_platform(root.kit, req, tenant), req.path)

    store = env.store(root.kit, tenant)
    # The kit's platform-wide state, the same in tenant and platform contexts.
    platform_store = env.store(root.kit, TenantContext(PLATFORM_TENANT_ID))
    if pure:
        store = _ReadOnlyStore(store) if store is not None else None
        platform_store = _ReadOnlyStore(platform_store) if platform_store is not None else None
    return PolicyContext(
        tenant=tenant,
        now=env.now(),
        read=read,
        store=store,
        platform_store=platform_store,
        read_platform=read_platform if platform_patterns else None,
        read_tenant=(
            read_tenant if tenant.connected_account_id is None and _has_child_tenant(root) else None
        ),
        proposal_id=proposal_id,
        run_status=_run_status(root, env, tenant),
    )


def _run_status(
    root: KitWorkflowSpec, env: Env, tenant: TenantContext
) -> Callable[[str, str], str | None] | None:
    """Read-only status of the root run of one of this kit's actions, for a proposal ID.
    Anything the env reports outside RUN_TERMINAL reads RUN_RUNNING (a redrive, say)."""
    lookup = getattr(env, "run_status", None)
    actions = getattr(env, "kit_actions", None)
    if lookup is None or actions is None:
        return None

    def run_status(proposal_id: str, action: str) -> str | None:
        if not isinstance(proposal_id, str) or not PROPOSAL_ID.fullmatch(proposal_id):
            raise ValueError("run_status needs a proposal ID")
        known = actions(root.kit)
        if action not in known:
            raise ValueError(f"run_status: {action!r} is not one of {root.kit}'s actions")
        status = lookup(root.kit, action, proposal_id, tenant.tenant_id)
        if status is None or status in RUN_TERMINAL:
            return status
        return RUN_RUNNING

    return run_status


PROPOSAL_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")  # a valid Step Functions execution name


def op_read(
    root: KitWorkflowSpec, env: Env, tenant: TenantContext, op: Operation
) -> Callable[[ReadRequest], Mapping[str, Any]]:
    """Rule 5: a finality or reconcile read inherits the operation's on_behalf_of."""
    tenant_patterns = allowlist(root)
    platform_patterns = platform_allowlist(root)

    def read(req: ReadRequest) -> Mapping[str, Any]:
        if req.on_behalf_of not in (None, op.on_behalf_of):
            raise TenantScopeViolation("read names a different account than its operation")
        req = replace(req, on_behalf_of=op.on_behalf_of)
        if req.on_behalf_of is None and tenant.connected_account_id is not None:
            if not path_allowed(platform_patterns, req.path):
                raise TenantScopeViolation(f"{root.kit} may not read {req.path} as the platform")
            return retrying(env, lambda: env.read_platform(root.kit, req, tenant), req.path)
        patterns = (
            own_account_patterns(tenant_patterns, platform_patterns)
            if tenant.connected_account_id is None
            else tenant_patterns
        )
        if not path_allowed(patterns, req.path):
            raise TenantScopeViolation(f"{root.kit} may not read {req.path}")
        bound = bind_read(req, tenant)
        return retrying(env, lambda: env.read(root.kit, bound, tenant), req.path)

    return read


def step_context(root: KitWorkflowSpec, run: Run, env: Env) -> StepContext:
    return StepContext(
        intent=run.intent,
        decision=run.decision,
        policy=policy_context(root, env, run.tenant, proposal_id=run.proposal.proposal_id),
        intent_hash=run.proposal.intent_hash,
        proposal_id=run.proposal.proposal_id,  # M4: the owner of every hold this run takes
        prior={k: tuple(v) for k, v in run.results.items()},
        root_proposal_id=run.root[0],  # what a person's tool guards with run_status
        root_action=run.root[1],
    )


def _tier(spec: KitWorkflowSpec, policy_tier: RiskTier, origin: Origin, child: bool) -> RiskTier:
    """Rule 11. A child is covered by its parent's (floored) approval, so no floor applies."""
    if not isinstance(policy_tier, RiskTier):
        raise WorkflowDefinitionError("policy must return a RiskTier")
    return policy_tier if child else effective_tier(policy_tier, origin, spec.agent_min_tier)


def propose(
    root: KitWorkflowSpec,
    spec_path: Sequence[str],
    raw_input: Mapping[str, Any],
    *,
    proposed_by: str,
    tenant: TenantContext,
    env: Env,
    origin: Origin = Origin.AGENT,
    proposal_id: str | None = None,
) -> tuple[Proposal, PolicyDecision]:
    """validate, policy, effective tier, Proposal.create: what a propose tool (or fan_out) does.
    The proposal ID defaults to derive_proposal_id, which is also the execution name."""
    spec = resolve(root, spec_path)
    if not proposed_by:
        raise ValueError("proposed_by is required")
    check_tenant_kit(tenant, spec.kit)  # never a resolved tenant of another kit
    intent = spec.validate(raw_input)
    ctx = policy_context(root, env, tenant, pure=True, proposal_id=proposal_id)
    decision = spec.policy(intent, ctx)
    tier = _tier(spec, decision.risk_tier, origin, bool(spec_path))
    if proposal_id is None:
        proposal_id = derive_proposal_id(
            kit=spec.kit,
            action=spec.action,
            intent=intent,
            tenant_id=tenant.tenant_id,
            proposed_by=proposed_by,
            now=ctx.now,
            ttl=spec.approval_ttl,
            computed=decision.computed,  # a new action with new computed gets a new ID
        )
    proposal = Proposal.create(
        kit=spec.kit,
        action=spec.action,
        intent=intent,
        computed=decision.computed,
        risk_tier=tier,
        reasons=decision.reasons,
        proposed_by=proposed_by,
        now=ctx.now,
        ttl=spec.approval_ttl,
        proposal_id=proposal_id,
        origin=origin,
    )
    return proposal, decision


def execution_input(
    proposal: Proposal,
    decision: PolicyDecision,
    raw_input: Mapping[str, Any],
    tenant_id: str | None,
    *,
    parent_proposal_id: str | None = None,
    spec_path: Sequence[str] = (),
    root: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """The KitWorkflow execution input, before transport (the agent plane builds the same).
    `origin` travels both inside the proposal (and its hash) and alongside it (rule 11)."""
    data: dict[str, Any] = {
        "proposal": proposal,
        "raw_input": dict(raw_input),
        "tenant_id": tenant_id,
        "decision": decision,
        "origin": proposal.origin.value,
    }
    if spec_path:
        data["parent_proposal_id"] = parent_proposal_id
        data["spec_path"] = list(spec_path)
        if root is None:
            raise WorkflowDefinitionError("a child execution names its root run")
        data["root_proposal_id"], data["root_action"] = root
    return data


# --- phases before the steps ----------------------------------------------------------------


def start(
    root: KitWorkflowSpec,
    execution: Mapping[str, Any],
    *,
    tenant: TenantContext,
    execution_name: str | None = None,
) -> Run:
    """Accept the proposal the propose tool (or the parent's fan_out) made.

    `execution` is the decoded execution input. The proposal must be intact, for this spec,
    named after the execution, carry the origin and tier the inputs imply and, for a root,
    have the derived proposal ID. Policy is re-run later, before the first step (recheck)."""
    spec_path = tuple(execution.get("spec_path") or ())
    spec = resolve(root, spec_path)
    check_tenant_kit(tenant, spec.kit)  # a tenant row belongs to one kit
    proposal = execution["proposal"]
    if not isinstance(proposal, Proposal):
        proposal = codec.proposal_from(proposal)
    decision = execution["decision"]
    if not isinstance(decision, PolicyDecision):
        decision = codec.policy_decision_from(decision)
    proposal.verify_hash()
    if (proposal.kit, proposal.action) != (spec.kit, spec.action):
        raise WorkflowDefinitionError("proposal is for a different kit action")
    if execution_name is not None and execution_name != proposal.proposal_id:
        raise WorkflowDefinitionError("execution name must equal the proposal ID")
    if execution.get("origin", proposal.origin.value) != proposal.origin.value:
        raise ApprovalBindingError("execution origin does not match the proposal")
    expected_tier = _tier(spec, decision.risk_tier, proposal.origin, bool(spec_path))
    if expected_tier is not proposal.risk_tier or dict(decision.computed) != dict(
        proposal.computed
    ):
        raise ApprovalBindingError("decision does not match the proposal")
    if not spec_path:
        derived = derive_proposal_id(
            kit=proposal.kit,
            action=proposal.action,
            intent=proposal.intent,
            tenant_id=tenant.tenant_id,
            proposed_by=proposal.proposed_by,
            now=proposal.created_at,
            ttl=proposal.expires_at - proposal.created_at,
            computed=proposal.computed,
        )
        if derived != proposal.proposal_id:
            raise ApprovalBindingError("proposal ID is not the derived proposal ID")
    run = Run(
        spec_path=spec_path,
        proposal=proposal,
        decision=decision,
        tenant=tenant,
        raw_input=dict(execution["raw_input"]),
        approval=ApprovalState.PENDING.value,
        parent_proposal_id=execution.get("parent_proposal_id"),
        root_proposal_id=execution.get("root_proposal_id") if spec_path else None,
        root_action=execution.get("root_action") if spec_path else None,
    )
    if spec_path and (not run.root_proposal_id or not run.root_action):
        raise ApprovalBindingError("a child execution must name its root run")
    if spec.fan_out is not None and tenant.connected_account_id is not None:
        return replace(
            run, state=Outcome.FAILED.value, detail="a fan_out parent runs as the platform tenant"
        )
    tier = proposal.risk_tier
    if tier is RiskTier.BLOCKED:
        return replace(run, approval=ApprovalState.BLOCKED.value, state=ApprovalState.BLOCKED.value)
    if tier is RiskTier.AUTO:
        return replace(run, approval=ApprovalState.NOT_REQUIRED.value)
    if run.is_child:  # rule 3: the parent's approval covers its children
        return replace(
            run,
            state=Outcome.FAILED.value,
            detail=f"child policy returned {tier.value}; a child may only be AUTO or BLOCKED",
        )
    return run


def binding(run: Run, decisions: Sequence[ApprovalDecision], *, now: datetime) -> Run:
    """Recompute the approval from stored decisions once the approval wait has ended, so a
    proposal still PENDING has EXPIRED. A decision that does not bind fails closed."""
    try:
        state = approval_state(run.proposal, decisions, now=now)
    except ApprovalBindingError as exc:
        return replace(
            run,
            approval=ApprovalState.REJECTED.value,
            state=ApprovalState.REJECTED.value,
            detail=f"approval binding failed: {exc}",
        )
    if state is ApprovalState.PENDING:
        state = ApprovalState.EXPIRED
    if state in (ApprovalState.APPROVED, ApprovalState.NOT_REQUIRED):
        return replace(run, approval=state.value)
    return replace(run, approval=state.value, state=state.value)


def recheck(root: KitWorkflowSpec, run: Run, env: Env) -> Run:
    """Rules 1 and 11: re-run validate and policy, apply the same tier floor, and fail closed
    as STALE if the proposal would differ."""
    spec = resolve(root, run.spec_path)
    p = run.proposal
    try:
        intent = spec.validate(run.raw_input)
        decision = spec.policy(
            intent,
            policy_context(root, env, run.tenant, pure=True, proposal_id=p.proposal_id),
        )
        fresh = Proposal.create(
            kit=spec.kit,
            action=spec.action,
            intent=intent,
            computed=decision.computed,
            risk_tier=_tier(spec, decision.risk_tier, p.origin, run.is_child),
            reasons=decision.reasons,
            proposed_by=p.proposed_by,
            now=p.created_at,
            ttl=p.expires_at - p.created_at,
            proposal_id=p.proposal_id,
            origin=p.origin,
        )
    except Exception as exc:
        return replace(
            run, state=Outcome.FAILED.value, stale=True, detail=f"recheck {_describe(exc)}"
        )
    if fresh.intent_hash != p.intent_hash:
        return replace(
            run,
            state=Outcome.FAILED.value,
            stale=True,
            detail=(
                "intent, computed amounts or risk tier changed since the proposal; propose again"
            ),
        )
    return replace(run, decision=decision, executing=True)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]


def _listed(child: Mapping[str, Any], computed: Mapping[str, Any]) -> bool:
    """Rule 3: a child intent counts as approved only if the parent's computed lists it."""
    target = canonical_json(child)
    for value in computed.values():
        if isinstance(value, list | tuple) and any(
            canonical_json(item) == target for item in value
        ):
            return True
    return False


def fan_out(root: KitWorkflowSpec, run: Run, env: Env) -> Run:
    """One child proposal per child intent. Child proposal IDs are derived from the parent's,
    so a redelivered fan_out names the same child executions."""
    spec = resolve(root, run.spec_path)
    if spec.fan_out is None:
        raise WorkflowDefinitionError("spec has no fan_out")
    raws = [dict(c) for c in spec.fan_out(run.intent, run.decision)]
    if len(raws) > MAX_FAN_OUT:
        return replace(
            run, state=Outcome.FAILED.value, detail=f"fan_out made {len(raws)} > {MAX_FAN_OUT}"
        )
    unlisted = [i for i, raw in enumerate(raws) if not _listed(raw, run.proposal.computed)]
    if unlisted:
        return replace(
            run,
            state=Outcome.FAILED.value,
            detail=f"fan_out children {unlisted} are not listed in the approved computed amounts",
        )
    child_path = (*run.spec_path, "child")
    child_spec = resolve(root, child_path)
    children = []
    for i, raw in enumerate(raws):
        child_id = str(uuid.uuid5(CHILD_NAMESPACE, f"{run.proposal.proposal_id}/{i}"))
        tenant = run.tenant
        if spec.child_tenant is not None:  # rule 3: resolved server-side, unknown refused
            tenant = env.resolve_tenant(spec.child_tenant(child_spec.validate(raw)))
        proposal, decision = propose(
            root,
            child_path,
            raw,
            proposed_by=run.proposal.proposed_by,
            tenant=tenant,
            env=env,
            origin=run.proposal.origin,
            proposal_id=child_id,
        )
        children.append(
            execution_input(
                proposal,
                decision,
                raw,
                tenant.tenant_id,
                parent_proposal_id=run.proposal.proposal_id,
                spec_path=child_path,
                root=run.root,
            )
        )
    return replace(run, children=tuple(children))


def children_done(run: Run, children: Sequence[Run | None]) -> Run:
    """A BLOCKED child is skipped and recorded; any other non-success, or a child execution
    that failed as an execution (None), fails the parent."""
    if len(children) != len(run.children):
        raise WorkflowDefinitionError("child run count does not match fan_out")
    results = dict(run.results)
    states: list[str] = []
    lost: list[str] = []
    for i, child in enumerate(children):
        if child is None:
            # The child failed as an EXECUTION (ChildLost). Its execution may still be running
            # (a .sync task failure), so record it: the audit marks it abnormal and run_status
            # keeps this run live until the child is over.
            states.append(Outcome.FAILED.value)
            lost.append(_child_id(run.children[i]))
            continue
        states.append(child.state or Outcome.FAILED.value)
        for step, step_results in child.results.items():
            results[f"child:{i}:{step}"] = tuple(step_results)
    ok = {Outcome.SUCCEEDED.value, ApprovalState.BLOCKED.value}
    failed = [i for i, s in enumerate(states) if s not in ok]
    run = replace(run, results=results, child_states=tuple(states), lost_children=tuple(lost))
    if failed:
        return replace(
            run, state=Outcome.FAILED.value, detail=f"child runs {failed} did not succeed"
        )
    return run


def _child_id(child_input: Mapping[str, Any]) -> str:
    proposal = child_input["proposal"]
    pid = proposal.proposal_id if isinstance(proposal, Proposal) else proposal["proposal_id"]
    return str(pid)


# --- steps ----------------------------------------------------------------------------------


def _check_op(spec: KitWorkflowSpec, run: Run, op: Operation, seen: set[str]) -> None:
    if not isinstance(op, Operation):
        raise WorkflowDefinitionError(f"kit code returned {type(op).__name__}, not an Operation")
    if op.kit != spec.kit:
        raise WorkflowDefinitionError(f"operation {op.name} is for {op.kit}, not {spec.kit}")
    if op.intent_hash != run.proposal.intent_hash:
        raise WorkflowDefinitionError(f"operation {op.name} is not bound to this proposal's hash")
    if op.request_id in seen:
        raise WorkflowDefinitionError(f"request_id reused within one run by {op.name}")
    seen.add(op.request_id)
    check_tenant_scope(op, run.tenant)  # rule 4: deny by default, nothing sent on a violation


def _seen(run: Run) -> set[str]:
    seen = {r.request_id for rs in run.results.values() for r in rs}
    if run.step is not None:
        seen |= {o.request_id for o in run.step.ops}
    return seen


def build(root: KitWorkflowSpec, run: Run, step_name: str, env: Env) -> Run:
    spec = resolve(root, run.spec_path)
    step = step_named(spec, step_name)
    ops = tuple(step.build(step_context(root, run, env)))
    seen = {r.request_id for rs in run.results.values() for r in rs}
    for op in ops:
        _check_op(spec, run, op, seen)
    state = StepState(name=step_name, ops=ops)
    run = replace(run, results={**run.results, step_name: ()}, step=state)
    return _advance_ops(root, run, env)


def _advance_ops(root: KitWorkflowSpec, run: Run, env: Env) -> Run:
    """After an operation is settled, queue the next one or move to the finality stage."""
    st = run.step
    assert st is not None
    if st.index < len(st.ops):
        return replace(
            run, step=replace(st, pending=Pending(st.ops[st.index], "step", Action.SEND))
        )
    fin = step_named(resolve(root, run.spec_path), st.name).finality
    if fin is None or not st.ops:
        return replace(run, step=replace(st, pending=None, stage="done"))
    return replace(
        run,
        step=replace(
            st,
            pending=None,
            stage="finality",
            unfinal=tuple(range(len(st.ops))),
            deadline=env.now() + timedelta(seconds=fin.timeout_s),
            advances=(0,) * len(st.ops),
            wait_s=fin.interval_s,
        ),
    )


def _record_step(root: KitWorkflowSpec, run: Run, result: OperationResult, env: Env) -> Run:
    st = run.step
    assert st is not None
    assert st.pending is not None
    op = st.pending.op
    results = {**run.results, st.name: (*run.results.get(st.name, ()), result)}
    st = replace(st, index=st.index + 1, pending=None)
    run = replace(run, results=results, step=st)
    if result.status is not OperationStatus.SUCCEEDED:
        detail = f"{op.name} {result.status.value}"
        if result.error_type:
            detail += f" ({result.error_type}: {result.error_code})"
        left = tuple(
            i for i, r in enumerate(results[st.name][:-1]) if r.status is OperationStatus.SUCCEEDED
        )
        fin = step_named(resolve(root, run.spec_path), st.name).finality
        if fin is None or not left:
            return replace(run, state=Outcome.FAILED.value, detail=detail)
        # Money that already left must not stay in flight because a later send of the same
        # step failed (seen with kit 6 in the live sandbox runs). The rest of the step is not
        # sent; the sends that left run the finality loop, then the run fails with this send's
        # detail.
        return replace(
            run,
            step=replace(
                st,
                stage="finality",
                unfinal=left,
                deadline=env.now() + timedelta(seconds=fin.timeout_s),
                advances=(0,) * len(st.ops),
                wait_s=fin.interval_s,
                failed=detail,
            ),
        )
    return _advance_ops(root, run, env)


def _record_advance(run: Run, result: OperationResult) -> Run:
    """Rule 10: an advance's result is recorded; a failed advance is not a run failure, the
    next poll decides."""
    st = run.step
    assert st is not None
    key = f"{st.name}:sandbox"
    results = {**run.results, key: (*run.results.get(key, ()), result)}
    queued = st.queued
    pending = Pending(queued[0], "advance", Action.ADVANCE) if queued else None
    return replace(
        run,
        results=results,
        step=replace(st, pending=pending, queued=queued[1:] if queued else ()),
    )


def _unknown(op: Operation, message: str) -> OperationResult:
    return OperationResult(
        request_id=op.request_id,
        status=OperationStatus.OUTCOME_UNKNOWN,
        error_type="OutcomeUnknown",
        error_message=message[:300],
    )


def result(root: KitWorkflowSpec, run: Run, outcome: OperationResult, env: Env) -> Run:
    """The adapter returned a result for the pending operation."""
    st = run.step
    if st is None or st.pending is None:
        raise WorkflowDefinitionError("no operation is waiting for a result")
    pending = st.pending
    if outcome.request_id != pending.op.request_id:
        raise WorkflowDefinitionError("result request_id does not match the operation sent")
    if pending.kind == "advance":
        return _record_advance(run, outcome)
    if outcome.status is OperationStatus.OUTCOME_UNKNOWN:
        return _after_unknown(root, run, env, outcome, crashed=False)
    if retryable(outcome) and pending.resends < MAX_RESENDS:
        # Rejected before acceptance (429, a request that never left): the same op again.
        return _resend(run, pending)
    return _record_step(root, run, outcome, env)


def crashed(root: KitWorkflowSpec, run: Run, error_text: str, env: Env) -> Run:
    """The adapter Lambda failed, timed out or ran out of memory after it may have sent the
    operation (rule 9). Treated exactly like an unknown outcome, never as a plain failure."""
    st = run.step
    if st is None or st.pending is None:
        raise WorkflowDefinitionError("no operation is waiting for a result")
    pending = st.pending
    outcome = _unknown(pending.op, f"adapter failed after the call may have left: {error_text}")
    if pending.kind == "advance":  # rule 10: never resent; write the unknown back
        complete = replace(pending, action=Action.COMPLETE, result=outcome)
        return replace(run, step=replace(st, pending=complete))
    return _after_unknown(root, run, env, outcome, crashed=True)


def _resend(run: Run, pending: Pending, *, past_lease: bool = False) -> Run:
    st = run.step
    assert st is not None
    wait = RESEND_INTERVAL_S * RESEND_BACKOFF**pending.resends
    if past_lease:  # let the attempt lease run out, so the executor reconciles
        wait = max(wait, LEASE_RESEND_WAIT_S)
    nxt = replace(pending, action=Action.RESEND, resends=pending.resends + 1, wait_s=wait)
    return replace(run, step=replace(st, pending=nxt))


# OUTCOME_UNKNOWN codes a resend cannot change: rule 9 stops at once rather than wait out
# MAX_RESENDS. in_progress_elsewhere stays resendable (those resends wait out the attempt lease).
FINAL_UNKNOWN_CODES = frozenset(
    {
        MISMATCH_CODE,
        "request_id_bound_to_another_body",
        "multiple_objects",
        FIRST_ATTEMPT_UNKNOWN_CODE,
        PAST_MARGIN_CODE,
    }
)


def _after_unknown(
    root: KitWorkflowSpec, run: Run, env: Env, outcome: OperationResult, *, crashed: bool
) -> Run:
    """Rule 9: reconcile before any resend; resend only an endpoint that deduplicates."""
    st = run.step
    assert st is not None
    assert st.pending is not None
    pending = st.pending
    op = pending.op
    step = step_named(resolve(root, run.spec_path), st.name)

    def stop(why: str) -> Run:
        final = replace(outcome, error_message=f"{outcome.error_message or ''}; {why}"[:300])
        if crashed:  # the store may still say PENDING: write the unknown back first
            complete = replace(pending, action=Action.COMPLETE, result=final)
            return replace(run, step=replace(st, pending=complete))
        return _record_step(root, run, final, env)  # the adapter already stored it

    if step.reconcile is not None:
        try:
            found = step.reconcile(op, op_read(root, env, run.tenant, op))
        except ReconcileUndetermined as exc:
            return stop(f"reconcile undetermined: {exc}")
        except Exception as exc:
            return stop(f"reconcile raised {_describe(exc)}")
        if found is not None:
            settled = _settle_found(op, step, found)
            complete = replace(pending, action=Action.COMPLETE, result=settled)
            return replace(run, step=replace(st, pending=complete))
    if outcome.error_code in FINAL_UNKNOWN_CODES:
        return stop(f"{outcome.error_code}: a resend cannot change it; a person decides")
    if not dedupes(op):
        return stop("endpoint does not deduplicate on request_id; not resent")
    if pending.resends >= MAX_RESENDS:
        return stop(f"still unknown after {MAX_RESENDS} resends")
    return _resend(run, pending, past_lease=crashed or outcome.error_code == IN_PROGRESS_CODE)


def mismatch_result(
    op: Operation, fields: Sequence[str], found_id: str | None, message: str
) -> OperationResult:
    """The one mismatch shape, the same as awx_spine's executor: a
    top-level provider_id or provider_status names an object only when it matched this
    operation, so both are None; the object found is in body["details"]["provider_id"]."""
    return OperationResult(
        request_id=op.request_id,
        status=OperationStatus.OUTCOME_UNKNOWN,
        error_type="OutcomeUnknown",
        error_code=MISMATCH_CODE,
        error_message=message[:300],
        body={
            "details": {"fields": list(fields), "provider_id": found_id},
            "last_error": {  # the one last_error shape, as awx_spine.executor writes it
                "error_type": "StateConflict",
                "error_code": MISMATCH_CODE,
                "http_status": None,  # the read that found it succeeded; nothing refused us
                "error_message": message[:300],
            },
        },
    )


def _settle_found(op: Operation, step: StepSpec, found: Mapping[str, Any]) -> OperationResult:
    """What a kit reconcile found, checked before it can count as this operation, exactly as
    awx_spine's executor checks a read-back: an object that does not echo the operation's
    amount, currency or counterparty is OUTCOME_UNKNOWN, error_code request_id_reused: the
    object Airwallex holds under our request_id may be this kit's own earlier body, so money
    may have moved and a person decides. Never a definitive FAILED. awx_spine's endpoint table
    carries no success-status set, so the step's FinalitySpec supplies it: a found object
    already in a terminal non-success state (for example a CANCELLED transfer, M1's definitive
    failure) is FAILED too."""
    spec = endpoint_for(op.name, op.path)
    pid, status = extract_ids(spec, found)
    wrong = mismatched_fields(spec, op.body, found)
    if wrong:
        message = (
            f"the object holding request_id {op.request_id} does not match this operation "
            f"({', '.join(wrong)}); it may be an earlier body of this operation, so money may "
            "have moved"
        )
        return mismatch_result(op, wrong, pid, message)
    fin = step.finality
    if fin is not None and status in fin.terminal and status not in fin.success:
        return OperationResult(
            request_id=op.request_id,
            status=OperationStatus.FAILED,
            http_status=200,
            provider_id=pid,
            provider_status=status,
            error_type="StateConflict",
            error_code="provider_failure_state",
            error_message=f"reconciled object is {status}",
        )
    return OperationResult(
        request_id=op.request_id,
        status=OperationStatus.SUCCEEDED,
        http_status=200,
        provider_id=pid,
        provider_status=status,
        body={**dict(found), "_awx_recovered_from": "reconcile"},
    )


def completed(root: KitWorkflowSpec, run: Run, stored: OperationResult, env: Env) -> Run:
    """The adapter wrote the runner's result into the operation store and returned what the
    store now holds (an earlier definitive result wins)."""
    st = run.step
    if st is None or st.pending is None or st.pending.action is not Action.COMPLETE:
        raise WorkflowDefinitionError("no completion is waiting")
    if st.pending.kind == "advance":
        return _record_advance(run, stored)
    return _record_step(root, run, stored, env)


def transient_read_error(exc: BaseException) -> str | None:
    """A read failure worth retrying or tolerating: RateLimited, TransientServerError or
    any 5xx, a read timeout. Never NotFound, TenantScopeViolation, Unauthorized,
    ValidationFailed, an allowlist refusal or kit code."""
    if isinstance(exc, TenantScopeViolation):
        return None
    status = getattr(exc, "http_status", None)
    if isinstance(exc, RateLimited | TransientServerError) or (
        isinstance(exc, AwxError) and isinstance(status, int) and status >= 500
    ):
        return f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")
    if isinstance(exc, TimeoutError) or (
        isinstance(exc, AwxError) and str(exc).startswith("Sandbox.Timedout")
    ):
        return f"read timeout ({type(exc).__name__})"
    return None


READ_RETRIES = 3
READ_BACKOFF_S = 1.0
RETRY_AFTER_CAP_S = 30.0


def _retry_after(exc: BaseException) -> float | None:
    details = getattr(exc, "details", None) or {}
    for key in ("retry_after", "retry_after_s", "Retry-After"):
        value = details.get(key)
        if value is None:
            continue
        try:
            return min(max(float(value), 0.0), RETRY_AFTER_CAP_S)
        except (TypeError, ValueError):
            return None
    return None


def retrying(env: Env, read: Callable[[], Mapping[str, Any]], what: str) -> Mapping[str, Any]:
    """Retry a transient read failure up to READ_RETRIES times with exponential backoff
    (Retry-After when the error carries one), as long as the wait fits in the caller's
    remaining budget (env.read_budget_s(), None = unbounded); then raise the last error.
    Non-transient errors are never retried."""
    for attempt in range(READ_RETRIES + 1):
        try:
            return read()
        except Exception as exc:
            why = transient_read_error(exc)
            if why is None or attempt == READ_RETRIES:
                raise
            wait = _retry_after(exc)
            if wait is None:
                wait = READ_BACKOFF_S * 2**attempt
            budget = env.read_budget_s()
            if budget is not None and wait >= budget:
                raise
            log.info("read %s failed (%s); retry %d in %.1fs", what, why, attempt + 1, wait)
            env.sleep(wait)
    raise AssertionError("unreachable")


def transient_read_failure(
    exc: BaseException, result: OperationResult, *, lag_window: bool = True
) -> str | None:
    """A finality read failure that means "not final yet", described by type and status
    (never a body); None for every other failure, which fails the run closed.

    Transient: RateLimited (429), TransientServerError or any 5xx, a read timeout (awx_spine's
    client reports a transport timeout as TransientServerError; a reader Lambda that timed out
    arrives as Sandbox.Timedout), and NotFound for an operation whose write SUCCEEDED, within
    NOT_FOUND_GRACE_S of the loop's start (the object is not visible yet under list lag).
    TenantScopeViolation, Unauthorized, ValidationFailed, an allowlist refusal and kit-code
    errors are never transient."""
    why = transient_read_error(exc)
    if why is not None:
        return why
    if isinstance(exc, NotFound) and result.status is OperationStatus.SUCCEEDED and lag_window:
        return "NotFound (the object is not visible yet)"
    return None


def finality_check(root: KitWorkflowSpec, run: Run, env: Env) -> Run:
    st = run.step
    if st is None or st.stage != "finality":
        raise WorkflowDefinitionError("finality loop not active")
    fin = step_named(resolve(root, run.spec_path), st.name).finality
    assert fin is not None
    spec = resolve(root, run.spec_path)
    step_results = list(run.results[st.name])
    advances = list(st.advances)
    queued: list[Operation] = []
    seen = _seen(run)
    still: list[int] = []
    read_failure: str | None = None
    now = env.now()
    started = st.deadline - timedelta(seconds=fin.timeout_s) if st.deadline else now
    lag_window = now - started < timedelta(seconds=NOT_FOUND_GRACE_S)
    for i in st.unfinal:
        try:
            op = st.ops[i]
            try:
                body = op_read(root, env, run.tenant, op)(fin.read_request(step_results[i]))
            except Exception as exc:
                why = transient_read_failure(exc, step_results[i], lag_window=lag_window)
                if why is None:  # a bug or misconfiguration: fail closed (rule 7)
                    # Keep what the reads before this one established.
                    kept = replace(run, results={**run.results, st.name: tuple(step_results)})
                    return error(kept, f"finality read {type(exc).__name__}: {exc}")
                log.warning(
                    "finality read for %s %s not final yet: %s",
                    op.name,
                    op.request_id,
                    why,
                )
                read_failure = f"the last read of operation {i} failed: {why}"
                still.append(i)
                continue
            if fin.select is not None:  # pick this operation's own row out of a list read
                body = fin.select(body, step_results[i])
                if body is None:  # not visible yet: a non-terminal poll within the deadline
                    read_failure = f"operation {i}'s object is not in the read yet"
                    still.append(i)
                    continue
            status = fin.status_of(body)
            if status in fin.terminal:
                step_results[i] = replace(step_results[i], provider_status=status)
                if status not in fin.success:
                    ended = f"step {st.name!r} operation {i} ended {status}"
                    return replace(
                        run,
                        results={**run.results, st.name: tuple(step_results)},
                        step=replace(st, unfinal=(), stage="done"),
                        state=Outcome.FAILED.value,
                        detail=f"{st.failed}; {ended}" if st.failed else ended,
                    )
                continue
            still.append(i)
            if env.sandbox and fin.sandbox_advance is not None:
                if advances[i] < fin.max_sandbox_advances:
                    adv = fin.sandbox_advance(op, step_results[i], body, now, advances[i] + 1)
                    if adv is not None:
                        _check_op(spec, run, adv, seen)
                        advances[i] += 1
                        queued.append(adv)
        except Exception as exc:  # select, status_of, sandbox_advance, _check_op (rule 7)
            # Fail closed, keeping what this poll already found. Finality never honours a
            # refusal: no operation is pending here, so error() adds none.
            kept = replace(run, results={**run.results, st.name: tuple(step_results)})
            return error(kept, f"finality {type(exc).__name__}: {exc}")
    run = replace(run, results={**run.results, st.name: tuple(step_results)})
    st = replace(st, unfinal=tuple(still), advances=tuple(advances))
    if not still:
        if st.failed:
            return replace(
                run, step=replace(st, stage="done"), state=Outcome.FAILED.value, detail=st.failed
            )
        return replace(run, step=replace(st, stage="done"))
    if st.deadline is not None and now >= st.deadline:
        return replace(
            run,
            step=replace(st, stage="done", unfinal=()),
            state=Outcome.FAILED.value,
            detail=(
                (f"{st.failed}; " if st.failed else "")
                + f"step {st.name!r} not final by its finality timeout"
                + (f"; {read_failure}; the payment's outcome is unknown" if read_failure else "")
            ),
        )
    if queued:
        pending = Pending(queued[0], "advance", Action.ADVANCE)
        st = replace(st, pending=pending, queued=tuple(queued[1:]))
    return replace(run, step=st)


# --- after the steps ------------------------------------------------------------------------


def postcondition(root: KitWorkflowSpec, run: Run, env: Env) -> Run:
    """Rule 2: runs once steps stop, on success or failure; a FAILED run stays FAILED."""
    if not run.executing or run.postcondition is not None:
        return run
    spec = resolve(root, run.spec_path)
    if spec.postcondition is None:
        return run if run.state is not None else replace(run, state=Outcome.SUCCEEDED.value)
    result_ = spec.postcondition(step_context(root, run, env))
    if run.state is not None:
        return replace(run, postcondition=result_)
    state = Outcome.SUCCEEDED if result_.ok else Outcome.POSTCONDITION_FAILED
    return replace(run, postcondition=result_, state=state.value)


def postcondition_guarded(root: KitWorkflowSpec, run: Run, env: Env) -> Run:
    """postcondition with rule 7 applied: a raise fails the run closed, is recorded once and
    is never retried (the failed result marks the postcondition as done)."""
    try:
        return postcondition(root, run, env)
    except Exception as exc:
        run = replace(run, postcondition=PostconditionResult(False, f"raised {_describe(exc)}"))
        return error(run, f"postcondition {_describe(exc)}")


def postcondition_due(run: Run) -> bool:
    return run.executing and run.postcondition is None


def abandon_reason(run: Run) -> str | None:
    """Rule 8: why a run ended without executing any step, if it did."""
    if run.executing:
        return None
    if run.stale:
        return "STALE"
    state = run.state or run.approval
    return state if state in ("REJECTED", "EXPIRED", "BLOCKED") else None


def abandon(root: KitWorkflowSpec, run: Run, env: Env) -> Run:
    """Call on_abandon once before the audit record; a raise is recorded, never retried."""
    reason = abandon_reason(run)
    spec = resolve(root, run.spec_path)
    if reason is None or spec.on_abandon is None:
        return run
    try:
        spec.on_abandon(step_context(root, run, env), reason)
    except Exception as exc:
        return error(run, f"on_abandon {_describe(exc)}")
    return run


# Refused before anything was sent under this request_id: the op is absent from prior.
REFUSAL_ERRORS = frozenset({"NotAuthorised", "TenantScopeViolation"})
# The request_id is already bound to a DIFFERENT body in the operation store; that body may be
# in flight or landed, so the op is recorded as OUTCOME_UNKNOWN, never absent.
REUSE_ERROR = "RequestIdReuse"
_STORED = re.compile(r"stored record status (\S+)")


def _reuse_result(op: Operation, message: str) -> OperationResult:
    found = _STORED.search(message)
    stored = found.group(1) if found and found.group(1) != "None" else None
    return OperationResult(
        request_id=op.request_id,
        status=OperationStatus.OUTCOME_UNKNOWN,
        error_type="OutcomeUnknown",
        error_code="request_id_bound_to_another_body",
        error_message=(
            "the request_id is bound to a different body in the operation store; that write "
            "may be in flight or landed"
        ),
        body={
            "last_error": {  # the one last_error shape, as awx_spine.executor writes it
                "error_type": REUSE_ERROR,
                "error_code": "request_id_bound_to_another_body",
                "http_status": None,
                "error_message": message[:300],
                "stored_status": stored,
            }
        },
    )


def error(run: Run, message: str, *, refused: bool = False, reused: bool = False) -> Run:
    """An unexpected failure in kit code or a task after the proposal exists.

    StepContext.prior holds a result for EVERY operation that was dispatched. So
    if an operation is pending, it goes into the results: its established result when the
    runner had one (a pending COMPLETE), else OUTCOME_UNKNOWN (it may have landed, M1). The
    only exception is `refused=True` on the operation's FIRST dispatch (a SEND, or a sandbox
    ADVANCE, which is never resent): the adapter refused it before anything was sent
    (REFUSAL_ERRORS), so it stays absent. The adapter re-authorises on every mode, so a refusal
    can also arrive on a RESEND or a COMPLETE; that operation was sent
    earlier and may have landed, so it is recorded like any other. `reused=True` (a
    RequestIdReuse) is NOT absent: another body holds the request_id and may have landed, so
    the op is OUTCOME_UNKNOWN with body["last_error"] naming the reuse and the stored status."""
    detail = message[:500] if run.detail is None else f"{run.detail}; {message}"[:500]
    st = run.step
    if st is not None and st.pending is not None and not (refused and _first(st.pending)):
        key = st.name if st.pending.kind == "step" else f"{st.name}:sandbox"
        if reused:
            unknown = _reuse_result(st.pending.op, message)
        else:
            unknown = st.pending.result or _unknown(
                st.pending.op, "run stopped mid-operation; it may have landed"
            )
        results = {**run.results, key: (*run.results.get(key, ()), unknown)}
        run = replace(run, results=results, step=replace(st, pending=None))
    return replace(run, state=Outcome.FAILED.value, detail=detail)


def _first(pending: Pending) -> bool:
    """Nothing went out under this operation before this dispatch."""
    if pending.action is Action.SEND:
        return pending.resends == 0
    return pending.action is Action.ADVANCE  # an advance is never resent; a crash completes it


def audit(run: Run, *, now: datetime) -> tuple[Run, dict[str, Any]]:
    state = run.state or run.approval
    run = replace(run, state=state)
    entry = {
        "proposal_id": run.proposal.proposal_id,
        "parent_proposal_id": run.parent_proposal_id,
        "kit": run.proposal.kit,
        "action": run.proposal.action,
        "spec_path": list(run.spec_path),
        "tenant_id": run.tenant.tenant_id,
        "proposed_by": run.proposal.proposed_by,
        "origin": run.proposal.origin.value,
        "intent_hash": run.proposal.intent_hash,
        "risk_tier": run.proposal.risk_tier.value,
        "approval": run.approval,
        "state": state,
        "abandon_reason": abandon_reason(run),
        "detail": run.detail,
        "child_states": list(run.child_states),
        "lost_children": list(run.lost_children),  # child executions lost, abnormal
        "results": {
            step: [
                {
                    "request_id": r.request_id,
                    "status": r.status.value,
                    "provider_id": r.provider_id,
                    "provider_status": r.provider_status,
                    "error_type": r.error_type,
                    "error_code": r.error_code,
                }
                for r in rs
            ]
            for step, rs in run.results.items()
        },
        "postcondition": run.postcondition,
        "at": now,
    }
    return run, entry


def child_budget_s(spec: KitWorkflowSpec) -> int:
    """A generous upper bound for one run's steps: finality loops plus resend waits."""
    resend_wait = sum(
        max(RESEND_INTERVAL_S * RESEND_BACKOFF**i, LEASE_RESEND_WAIT_S) for i in range(MAX_RESENDS)
    )
    total = 0
    for s in spec.steps:
        total += 600 + resend_wait * 4
        if s.finality is not None:
            total += s.finality.timeout_s + s.finality.interval_s
    if spec.child is not None:
        total += math.ceil(MAX_FAN_OUT / MAP_CONCURRENCY) * child_budget_s(spec.child)
    return total


# --- payload codec --------------------------------------------------------------------------


def to_payload(run: Run) -> dict[str, Any]:
    """Transport-encode a run for a state payload, with the derived fields Choice states read."""
    data = encode(run)
    data["postcondition_due"] = postcondition_due(run)
    return data


def _pending_from(data: Mapping[str, Any] | None) -> Pending | None:
    if not data:
        return None
    return Pending(
        op=codec.operation_from(data["op"]),
        kind=data["kind"],
        action=Action(data["action"]),
        resends=int(data.get("resends") or 0),
        wait_s=int(data.get("wait_s") or 0),
        result=codec.result_from(data["result"]) if data.get("result") else None,
    )


def _step_from(data: Mapping[str, Any] | None) -> StepState | None:
    if not data:
        return None
    return StepState(
        name=data["name"],
        ops=tuple(codec.operation_from(o) for o in data["ops"]),
        index=int(data["index"]),
        stage=data["stage"],
        pending=_pending_from(data.get("pending")),
        unfinal=tuple(int(i) for i in data.get("unfinal") or ()),
        deadline=data.get("deadline"),
        advances=tuple(int(i) for i in data.get("advances") or ()),
        queued=tuple(codec.operation_from(o) for o in data.get("queued") or ()),
        wait_s=int(data.get("wait_s") or 0),
        failed=data.get("failed"),
    )


def from_payload(data: Mapping[str, Any]) -> Run:
    """Rebuild a run from a decoded payload (awx_common.transport.decode already applied)."""
    return Run(
        spec_path=tuple(data["spec_path"]),
        proposal=codec.proposal_from(data["proposal"]),
        decision=codec.policy_decision_from(data["decision"]),
        tenant=codec.tenant_from(data["tenant"]),
        raw_input=dict(data["raw_input"]),
        approval=data["approval"],
        results={
            step: tuple(codec.result_from(r) for r in rs)
            for step, rs in (data.get("results") or {}).items()
        },
        state=data.get("state"),
        detail=data.get("detail"),
        executing=bool(data.get("executing")),
        stale=bool(data.get("stale")),
        step=_step_from(data.get("step")),
        children=tuple(data.get("children") or ()),
        child_states=tuple(data.get("child_states") or ()),
        lost_children=tuple(str(p) for p in data.get("lost_children") or ()),
        postcondition=codec.postcondition_from(data.get("postcondition")),
        parent_proposal_id=data.get("parent_proposal_id"),
        root_proposal_id=data.get("root_proposal_id"),
        root_action=data.get("root_action"),
    )
