# Workflow rules

A kit describes each action as a `KitWorkflowSpec` (`awx_common.workflow`): validate, policy,
steps, finality, postcondition. The spine runs it. The `awx_common.workflow` module docstring
is the definition of every rule on this page. This page explains each in plain language and
names the code that enforces it.

Runner rules 0 to 11 bind the spine: the workflow engine (`awx_spine_aws.engine`), which both
the Step Functions state machine and `awx_spine_aws.local_runner` run, and the mutation
executor (`awx_spine.executor`). Kit rules M1 to M4 bind every kit's own code.

The order of one run is:

```text
validate -> policy -> approval (by risk tier) -> steps in order, each step's operations
through the mutation adapter then its finality poll -> postcondition -> audit
```

Tests for the runner rules live in `packages/awx_common/tests`, `packages/awx_spine/tests` and
`packages/awx_spine_aws/tests`. `packages/awx_spine_aws/tests/test_asl_equivalence.py` checks
that the state machine and `local_runner` agree. Each kit tests its own M1 to M4 handling.

## Rule 0: pure callables

`validate`, `policy`, `fan_out` and `child_tenant` may read but never write the store or emit
operations, because they run more than once: the propose tool previews the policy and the
workflow re-runs it. A step's build, the postcondition and `on_abandon` may write, and must be
idempotent because a runner may call them again after a crash.

Enforced by `engine.policy_context(..., pure=True)`, which hands pure callables a read-only
store that raises `PurityViolation` on any write.

## Rule 1: recheck before the first step

The execution re-runs validate and policy before the first step. If either raises, or the
proposal hash (kit, action, intent, computed values, risk tier and origin) differs from the
approved proposal's,
the run fails closed and `on_abandon` runs with reason `STALE`.

Enforced by `engine.recheck`.

## Rule 2: the postcondition always runs

Once the steps stop, the postcondition runs: after success, and also after an operation comes
back `FAILED` or `OUTCOME_UNKNOWN`, a finality poll fails or times out, or a build raises.
`StepContext.prior` holds the results so far, so a kit can record arrears, receivables or a
released lock. The run's outcome stays `FAILED` if any operation failed.

Enforced by `engine.postcondition` and the state machine's AfterStop and Postcondition states.

## Rule 3: fan-out children

A spec with `fan_out` runs its `child` spec once per child intent (kit 6 runs one child per
employer). The parent runs as a tenant with no connected account (the platform, or a kit
operator such as `k6:operator`), and its approval covers every child, so the parent's
computed values must list the child intents. `child_tenant` names each child's
tenant. The runner resolves it through the `TenantResolver` and refuses an unknown tenant.
A child's policy may return only AUTO (run it) or BLOCKED (skip it and record why). Any other
tier fails that child closed.

Enforced by `engine.fan_out` and `engine.children_done`, and the state machine's Map state,
which starts one `awx-<kit>-<action>-child` execution per child.

## Rule 4: tenant scope before every operation

Runners never bind write operations to tenants. Kit step code binds every connected-account
operation with `bind_operation` or `bind_platform_operation`, from `ctx.policy.tenant`. Before
executing any operation the runner calls `tenancy.check_tenant_scope(op, tenant)`, which
denies by default. A violation fails the run closed with nothing sent.

Enforced by `engine.build` (through `awx_common.tenancy.check_tenant_scope`), and again by the
adapter Lambda against the tenant recorded on the proposal.

## Rule 5: read routing

Finality and reconcile reads inherit the operation's `on_behalf_of`. A read with no
`on_behalf_of` in a context whose tenant has a connected account is a platform read and must
match `platform_read_allowlist`; every other read must match `read_allowlist`.
`ctx.read_tenant` reads must match `read_allowlist`.

Enforced by `engine.op_read` and `engine.policy_context`, and again by the kit's reader Lambda
(`awx_spine_aws.handlers.reader`), which derives the read mode itself.

## Rule 6: a fresh context per call

Each step's build, the postcondition and `on_abandon` get a fresh `PolicyContext` whose `now`
is the time of that call.

Enforced by `engine.step_context`.

## Rule 7: kit exceptions fail closed

An exception from validate, policy, `fan_out`, `child_tenant`, a build or the postcondition
fails the run closed with no retry, and its type and message go in the audit record.

Enforced by `engine.error` and the kit-step Lambda (`awx_spine_aws.handlers.kit_step`).

## Rule 8: on_abandon

`on_abandon(ctx, reason)` runs when the workflow ends without executing any step, with reason
`REJECTED`, `EXPIRED`, `BLOCKED` or `STALE`, so a kit can release a hold or a queue place.

Enforced by `engine.abandon`, once, just before the audit record.

## Rule 9: reconcile before any resend

After an `OUTCOME_UNKNOWN` (a timeout, a dropped connection, a 5xx, or the adapter Lambda
itself timing out or crashing after the call left), the executor reconciles before any resend:
`StepSpec.reconcile` when the kit provides it, else the generic read-back for the endpoint. It
resends the same operation only when reconcile shows the write did not land and the endpoint
deduplicates on `request_id`. For an endpoint that ignores `request_id`, or when reconcile
raises `ReconcileUndetermined`, it stops at `OUTCOME_UNKNOWN` and rule 2 runs. It never mints a
new `request_id`.

Once any send may have landed without a known result, the operation stays uncertain
(`OperationRecord.may_have_landed`, stored and never cleared): every later run reconciles
before any send, and a refused resend, or one that ends in a 429 or a not-sent failure, is
`OUTCOME_UNKNOWN`, never `FAILED`. Only a read-back that finds the matching object or a
mismatch, or a person, resolves it. Two objects under one `request_id` are
`OUTCOME_UNKNOWN` naming both.

A resend is allowed only while the first attempt is younger than `DEDUP_RESEND_MARGIN` (6 days,
inside the sandbox's deduplication window of about 7 days), and not while another attempt
still holds the attempt lease.

Enforced by `awx_spine.executor.MutationExecutor` with the endpoint table in
`awx_spine.endpoints` (`dedupes`, `readback`, `match_fields`; unknown endpoints count as not
deduplicating), and by `engine.crashed`, `engine.result` and the state machine's Crashed and
ResendWait states.

## Rule 10: sandbox mode

In sandbox mode, the only mode this repository deploys, a non-terminal finality poll calls
`FinalitySpec.sandbox_advance`, which returns a simulation operation that moves the sandbox
object one step. The runner sends it through the adapter with the same scope check and audit
as any operation, but never resends it: a failure is recorded and the next poll decides. It
stops after `max_sandbox_advances`. Outside sandbox mode `sandbox_advance` is never called.

Enforced by `engine.finality_check`, switched on by the kit-step Lambda's `AWX_SANDBOX_MODE`
setting. `awx_common.sandbox.make_transfer_advance` is a ready-made advance for transfers.

## Rule 11: proposal origin and the agent approval floor

Every proposal records its origin, set server-side: `AGENT` for a `propose_*` tool call,
`SCHEDULE` for a scheduled trigger, `OPERATOR` for the command line's direct path. Its tier is
`approvals.effective_tier(policy tier, origin, spec.agent_min_tier)`, both when the proposal is
made and in rule 1's re-run. The floor applies only to origin `AGENT`: a proposal the model
starts is raised to at least SINGLE (one approver who is not the proposer), whatever an
injected document says, unless the action's spec sets `agent_min_tier=AUTO`. A kit sets AUTO
only with a justification in its own README or DESIGN.md. These actions set it:

| Kit | Action | Effect at AUTO |
|---|---|---|
| 1 | `lock_rate` | create a quote |
| 1 | `execute_plan` | convert and pay, within the policy's autonomous limits (the only AUTO action that can move money) |
| 2 | `update_card_controls`, `freeze_card` | tighten an existing card only (any raise gets SINGLE or DUAL from the policy) |
| 3 | `close_incident` | record a closure |
| 4 | `refer_dispute`, `close_dispute` | record a decision |
| 5 | `register_shortfall`, `reconcile_bridge` | write the kit's ledger only |
| 6 | `lock_rates` | create quotes |
| 7 | `reconcile_portfolio` | read back and report |
| 8 | `reconcile_seller_money` | read back and report |

Every other action keeps the default floor, so nothing else the model starts executes without
a human approval.

Proposal ids come from `approvals.derive_proposal_id` and are the execution name, so a model
retry inside one approval window cannot start a second run. The id covers the intent and the
computed values, so a new action whose computed values differ gets its own id. The floor
applies to proposals only: a fan-out child is not a proposal and keeps its own policy tier.

Enforced by `engine.propose`, `engine.start` and `engine.recheck`.

## M1: unknown outcomes keep the money in flight

Money whose operation ended `OUTCOME_UNKNOWN`, or whose finality poll failed or timed out,
stays in flight on that operation's `request_id`. The postcondition keeps every lock, hold and
claim for it and records it as in flight, never as failed, payable, released or free. A later
run reconciles that `request_id` first and may re-emit the same operation inside the
deduplication window. Only a definitive outcome moves the money: landed, or failed (`FAILED` at
create, or the provider's own failure state, such as a `CANCELLED` transfer with its failure
block). An empty read-back is not a failure. A new `request_id` for the same money comes only
after a definitive failure. Past the window, or for an endpoint that ignores `request_id`, the
kit routes it to a person and sends nothing.

## M2: request ids carry the kit's book id

A `request_id` built from any kit-local value (anything that is not an Airwallex object id,
such as a seeded obligation, loan, employee or cycle id, or a local counter) includes the
kit's book id: a random id minted once, when the kit's state for that tenant or platform is
first created, and stored with that state. A fresh book seeded like an old one produces
disjoint request ids, so a re-run on the shared sandbox can never adopt an earlier run's
objects.

## M3: the approval bounds every outgoing amount

Every amount that leaves an account, including the sell side of an FX (foreign exchange)
conversion, is bound by the approval: the computed values carry the exact amount, or a maximum
that the build checks against the live quote before sending, failing closed above it.

## M4: holds belong to the run that took them

Every hold, lock and claim records as its owner the `proposal_id` of the run that took it
(`StepContext.proposal_id`), never the proposal hash (`intent_hash`), which identical proposals
share across approval windows. Only its owner releases it (its postcondition), or a later run
that has read a definitive outcome for the money it covers (M1). An abandoned proposal never
ran a build, so its `on_abandon` releases nothing it did not take itself.
