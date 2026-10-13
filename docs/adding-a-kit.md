# Adding a kit

A kit is one Python package of pure domain code. It writes no CDK, no Lambda handler and no
Step Functions definition: it exports data that the spine, the CDK app and the agent plane
read. This page walks through adding a new kit as `kits/k9_<slug>/`. The id `k9` is an
example, not the guide's kit 9.

## Package layout

```text
kits/k9_<slug>/
  pyproject.toml            name awx-k9-<slug>, depends on awx-common
  README.md                 the kit overview (copy the headings of an existing kit README)
  src/awx_k9_<slug>/
    __init__.py             re-exports SPECS, TOOLS, READS (and OPERATOR_COMMANDS if any)
    workflow.py             SPECS: dict[str, KitWorkflowSpec], keyed by action
    tools.py                TOOLS: tuple of awx_common.ToolSpec
    reads.py                READS: read tool name -> function(args, ctx) -> Mapping
    functions.py            optional: FUNCTIONS, Lambdas the kit needs of its own
    ...                     policy, operation builders, finality, ledger, as the kit needs
  tests/
```

The package name must be `awx_` followed by the directory name, because the CDK app and the
Lambda runtime derive it from the directory (`infra.kit_manifest.kit_package`). Copy
`pyproject.toml` from an existing kit and change the name, description and package path. The
root `pyproject.toml` already includes `kits/*` in the uv workspace, so `make sync` picks the
new package up.

## Kit exports
| Export | Module | Contents |
|---|---|---|
| `SPECS` | `workflow.py` | one `awx_common.workflow.KitWorkflowSpec` per action: `validate`, `policy`, `steps` (each a `StepSpec` with `build`, optional `FinalitySpec` and optional `reconcile`), `approval_ttl`, `read_allowlist`, and optionally `postcondition`, `on_abandon`, `fan_out` with `child` and `child_tenant`, `platform_read_allowlist`, `agent_min_tier` and `approver` |
| `TOOLS` | `tools.py` | `ToolSpec(name, kind, description, input_schema)`. Every `propose_<action>` tool names a `SPECS` action; every `read_*` tool has a `READS` entry. Input schemas set `additionalProperties: false` and may not carry a server-owned field (`awx_common.tools.SERVER_OWNED_FIELDS`) |
| `READS` | `reads.py` | the function behind each read tool, called with the tool arguments and a read-only `PolicyContext` |
| `FUNCTIONS` | `functions.py` (optional) | `awx_common.functions.KitFunctionSpec` entries. `KitStack` builds each Lambda with its own role and only the grants it names from the closed `KitGrant` menu (`EVIDENCE_PUT`, `KIT_STORE`, `OWN_READER`). Kit 4's evidence upload is the example |
| `OPERATOR_COMMANDS` | the package root (optional) | `awx_common.operator_commands.OperatorCommand` entries: seeds, configuration and data feeds an operator runs with `awx-deploy run`. They are never agent tools, and a command name may not repeat a tool, action or function name |

Operation builders return `awx_common.Operation` objects named with the spine's operation
names (`awx_spine.endpoints`). Request ids come from `awx_common.intent.derive_request_id`
with a business key. An endpoint the spine does not yet know counts as not deduplicating.
Adding one means an entry in `awx_spine.endpoints`, a model in `FakeAirwallex`, and the
sandbox evidence for both in [packages/awx_spine/FAKE-FIDELITY.md](../packages/awx_spine/FAKE-FIDELITY.md).

## Registering the kit

1. Add `"k9"` with the kit's guide name to `KITS`, and `"k9": "kits/k9_<slug>"` to `KIT_DIRS`,
   in `packages/awx_common/src/awx_common/kits.py`. The CDK app, the agent's prompt and command
   line, and `awx-deploy` read the kit list from there.
2. If the kit acts on customers' connected accounts, add `"k9"` to `CONNECTED_ACCOUNT_KITS` in
   both `awx_common.tenancy` and `awx_agent.identity`. Every root spec of such a kit must then
   declare `approver` (`ApproverScope.OWN_TENANT` or `ApproverScope.KIT_OPERATOR`), or its
   proposals are refused.
3. If the kit has tenants rows or operator commands, add `deploy/k9.yaml` in the shape of the
   existing deploy files.
4. If an action is schedule-driven, add it to `SCHEDULED_ACTIONS` in
   `packages/awx_spine_aws/src/awx_spine_aws/deploy.py`. `awx-deploy trigger` refuses any
   other action.

## From kit package to CDK stack
With the kit in `KIT_DIRS`, `infra/app.py` loads its manifest through
`infra.kit_manifest.load_manifests`, which imports the kit in a subprocess and checks its
exports. The generic `KitStack` then builds `AwxKitK9` with one state machine per action
(`awx-k9-<action>`), the kit-step Lambda and role for each, and any declared functions. The
spine adds a reader Lambda for kit 9 with that kit's read allowlists, and the agent stack adds
the kit's tool Lambda and Gateway target. No CDK file changes.

Synthesise just the new kit to check it, with `REGION` set in the shell
(`export REGION=us-west-2`, or the Region of the deployment being updated):

```sh
make synth ACCOUNT=111122223333 ALERT_EMAIL=you@example.com KITS=k9 REGION="$REGION"
```

## Rules the kit must follow

- Runner rules 0 to 11 and kit money rules M1 to M4 in [workflow-rules.md](workflow-rules.md).
  In particular: `validate`, `policy`, `fan_out` and `child_tenant` are pure; builds,
  postconditions and `on_abandon` are idempotent; the postcondition copes with failed and
  unknown outcomes.
- Policy computes every amount and puts it in `PolicyDecision.computed`; the model supplies
  only what the tool schema allows.
- Connected-account operations are bound with `bind_operation` or `bind_platform_operation`
  from the run's tenant, never from tool input.
- `agent_min_tier` stays at the default SINGLE unless the kit README justifies AUTO for that
  action.
- Read allowlists are as narrow as the kit's reads need.
- Every sandbox object the kit creates carries a `poc-k9-` label, and its tests never change an
  object they did not create.

## Tests the kit needs

Each existing kit's `tests/` directory has an example of every row.

| Test | What it proves |
|---|---|
| Contract | `TOOLS`, `SPECS` and `READS` line up, every read stays inside the allowlist, no tool takes a server-owned field |
| Tool surface | through the real dispatcher (`awx_agent.testing.KitToolHarness`): the approval binds to the hash, tenants stay isolated, the model never chooses an amount, account or request id |
| Scenarios | the kit's actions end to end through `awx_spine_aws.local_runner.run_kit_workflow` on `FakeAirwallex`, including under each `Fault` |
| Properties | Hypothesis property tests for every allocator and money rule |
| Acceptance | the kit's acceptance scenario, with a live variant marked `sandbox` and run only behind a switch |
| Synth | marked `synth`: `build_app(App(context={"kits": "k9"})).synth()` with `AWX_SKIP_RUNTIME_DEPS=1`, then assertions on the kit's template |

The repository-wide suites pick the kit up from `KIT_DIRS` too: the kit-stack synth tests in
`packages/awx_agent/tests` check its stack, IAM and cdk-nag, and `tests/contract` checks its
operator commands. Run `make check` and `make synth-test` before opening a change.
