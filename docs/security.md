# Security

## Threat model

The design assumes the model can be steered by anything it reads and that any component
reachable from the network might be called by someone other than the intended caller. It
defends against:

| Threat | Example |
|---|---|
| Prompt injection through documents and tool output | a dispute evidence file or a supplier email tells the agent to pay a different account or skip approval |
| A model inventing values | the model makes up an amount, an account, a tenant, a request id or a proposal id |
| Replayed or edited approvals | an approval for one proposal is reused for another, the proposal is changed after approval, or the proposer approves their own proposal |
| Cross-kit and cross-tenant access | a caller of one kit reads or moves money in another kit, or in another customer's connected account |
| A stolen or forged tool invocation | someone with Lambda invoke rights calls a tool Lambda directly with a made-up identity |
| Lost or duplicated writes | a timeout leaves a transfer's outcome unknown and a retry sends it twice |

## Controls

| Control | Where it is enforced | How it is tested |
|---|---|---|
| Agent tools are `read_*` or `propose_*` only, and no tool input may carry a server-owned field (account, tenant, request id, intent hash, approver) | `awx_common.tools.ToolSpec`, `awx_agent.registry` | `packages/awx_common/tests`, `packages/awx_agent/tests`, each kit's tool-surface tests |
| Read results reach the model under `untrusted_data`, and the system prompt treats tool output and documents as data | `awx_agent.handlers.tools`, `awx_agent.runtime.prompt` | `packages/awx_agent/tests` (prompt-injection fixtures) |
| Anything the model proposes needs at least one human approval, unless the action's spec sets `agent_min_tier=AUTO` and the kit's docs justify it (agent approval floor, [rule 11](workflow-rules.md#rule-11-proposal-origin-and-the-agent-approval-floor)) | `awx_common.approvals.effective_tier`, applied at propose and at recheck | `packages/awx_common/tests`, `packages/awx_agent/tests`, kit scenario tests |
| Deterministic code computes every amount, account and request id; the workflow re-runs validate and policy and fails closed if the proposal hash moved | kit `policy` functions, `awx_spine_aws.engine.recheck` (rule 1) | `packages/awx_spine_aws/tests`, kit scenario tests |
| An approval binds to the proposal id and hash, comes from someone other than the proposer and lands inside the approval window | `awx_common.approvals.approval_state`, the approval Lambda, the engine's binding phase and the adapter | `packages/awx_common/tests`, `packages/awx_spine_aws/tests` |
| Platform money toward a customer is approved by the kit's operator | `KitWorkflowSpec.approver` (`ApproverScope.KIT_OPERATOR`), `approver_tenant`, the approval Lambda | `packages/awx_spine_aws/tests`, kits 5 to 8 tests |
| Only the adapter Lambda can write to Airwallex, and only kit state machine roles may invoke it | `infra/stacks/spine_stack.py`, `infra/constructs/kit_workflow.py` | `tests/security` (synth), deployed checks |
| The adapter re-reads the proposal and its decisions, checks the operation's kit and hash, and checks tenant scope before any send | `awx_spine_aws.handlers.adapter` | `packages/awx_spine_aws/tests` |
| Each kit has its own reader Lambda: GET only, that kit's path allowlist, a payload naming another kit is refused | `awx_spine_aws.handlers.reader`, `SpineStack.reader_for` | `packages/awx_spine_aws/tests`, `tests/security`, deployed check 6 |
| Connected-account operations are bound to the run's tenant by kit code, and the runner checks tenant scope before every operation (deny by default) | `awx_common.tenancy.bind_operation`, `check_tenant_scope` (rule 4) | `packages/awx_common/tests`, `tests/security/test_cross_kit_tenants.py` |
| Kits 1 to 4 serve platform callers only: a caller whose `tenant_id` claim is not exactly `platform` (another tenant, with or without a tenants-table row, or no claim) is refused with `TENANT_SCOPE` before the tool reads its arguments | `awx_agent.identity.CallerIdentity.run_tenant`, `awx_agent.handlers.tools` | `tests/security/test_platform_kit_callers.py`, `packages/awx_agent/tests`, deployed checks 5 and 5c |
| Every tenants-table row belongs to one kit and role; a caller whose row belongs to another kit is refused | `awx_common.tenancy.resolve_in_kit`, `awx-deploy tenants put` | `tests/security/test_cross_kit_tenants.py`, deployed check 5 |
| Each kit's ledger access is limited by IAM to its own partitions (`dynamodb:LeadingKeys <kit>#*`) | kit-step, tool and kit-function roles | `tests/security`, `infra` synth tests |
| Writes are idempotent and reconciled before any resend after an unknown outcome | `awx_spine.executor.MutationExecutor`, `awx_spine.endpoints` (rule 9, M1) | `packages/awx_spine/tests`, `tests/contract`, kit fault tests on FakeAirwallex |
| cdk-nag AwsSolutions runs over every stack, with a written reason for each suppression and no stack-wide suppression | `infra/app.py`, `awx_agent.nag` | synth tests |
| A secret scan runs in `make check`, and on every commit once `make hooks` has set `core.hooksPath` to `.githooks` | `scripts/secret_scan.py`, `.githooks/pre-commit` | `make secret-scan` |

## Identity and tenancy

Users sign in to one Cognito user pool. An administrator sets each user's immutable
`custom:tenant_id` attribute and adds the user to the `requester` or `approver` group. A Pre
Token Generation trigger copies the attribute into a `tenant_id` claim on the access token.
The tool Lambdas and the approval application programming interface (API) read the tenant from
that claim only, never from `custom:tenant_id`, and refuse a `tenant_id` claim that is present
but not a non-empty string.
A user without the attribute gets no claim, and every tenant-scoped API refuses them.

The AgentCore Runtime, the AgentCore Gateway and the approval API all accept access tokens of
the same app client, so one sign-in works everywhere. The Gateway's interceptor verifies the
token signature (RS256 against the user pool's keys) and passes the verified token to the kit's
tool Lambda, which verifies it again. A direct Lambda invoke with a made-up identity is
refused.

The tenant comes only from the verified token. Kits 1 to 4 run on the platform's own Airwallex
account and serve platform callers only. Their tool Lambdas accept a caller whose `tenant_id`
claim is exactly `platform` and refuse every other caller with `TENANT_SCOPE`, before the tool
reads its arguments: a tenant with a tenants-table row in another kit, a tenant with no row at
all (for example `k5:operator`, a mistyped id, or a user created before its kit's rows were
loaded) and a caller with no claim. Whether a tenant has a row never changes that answer. For
kits 5 to 8 the tool Lambda resolves the claim through the tenants table in the tool's own
kit, and refuses a caller with no claim, the `platform` claim, an unknown tenant or a row bound
to another kit. Kits 5 to 8 act on customers' connected accounts, and a customer row names
exactly one connected account that no other row may use. Kit code builds every connected-account operation from the
resolved tenant with `bind_operation` or `bind_platform_operation`. The model never names an
account.

The approval API lists and decides only proposals whose approver tenant matches the caller's.
For a tenant's own money that is the proposal's tenant. For platform money toward a tenant it
is the kit's operator tenant `<kit>:operator`. Reading needs the requester or approver group.
Deciding needs the approver group and the hash the approver was shown.

## Secrets and keys

The Airwallex secret is created empty and seeded out of band. Of the deployed roles, only the
reader and adapter roles can read it. Kit-step, tool and agent roles cannot. Every table, the
bucket, the secret, the alarm topic and the log groups use one customer managed AWS Key
Management Service (AWS KMS) key. The artifacts bucket enforces TLS, blocks public access and
is versioned. Its `evidence/` objects are readable by the adapter role only.

The tool Lambda may read only `proposal_id` and `outcome` from the proposals table, so it can
never read a task token. The Runtime role may invoke the agent model and its fallback. Beyond
that it holds only what the Runtime needs to log, trace and report: create, describe and write
to log groups under `/aws/bedrock-agentcore/runtimes/*` (every Runtime's, not only its own),
describe any log group, write AWS X-Ray traces and read sampling rules, put metrics in the
`bedrock-agentcore` namespace, get its workload access token, and read the deployment assets
bucket that holds its code asset.
Demo user passwords live in AWS Secrets Manager, never in files
([docs/deployment.md](deployment.md)).

## Deployed checks

`scripts/deployed_security_checks.py` runs read-only against a deployment and calls no tool
that moves money. It prints one PASS or FAIL line per check and no secret, token or password.
[docs/deployment.md](deployment.md#check-the-deployment) lists each check and what it
proves. The checks on caller identity and tenancy work as follows:

- Check 0 follows the Gateway's `nextCursor` through every page of `tools/list` and reports
  the full tool count and the number of pages. It fails when paging stops early: a page whose
  Hypertext Transfer Protocol (HTTP) status is not 200 or that is not a `tools/list` result, a
  tool entry with no name, a repeated cursor, or 50 pages without an end. On 8 Oct 2026 a
  deployment of kits 1 to 8 listed 61 tools across 3 pages.
- Check 3 calls `k1___read_balances` as `demo-platform-requester` and passes only on positive
  evidence that the verified caller reached the tool code: a read result, or `READ_FAILED`
  when the reader could not complete the read. A missing caller token, any other error or a
  malformed reply fails it.
- Check 5 calls `k1___read_balances` as `demo-k6-operator` (tenant `k6:operator`, a row in
  kit 6). It passes only on the tool plane's own refusal: error code `TENANT_SCOPE` with a
  message containing "serves platform callers only". Any other reply fails. That includes
  `NOT_CONFIGURED`, which means the call got past the tenant check, and a `TENANT_SCOPE`
  raised later, by the reader for example. So the result does not depend on whether kit 1 has
  been seeded.
- Check 5c makes the same call as `demo-k5-operator-approver` (tenant `k5:operator`, which
  has no tenants-table row) and passes only on the same refusal. If that user does not exist,
  the check fails and names the user to create.
- A reply of an unexpected shape, a network error or a timeout fails the check it belongs to.
  If a step cannot go on, the script records a FAIL for the run and still prints its summary
  line.

On 8 Oct 2026, check 5 failed on a deployment from a fresh clone into a new account: a caller
whose tenant had no tenants-table row read the platform's balances through kit 1. After the fix
that kits 1 to 4 serve platform callers only, every check passed on that deployment.

## Known limits

- IAM does not separate the tenants of one kit. The ledger condition keeps a kit's roles inside
  that kit's partitions, but every tenant of the kit shares the prefix. Tenant separation is in
  code: the tenant comes only from the verified token, is resolved server-side, the kit store is
  built for that one tenant, and runners check tenant scope before any write.
- The proposals, decisions and audit tables are keyed by proposal id, which names no kit, so
  IAM cannot pin them to a kit. Their kit isolation rests on the adapter, which re-reads each
  proposal and checks its kit and hash.
- A reader trusts the tenant id its caller passes (the workflow's stored tenant, or the tool
  Lambda's verified claim). Within its kit's allowlist it can read any registered tenant of
  that kit. It can never widen the paths or read outside the kit.
- The reader Lambdas (one per kit) share one IAM role, which can read the Airwallex secret and the
  whole tenants table. Each kit's roles may invoke only that kit's reader, and the reader's
  code refuses a payload that names another kit.
- The reader and the adapter read the same Airwallex key. The reader's code path, not the key,
  stops it writing. Airwallex offers scoped read-only keys, which would take write capability
  away from the reader.
- A model retry that straddles an approval-window boundary gets a new proposal id and starts a
  second execution. That proposal needs its own approval, and the kit's business-key request
  ids stop a second write of the same object.
- The verified token travels in the tool arguments between the interceptor and the tool
  Lambda. Neither logs it, so do not turn on Gateway request logging at DEBUG.
- Runtime conversation history lives in process memory per session and is lost when the
  Runtime recycles.
- The Runtime role's Bedrock grant ends in a wildcard to cover a dated or versioned model id
  behind the inference profile.
