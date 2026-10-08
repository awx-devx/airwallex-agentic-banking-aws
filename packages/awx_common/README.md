# awx_common

The pure contract every package shares: exact money, normalised errors, the Operation that
describes an Airwallex write, proposals and approvals, tenant binding, tool specs and the
`KitWorkflowSpec` description of a kit action. It uses the Python standard library only and
does no input or output. Kit packages (`kits/k<N>_<slug>`) depend on `awx_common` alone;
`awx_spine` (the Airwallex layer), `awx_spine_aws` (the AWS runtime), `awx_agent` (the agent
plane) and `infra` (the AWS Cloud Development Kit app) build on it. The module docstring of
`awx_common.workflow` defines runner rules 0 to 11 and kit money rules M1 to M4, which the
rest of the codebase cites by number (see also
[docs/workflow-rules.md](../../docs/workflow-rules.md)).

## Modules

| Module | What it holds |
|---|---|
| `money` | `Money`: a Decimal at the currency's minor-unit scale, never a float; explicit rounding, same-currency arithmetic, `allocate` that always sums exactly |
| `errors` | `AwxError` and its subclasses, each Airwallex failure mapped to one class with `retryable` and `outcome_unknown` flags; `TenantScopeViolation` |
| `intent` | canonical JSON, `intent_hash`, `derive_request_id(kit, business_key, step)` |
| `transport` | lossless JSON for Step Functions, Lambda and DynamoDB payloads (tagged `$decimal`, `$money`, `$datetime`); refuses floats both ways |
| `ops` | `Operation` (one write), `OperationResult`, `ReadRequest`, the `AirwallexPort` protocol, `DEDUP_RESEND_MARGIN` |
| `approvals` | `RiskTier`, `Origin`, `Proposal`, `ApprovalDecision`, `approval_state`, `effective_tier`, `derive_proposal_id`, `proposal_hash` |
| `tenancy` | `TenantContext`, tenants-table row rules, resolvers, `bind_operation`, `bind_platform_operation`, `bind_read`, `check_tenant_scope` |
| `workflow` | `KitWorkflowSpec`, `StepSpec`, `FinalitySpec`, `PolicyContext`, `StepContext`, `ApproverScope`; the runner and kit rules |
| `spec_rules` | the structural checks on a workflow spec, shared by `KitWorkflowSpec` and the CDK manifest parser (`infra.kit_manifest`) |
| `store` | the `KitStore` port (optimistic versions, prefix query) and `InMemoryKitStore` |
| `tools` | `ToolSpec` and `ToolKind` (READ, PROPOSE); `SERVER_OWNED_FIELDS` no tool input may carry |
| `functions` | `KitFunctionSpec` and the closed `KitGrant` menu for Lambdas a kit declares as data |
| `operator_commands` | `OperatorCommand` and `run_operator_command`, the shipped entry point for state that is not model input |
| `evidence` | `EvidenceRef`, `EvidenceStore`, `load_verified` for `files.upload` |
| `sandbox` | `make_transfer_advance`: drives sandbox transfers to a final status (rule 10) |
| `kits` | `KITS` (kit ids and guide names) and `KIT_DIRS` |

## Key behaviours

Amounts are exact. `Money.of` refuses floats, booleans, unknown currencies and extra decimal
places unless the caller passes a rounding mode; `transport` keeps Decimal and Money exact
through services that read JSON numbers as doubles.

An approval binds to what the approver saw. `proposal_hash` covers kit, action, intent, the
computed amounts, the risk tier and the origin. `approval_state` re-verifies the hash, refuses
a decision for another proposal or hash, a decision by the proposer or one outside the
validity window, and needs two distinct approvers for `DUAL_APPROVER`.

A model-started proposal never falls below its approval floor. Every proposal gets its policy
tier, and `effective_tier` raises an `AGENT` proposal to at least the spec's `agent_min_tier`
(rule 11). The default is `SINGLE_APPROVER`: one approver who is not the proposer. A spec may
set `agent_min_tier=AUTO` only with a justification in its kit's README or DESIGN.md; kit 1's
`execute_plan` is the only AUTO action that can move money, within the policy's autonomous
limits.
[Rule 11](../../docs/workflow-rules.md#rule-11-proposal-origin-and-the-agent-approval-floor)
lists every action that sets AUTO.

Proposal ids are deterministic. `derive_proposal_id` hashes intent, computed, tenant, proposer
and the approval window, and is the Step Functions execution name: a model retry reaches the
existing run, while a new action whose computed values differ gets its own id.

Request ids derive from business objects. `derive_request_id` gives the same business action
the same request id every time, so Airwallex deduplicates a resend. `DEDUP_RESEND_MARGIN`
(6 days, inside the provider's window of about 7) bounds when a resend may reuse one.

Tenancy denies by default. Kit step code binds every connected-account write with
`bind_operation` (the `x-on-behalf-of` header) or `bind_platform_operation` (a body field from
`PLATFORM_BODY_FIELDS`); `check_tenant_scope` refuses anything else before it is sent (rule 4).
`tenant_from_row` gives every tenants-table row one kit and one role, and refuses a malformed
`acct_...` id rather than reading it as the platform's own account.

A workflow spec is checked when it is built. `KitWorkflowSpec.__post_init__` and
`spec_rules.check_workflow_shape` enforce snake_case actions, unique step names, anchored
`^/api/v1/...$` read allowlists, `fan_out` with `child`, and positive approval lifetimes.
`approver_tenant` refuses a kit 5 to 8 root spec that declares no `ApproverScope`, so no
proposal is stored for it.

Operator commands stay out of the agent's reach. A command name may not look like a tool or
repeat a tool, action or function name, a `once` command refuses to reseed without
`replace: true`, and a lost write race becomes an `OperatorCommandError`.

Evidence travels by reference. A `files.upload` body carries an `EvidenceRef`; `load_verified`
releases the bytes only when size and SHA-256 match.

## Tests

| Topic | Files |
|---|---|
| Money, canonical JSON, transport | `test_money.py`, `test_intent_transport.py` |
| Operations, errors, approvals, tool specs | `test_contract.py` |
| Proposal hash and origin, approval floor, proposal ids | `test_proposal_hash.py`, `test_proposal_origin.py` |
| Tenant binding and scope check | `test_tenant_scope.py` |
| Workflow spec shape and hooks | `test_workflow.py` |
| Kit store | `test_store.py` |
| Operator commands | `test_operator_commands.py` |
| Evidence references | `test_evidence.py` |

Every test is offline and unmarked. Run them from the repository root:

```
uv run --all-packages pytest packages/awx_common/tests
```

See [docs/testing.md](../../docs/testing.md) for the markers the other packages use.
