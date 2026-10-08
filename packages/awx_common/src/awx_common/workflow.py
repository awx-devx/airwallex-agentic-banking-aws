"""The pure description of a kit action. Kits export these; the spine runs them.

Each kit module `awx_k<N>_<slug>.workflow` exports SPECS: dict[str, KitWorkflowSpec] keyed by
action. The propose tool for an action is named propose_<action>. The KitWorkflow construct in
awx_spine_aws (Step Functions) and local_runner (in-process) execute the same spec in the same
order:

  validate -> policy -> approval (by risk tier) -> steps in order, each step's operations
  through the mutation adapter then its finality poll -> postcondition -> audit

Runtime rules every runner follows:

0. validate, policy, fan_out and child_tenant are pure. They may read (ctx.read,
   ctx.read_platform, ctx.read_tenant, ctx.store get and query) but never write the store or
   emit operations, because they run more than once: the propose tool previews the policy and
   the workflow re-runs it. A step's build, the postcondition and on_abandon may write the
   store, and must be idempotent because a runner may call them again after a crash.
1. The execution re-runs validate and policy before the first step. If either raises, or the
   resulting proposal hash (intent, computed amounts and risk tier) differs from the approved
   proposal's, the run fails closed and on_abandon runs with reason "STALE".
2. The postcondition always runs once the steps stop: after success, and also after an
   operation comes back FAILED or OUTCOME_UNKNOWN, a finality poll fails or times out, or a
   step's build raises. StepContext.prior then holds the results so far, so kits can record
   arrears, receivables or a released lock. The run's outcome stays FAILED if any operation
   failed.
3. A spec with fan_out runs `child` once per child intent (kit 6: one run per employer). The
   parent runs as the platform (a tenant with no connected account). The parent's approval
   covers every child, so the parent's computed must list the child intents. child_tenant
   names each child's tenant_id; the runner resolves it through the TenantResolver, which
   refuses unknown tenants. A child's policy may return only AUTO (run it) or BLOCKED (skip it
   and record why); any other tier fails that child closed.
4. Runners never bind write operations to tenants. Kit step code binds every connected-account
   operation with bind_operation or bind_platform_operation, from ctx.policy.tenant. Before
   executing any operation, the runner calls tenancy.check_tenant_scope(op, tenant), which
   denies by default, and a violation fails the run closed (rule 7) with nothing sent.
5. Finality and reconcile reads inherit the operation's on_behalf_of (a read_request's own
   value must be None or equal). A read with no on_behalf_of in a context whose tenant has a
   connected account is a platform read and must match platform_read_allowlist; every other
   read must match read_allowlist. ctx.read_tenant reads must match read_allowlist.
6. Each step's build, the postcondition and on_abandon get a fresh PolicyContext whose `now`
   is the time of that call.
7. An exception from validate, policy, fan_out, child_tenant, a build or the postcondition
   fails the run closed with no retry, and its type and message go in the audit record.
8. on_abandon(ctx, reason) runs when the workflow ends without executing any step, with reason
   "REJECTED", "EXPIRED", "BLOCKED" or "STALE", so a kit can release a hold or a queue place.
9. After an OUTCOME_UNKNOWN (timeout, dropped connection, 5xx, or the adapter Lambda itself
   timing out or crashing after the call left), the executor reconciles BEFORE any resend:
   StepSpec.reconcile when present, else the generic read-back. It resends the same
   Operation only when reconcile shows the write did not land AND the endpoint deduplicates
   on request_id (awx_spine's endpoint table; unknown endpoints count as not deduplicating).
   For an endpoint that ignores request_id (simulation/issuing/create, fx/quotes/create,
   cardholder create, simulation transitions), or when reconcile raises
   ReconcileUndetermined, it stops at OUTCOME_UNKNOWN and rule 2 runs. It never mints a
   new request_id. Once any send may have landed without a known result, the operation
   stays uncertain (OperationRecord.may_have_landed, stored and never cleared): every later
   run reconciles before any send, and a refused resend, or one that ends in a 429 or a
   not-sent failure, is OUTCOME_UNKNOWN carrying the last error, never a FAILED. Only a
   found, matching object, a mismatch or a person resolves it. Two objects under one
   request_id are OUTCOME_UNKNOWN naming both.
10. Sandbox mode (deployment flag; the only mode this proof of concept deploys): on a
   non-terminal finality poll the runner calls FinalitySpec.sandbox_advance and executes the
   simulation Operation it returns through the adapter, with the same scope check and audit
   as any operation, but with no resend: a failure is recorded and the next poll decides. It
   stops after max_sandbox_advances. Outside sandbox mode sandbox_advance is never called.
11. Every Proposal records its origin, set server-side: AGENT for a propose_* tool call,
   SCHEDULE for a scheduler, OPERATOR for the CLI's direct path. Its tier is
   approvals.effective_tier(policy tier, origin, spec.agent_min_tier), both when the proposal
   is made and in rule 1's re-run. The default floor is SINGLE, so nothing the model starts
   executes without a human approval, whatever an injected document says. Proposal IDs come
   from approvals.derive_proposal_id and are the execution name, so a model retry inside one
   approval window cannot start a second run. The ID covers intent and computed, so a new
   action whose computed differs (the next card change number) gets its own ID. The floor
   applies to proposals only: a fan_out child is not a proposal, runs under its parent's
   approval, and keeps its own policy tier (rule 3), whatever the parent's origin.

Every kit follows these money rules:

M1. Money whose operation ended OUTCOME_UNKNOWN, or whose finality poll failed or timed out,
   stays in flight on that operation's request_id. The postcondition keeps every lock, hold
   and claim for it and records it as in flight, never as failed, payable, released or free.
   A later run reconciles that request_id first and may re-emit the same Operation (same
   request_id and body) inside the endpoint's deduplication window, about 7 days; runners
   treat that as rule 9, never as a new write and never as RequestIdReuse. Only a definitive
   outcome moves the money: landed (nothing more is owed), or failed (FAILED at create, or the
   provider's own failure state, such as a CANCELLED transfer with its failure block). An
   empty read-back is not a failure. A new request_id for the same money (a new attempt, a
   later cycle, a re-proposal) comes only after a definitive failure. Past the window, or for
   an endpoint that ignores request_id, the kit routes it to a person and sends nothing.
M2. A request_id built from any kit-local value (anything that is not an Airwallex object id,
   such as a seeded obligation, loan, employee or cycle id, or a local counter) includes the
   kit's book id: a random id minted once, when the kit's state for that tenant or platform
   is first created, and stored with that state; never a constant or a default. A fresh book
   seeded like an old one must produce disjoint request_ids, so a re-run on the shared
   sandbox can never adopt an earlier run's objects (the sandbox deduplicates for ~7 days).
M3. Every amount that leaves an account, including the sell side of an FX conversion, is
   bound by the approval: computed carries the exact amount, or a maximum that the build
   checks against the live quote before sending, failing closed above it.
M4. Every hold, lock and claim records as its owner the proposal_id of the run that took it
   (StepContext.proposal_id), never intent_hash, which identical proposals share across
   windows. Only its owner releases it (its postcondition), or a later run that has read a
   definitive outcome for the money it covers (M1). An abandoned proposal never ran a build
   (rule 8), so its on_abandon releases nothing it did not take itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .approvals import RiskTier
from .kits import KITS
from .ops import Operation, OperationResult, ReadRequest
from .spec_rules import check_finality_bounds, check_workflow_shape
from .store import KitStore
from .tenancy import TenantContext

_ACTION = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
ABANDON_REASONS = frozenset({"REJECTED", "EXPIRED", "BLOCKED", "STALE"})


@dataclass(frozen=True)
class PolicyContext:
    """What policy, step and read code may see.

    read           tenant-bound Airwallex GET, limited to the spec's read_allowlist
    read_platform  platform-account GET with no x-on-behalf-of, limited to the spec's
                   platform_read_allowlist; None when the spec declares none
    read_tenant    read_tenant(tenant_id, req): a GET inside another tenant's connected
                   account, resolved server-side through the TenantResolver and limited to
                   read_allowlist. Set only for a platform context (tenant has no connected
                   account) in a spec with child_tenant; None otherwise
    store          this kit's own state for this tenant; None only in pure unit tests
    platform_store this kit's own platform-wide state (tenant "platform"), for state shared by
                   every tenant such as a capital pool; same rules as store; never a
                   process-global the kit installs itself
    proposal_id    read-only: the current run's proposal ID (its execution name), so a kit can
                   tell its own claim from another run's; None while a proposal is being
                   created (propose's own policy call), when it does not exist yet
    run_status     run_status(proposal_id, action): the ROOT run of this kit's `action` for
                   that proposal. RUN_RUNNING while it may still send anything; a member of
                   RUN_TERMINAL only once it can send nothing more; None
                   when there is no such root run. Raises ValueError for an action that is not
                   one of this kit's, a malformed ID, or a child proposal's ID. Read-only. The
                   callable itself is None where no run status is available (the tool plane at
                   propose time, pure unit tests): decide at recheck, never read it as terminal
    """

    tenant: TenantContext
    now: datetime
    read: Callable[[ReadRequest], Mapping[str, Any]]
    store: KitStore | None = None
    read_platform: Callable[[ReadRequest], Mapping[str, Any]] | None = None
    read_tenant: Callable[[str, ReadRequest], Mapping[str, Any]] | None = None
    platform_store: KitStore | None = None
    proposal_id: str | None = None
    run_status: Callable[[str, str], str | None] | None = None


# Statuses run_status reports, the Step Functions names. Anything that is not in
# RUN_TERMINAL (PENDING_REDRIVE included) reads RUN_RUNNING; a terminal status is reported only
# once the run can send nothing more.
RUN_RUNNING = "RUNNING"
RUN_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})


@dataclass(frozen=True)
class PolicyDecision:
    risk_tier: RiskTier
    reasons: tuple[str, ...]
    computed: Mapping[str, Any] = field(default_factory=dict)  # amounts the code chose


class ReconcileUndetermined(Exception):
    """Raised by StepSpec.reconcile when it cannot tell whether the operation landed.

    The executor then stops at OUTCOME_UNKNOWN and never resends (rule 9).
    """


SandboxAdvance = Callable[
    [Operation, OperationResult, Mapping[str, Any], datetime, int], Operation | None
]


@dataclass(frozen=True)
class FinalitySpec:
    """How to read one operation's object until its provider status is terminal.

    sandbox_advance(op, result, body, now, attempt) is for the Airwallex sandbox only, where
    created objects do not move without a simulation call (awx_spine FAKE-FIDELITY.md). On each
    non-terminal poll in sandbox mode the runner calls it with the latest read body and an
    attempt counter from 1; it returns a simulation Operation that moves the object one step,
    or None to wait (rule 10).

    select(body, result) (optional) picks this operation's object out of the read body
    before status_of sees it, for a finality that reads a list (?request_id= or a filtered
    list) and must choose its row by the create reply's provider_id. None means the object is
    not visible yet: a non-terminal poll, retried within the deadline. Without select,
    status_of gets the read body itself.
    """

    read_request: Callable[[OperationResult], ReadRequest]
    status_of: Callable[[Mapping[str, Any]], str]
    terminal: frozenset[str]
    success: frozenset[str]
    interval_s: int = 5
    timeout_s: int = 300
    sandbox_advance: SandboxAdvance | None = None
    max_sandbox_advances: int = 6
    select: Callable[[Mapping[str, Any], OperationResult], Mapping[str, Any] | None] | None = None

    def __post_init__(self) -> None:
        if not self.success <= self.terminal:
            raise ValueError("success statuses must be terminal statuses")
        check_finality_bounds(self.interval_s, self.timeout_s)
        if self.max_sandbox_advances < 0:
            raise ValueError("max_sandbox_advances must not be negative")


class ApproverScope(Enum):
    """Whose approvers decide a proposal (a closed set).

    OWN_TENANT: the proposal's own tenant (a tenant's own money: the platform for kits 1-4, a
    borrower's repayment, an employer's payroll). KIT_OPERATOR: the kit's operator tenant
    `<kit>:operator`, for money that leaves a platform, lender or marketplace balance toward a
    tenant (an advance, a bridge, a seller payout) or that changes what a tenant is owed."""

    OWN_TENANT = "OWN_TENANT"
    KIT_OPERATOR = "KIT_OPERATOR"


def operator_tenant(kit: str) -> str:
    return f"{kit}:operator"


def approver_tenant(spec: KitWorkflowSpec, tenant_id: str, *, is_child: bool = False) -> str:
    """The tenant whose approvers may list, read and decide this proposal. Fails closed: a kit
    5-8 root spec with no declared scope raises, so no proposal is stored for it."""
    from .tenancy import CONNECTED_ACCOUNT_KITS

    scope = spec.approver
    if scope is None:
        if spec.kit in CONNECTED_ACCOUNT_KITS and not is_child:
            raise ValueError(f"{spec.kit}.{spec.action} declares no approver scope")
        scope = ApproverScope.OWN_TENANT
    return operator_tenant(spec.kit) if scope is ApproverScope.KIT_OPERATOR else tenant_id


@dataclass(frozen=True)
class StepContext:
    intent: Mapping[str, Any]
    decision: PolicyDecision
    policy: PolicyContext
    intent_hash: str  # proposal_hash: shared by identical proposals, so never an owner (M4)
    prior: Mapping[str, Sequence[OperationResult]]  # results of earlier steps, by step name
    proposal_id: str  # this run's proposal ID (the execution name): the owner of its holds
    # The ROOT run of this run's fan-out tree, the run a person's tool
    # guards with run_status(root_proposal_id, root_action). A root run's own; a child's root.
    # Record them in any claim a person's tool will release. None only on a hand-built context.
    root_proposal_id: str | None = None
    root_action: str | None = None


Reconcile = Callable[
    [Operation, Callable[[ReadRequest], Mapping[str, Any]]], Mapping[str, Any] | None
]


@dataclass(frozen=True)
class StepSpec:
    """One step: build its operations, send them through the adapter, then poll finality.

    reconcile(op, read) finds the provider object an operation created, for read-back after
    OUTCOME_UNKNOWN or DuplicateRequest, without ever minting a new request_id. It returns the
    object, or None when the object does not exist. `read` already carries the operation's
    on_behalf_of (rule 5). Without a reconcile hook the executor uses awx_spine's generic
    read-back for the endpoint.
    """

    name: str
    build: Callable[[StepContext], Sequence[Operation]]
    finality: FinalitySpec | None = None
    reconcile: Reconcile | None = None


@dataclass(frozen=True)
class PostconditionResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class KitWorkflowSpec:
    kit: str
    action: str
    validate: Callable[[Mapping[str, Any]], Mapping[str, Any]]  # tool input -> canonical intent
    policy: Callable[[Mapping[str, Any], PolicyContext], PolicyDecision]
    steps: tuple[StepSpec, ...]
    approval_ttl: timedelta
    read_allowlist: tuple[str, ...]  # regexes of GET paths this kit's code may read
    postcondition: Callable[[StepContext], PostconditionResult] | None = None
    fan_out: Callable[[Mapping[str, Any], PolicyDecision], Sequence[Mapping[str, Any]]] | None = (
        None
    )
    child: KitWorkflowSpec | None = None
    platform_read_allowlist: tuple[str, ...] = ()  # platform-scope GETs, e.g. reconciliation
    on_abandon: Callable[[StepContext, str], None] | None = (
        None  # rule 8; reason in ABANDON_REASONS
    )
    child_tenant: Callable[[Mapping[str, Any]], str] | None = None  # rule 3; child intent -> tenant
    agent_min_tier: RiskTier = RiskTier.SINGLE  # rule 11; AUTO only with a README justification
    # Who may approve. None = undeclared: kits 1-4 approve in the proposal's own tenant
    # (the platform); kits 5-8 must declare it on every root spec, or the proposal is refused.
    approver: ApproverScope | None = None

    def __post_init__(self) -> None:
        if self.kit not in KITS:
            raise ValueError(f"unknown kit {self.kit!r}")
        if self.agent_min_tier is RiskTier.BLOCKED:
            raise ValueError("agent_min_tier cannot be BLOCKED")
        # One validator, shared with the infra parent's manifest check (awx_common.spec_rules).
        check_workflow_shape(
            action=self.action,
            step_names=[s.name for s in self.steps],
            has_fan_out=self.fan_out is not None,
            has_child=self.child is not None,
            has_child_tenant=self.child_tenant is not None,
            approval_ttl_s=self.approval_ttl.total_seconds(),
            read_allowlist=self.read_allowlist,
            platform_read_allowlist=self.platform_read_allowlist,
        )

    @property
    def tool_name(self) -> str:
        return f"propose_{self.action}"

    def allows_read(self, path: str) -> bool:
        return any(re.match(p, path) for p in self.read_allowlist)

    def allows_platform_read(self, path: str) -> bool:
        return any(re.match(p, path) for p in self.platform_read_allowlist)
