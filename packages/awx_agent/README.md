# awx_agent

The agent plane. A Strands agent on Amazon Bedrock AgentCore Runtime reads Airwallex data and
proposes actions through one AgentCore Gateway target per kit; it never writes. The package
holds the Runtime entrypoint, the Gateway REQUEST interceptor, the per-kit tool Lambda, the
CDK registration of each kit's tools (used by `infra`'s `AwxAgentStack` and the generic
`KitStack`), the `awxpoc` command-line interface (CLI) and `awx_agent.testing`, the harness kit
packages use to run their tools with no AWS. It builds on `awx_common` (contract types) and
`awx_spine_aws` (the workflow engine the tools preview and propose with). See
[docs/architecture.md](../../docs/architecture.md) and [docs/security.md](../../docs/security.md)
for how it fits the whole system.

```text
awxpoc CLI --(Cognito access token)--> AgentCore Runtime (Strands agent, awx_agent.runtime.app)
   --(same user token)--> AgentCore Gateway (Cognito JWT authorizer)
      --> REQUEST interceptor: verifies the token (RS256 against the JSON Web Key Set, JWKS)
          and writes it into tools/call as _awx_caller (a declared, optional, server-filled
          property)
      --> kit tool Lambda (awx_agent.handlers.tools), one Gateway target per kit; verifies the
          token again, resolves the tenant server-side
            read_*    -> the kit's READS with engine.policy_context(pure=True) via reader_for(kit)
            propose_* -> engine.propose(origin=AGENT): agent approval floor, derived proposal id
                         -> StartExecution(name=proposal_id, engine.execution_input)
```

## Modules

| Module | What it holds |
|---|---|
| `runtime/app.py` | AgentCore Runtime entrypoint: payload checks, bearer token pass-through, kit tool filter, paged Gateway tool listing, model fallback, per-session history |
| `runtime/prompt.py` | the agent system prompt for one kit |
| `handlers/interceptor.py` | Gateway REQUEST interceptor: verifies the caller's token and writes it into every tools/call |
| `handlers/tools.py` | the per-kit tool Lambda: `ToolDispatcher` (identity, tenant, argument checks, read and propose) and `GatewayToolEnv` (the engine environment for the tool plane) |
| `identity.py` | `CallerIdentity` from a verified token, the requester and approver groups, the tenant a kit runs as |
| `jwtverify.py` | `CognitoVerifier` and `JwksCache`: in-process RS256 verification with PyJWT |
| `schema.py` | kit JSON Schema to Gateway tool schema (type, description, properties, required, items; other keywords folded into descriptions) and structural argument checks |
| `registry.py` | `load_kit_tools` (checks a kit's TOOLS, SPECS and READS) and `register_kit_tools` (CDK: a kit's tool Lambda, its IAM and its Gateway target) |
| `staging.py` | Lambda and Runtime code assets at synth time; Runtime wheels for linux/arm64 |
| `nag.py` | resource-level cdk-nag suppressions, each naming the exact finding with `appliesTo` |
| `cli.py` | `awxpoc login`, `chat`, `proposals list` and `show`, `approve`, `reject` |
| `testing.py` | `KitToolHarness`: a kit's tools through the real `ToolDispatcher` with a fake reader, a recording Step Functions client and a local token issuer |

## Key behaviours

- The model reads and proposes, never writes. A tool Lambda never sends a task token, never
  invokes the mutation adapter, never writes the kit store, and its IAM role allows none of
  those (`register_kit_tools`).
- Identity comes only from the verified token. The interceptor verifies it and writes it into
  the tool arguments; `ToolDispatcher.dispatch` verifies it again (signature, issuer, client,
  `token_use`, expiry), so a direct Lambda invoke with a forged event gets `NOT_AUTHORISED`.
  Verification is PyJWT 2.15.1 with cryptography 50.0.2, `algorithms=["RS256"]` only, after a
  strict base64url check on every segment and a refusal of any `crit` header (`jwtverify.py`).
- The tenant is resolved server-side. Kits 1-4 run as the platform tenant and kits 5-8 as the
  caller's `tenant_id` claim (`CallerIdentity.run_tenant`), resolved through the tenants table
  within the tool's own kit only (`GatewayToolEnv.resolve_tenant`). A claim whose row is bound
  to another kit is refused before the tool looks at its arguments, including on kits 1-4.
- Approval floor (runner rule 11). Every proposal gets its policy tier; a model-started
  proposal is raised to at least SINGLE (one approver who is not the proposer) unless the
  action's spec sets `agent_min_tier=AUTO`, which each kit justifies in its README or
  DESIGN.md.
  [Rule 11](../../docs/workflow-rules.md#rule-11-proposal-origin-and-the-agent-approval-floor)
  lists those actions and what each can do at AUTO; only kit 1's `execute_plan` can move
  money. `engine.propose` applies the floor and derives the proposal ID; the handler builds
  neither itself.
- Previews and read tools use `engine.policy_context(..., pure=True)`, the same context the
  runner's recheck builds, so the preview's tier and hash equal the recheck's.
- The proposal ID hashes intent and computed values and is the execution name. A retry inside
  one approval window reaches the same execution (same input: StartExecution is idempotent;
  different input: `ExecutionAlreadyExists`) and reports that execution's real status and the
  proposal's recorded outcome, never a status derived from the tier (`ToolDispatcher._existing`).
  A re-proposal whose computed amounts differ gets a new ID and a new execution.
- Read results go under `untrusted_data`, so the model is reminded that provider content is data.
- Reads retry in one layer only, the engine's `retrying`; `GatewayToolEnv` supplies `sleep` and
  a `read_budget_s` that leaves room for a whole reader call inside the Lambda's time.
- The Runtime lists every page of the Gateway's tool list (`list_all_tools`) and refuses a
  turn with no tools, so the model never answers a kit it has no tools for.
- A kit's tools are checked before registration: no server-owned field, `additionalProperties`
  false, every propose tool has a SPECS entry, every read tool a READS entry, names fit in 64
  characters once prefixed (`validate_kit_tools`). `register_kit_tools`
  imports no kit code; the manifest loader subprocess already checked the real modules.
- IAM: the tool role may invoke its kit's own reader (`spine.reader_for(kit)`; there is no
  shared reader), start and describe its kit's executions, GetItem and Query its own ledger
  partitions (`<kit>#*`), GetItem the tenants table, GetItem only `proposal_id` and `outcome`
  from the proposals table (`dynamodb:Attributes` and `dynamodb:Select` conditions; never the
  task token), and decrypt with the spine key. The Runtime role may invoke the Bedrock models;
  beyond that it holds only what the Runtime needs to log, trace and report: create, describe
  and write to log groups under `/aws/bedrock-agentcore/runtimes/*` (every Runtime's, not only
  its own), describe any log group, write AWS X-Ray traces and read sampling rules, put metrics
  in the `bedrock-agentcore` namespace, get its workload access token, and read the deployment
  assets bucket that holds its code asset. Neither touches the adapter, the Airwallex secret or
  a task token.
- The spine key admits the AgentCore service principal with `aws:SourceAccount` only. AWS
  documents no `aws:SourceArn` on Gateway KMS calls; the documented path is the Gateway's
  service role, which holds the key through IAM.
- Assets: Lambdas copy first-party source; the Runtime adds linux/arm64 CPython 3.12 wheels
  with `uv pip install --python-platform aarch64-manylinux2014` (no container, no emulation).
  `AWX_SKIP_RUNTIME_DEPS=1` stages source only, for synth tests (`staging.py`).
- `awxpoc approve` and `reject` print the approval API's own body for any 4xx (410 after a
  failed task-token send) and print a 202 body as a recorded, still-pending decision.

## Known limits

- A retry that straddles an approval-window boundary (`now // approval_ttl` changes between
  the two calls) gets a new proposal ID and starts a second execution. That follows from the
  windowed `derive_proposal_id`; the second proposal still needs its own approval, and the
  kit's business-key request IDs stop a second write of the same object.
- IAM does not isolate tenants. The ledger condition `dynamodb:LeadingKeys <kit>#*` keeps a
  kit's tool Lambda inside its own kit, but every tenant of that kit shares the prefix (pk is
  `<kit>#<tenant>`). Tenant isolation is in code: the tenant comes only from the verified
  token, is resolved server-side through the `TenantResolver`, the `KitStore` is built for
  that one tenant, and runners call `check_tenant_scope` before any write.
- Runtime conversation history lives in process memory per session, keyed on the session ID,
  the kit and the token's `sub` (decoded unverified, because the Runtime's own JWT authorizer
  verified it). It is lost when the microVM recycles.
- The verified token sits in the tool arguments between the interceptor and the tool Lambda.
  Neither logs it; do not turn on Gateway request logging at DEBUG.

## Deployed checks

The tests prove the code; these checks prove the deployed Gateway behaves as the code assumes.
`scripts/deployed_security_checks.py` automates the security checks (read-only, no
money-moving tool is called); see [docs/deployment.md](../../docs/deployment.md).

1. A bad signature is denied: a tools/call whose token has valid claims but a signature from
   another key gets a JSON-RPC error and never reaches the tool Lambda (its logs show no
   invocation). This holds whatever the order of the Gateway authorizer and the interceptor.
2. Identity reaches the Lambda: a normal read_* call through the Gateway succeeds, which needs
   `_awx_caller` (declared in the target schema) to arrive in the Lambda event. A failure with
   NOT_AUTHORISED "carries no caller token" means the Gateway dropped interceptor-added
   arguments.
3. A direct invoke is useless: `aws lambda invoke` on a tool Lambda with a forged
   `client_context` and a self-made `_awx_caller` returns NOT_AUTHORISED.
4. The model ID in the Runtime's `AWX_MODEL_ID` (for example `us.anthropic.claude-sonnet-5`)
   resolves in the deploy Region. The Runtime role's foundation-model resource ends in a
   wildcard for the dated ID behind the geographic inference profile, and its
   `bedrock:InferenceProfileArn` condition allows the model only through the role's own
   profiles (`infra/constructs/agentcore_runtime.py`); narrow the wildcard once that ID is known.
5. Execution idempotency: the same propose call twice yields one execution.
6. Gateway KMS use: CloudTrail shows which principal and encryption context the Gateway's KMS
   calls carry, which decides whether to scope the role's grant with
   `kms:EncryptionContext:aws:bedrock-agentcore-gateway:arn`.

## Tests

Run from the repository root (see [docs/testing.md](../../docs/testing.md)):

```bash
uv run --all-packages pytest packages/awx_agent/tests -m "not sandbox and not synth and not live"
uv run --all-packages pytest packages/awx_agent/tests -m synth   # cdk synth; slow
```

| Topic | Files | Marker |
|---|---|---|
| Token verification and identity | `test_jwtverify.py`, `test_interceptor.py` | none |
| Tool dispatch, read and propose | `test_tool_handlers.py`, `test_tool_read_retries.py` | none |
| Prompt injection cannot cause a write | `test_prompt_injection.py` | none |
| Tool registration and schemas | `test_registry.py`, `test_schema.py` | none |
| Runtime entrypoint | `test_runtime.py` | none |
| CLI | `test_cli.py` | none |
| Asset staging | `test_staging.py` | none |
| Kit tool harness | `test_testing_harness.py` | none |
| AgentStack security shape | `test_synth.py` | `synth` |
| Generic KitStack, full app topology, exact IAM per role, manifest loader isolation | `test_kit_stacks.py` | `synth` |
| Lambda and Runtime assets import in isolation | `test_assets.py` | `synth` |

Helpers: `awx_agent_stubs.py` (strict boto3 and spine stand-ins), `jwt_helpers.py` (test keys
and tokens), `role_check.py` (exact action and resource pairs per IAM role) and
`fixtures/awx_fixture_kit` (a minimal kit whose policy reads every `PolicyContext` capability).
The package has no sandbox or live tests.
