# infra: the AWS CDK app

The AWS Cloud Development Kit (CDK) app that deploys the spine, one stack per kit and the
agent plane. `build_app()` in `app.py` builds every stack, and `cdk.json` runs `app.py`. For the
full path from an empty AWS account to a working demo, see
[docs/deployment.md](../docs/deployment.md).

## Stacks

| Stack | Built by | Holds |
|---|---|---|
| `AwxSpine` | `stacks/spine_stack.py` | The spine KMS key, the DynamoDB tables (operations, proposals, decisions, tenants, ledger, audit), the empty Airwallex secret, one reader Lambda per kit, the mutation adapter Lambda, the approval API (HTTP API with a Cognito JWT authorizer), the Cognito user pool with its `requester` and `approver` groups, the artifacts bucket, the alarm topic, alarms and the monthly AWS Budget. |
| `AwxKitK<N>` | `stacks/kit_stack.py` (one generic `KitStack` per kit) | One Step Functions Standard state machine per action in the kit's `SPECS` (`constructs/kit_workflow.py`), named `awx-<kit>-<action>` (a child machine `awx-<kit>-<action>-child`), one kit-step Lambda and role per action, and one Lambda per entry in the kit's `FUNCTIONS`, if any. |
| `AwxAgentStack` | `stacks/agent_stack.py` | The Amazon Bedrock AgentCore Gateway with its identity interceptor, one tool Lambda and Gateway target per kit, and the AgentCore Runtime that hosts the agent. |

Dependencies run in one direction only: the kit stacks depend on the spine, and the agent
stack depends on the kit stacks. A kit stack
imports the spine's tables, key, approval role and that kit's reader. The agent stack imports
each kit's root state machine Amazon Resource Names (ARNs), because a kit's propose tools start that kit's executions.
No kit stack refers to the agent stack and no stack refers to a later one, so each stack can
be deployed after the ones it needs and destroyed before them.

A kit writes no CDK. `KitStack` builds everything from the kit's manifest, which
`kit_manifest.py` produces by importing the kit in a subprocess and validates strictly before
any construct exists. Kit code never runs in the CDK process, so a synth error naming a kit's
manifest is a fault in that kit.

Every Lambda is arm64 Python 3.12 with its own log group. cdk-nag `AwsSolutions` checks run
over every stack, and each suppression carries its reason in the code.

## Context parameters

| Context | Default | Meaning |
|---|---|---|
| `account` | the `CDK_DEFAULT_ACCOUNT` environment variable | The 12-digit AWS account to build for. If neither is set, synth fails rather than target an account by accident. |
| `region` | `us-west-2` (`DEFAULT_REGION` in `awx_common.regions`) | The AWS Region. `make` passes `REGION`. See "Region and models" in [docs/deployment.md](../docs/deployment.md) for the Region table. |
| `model_id` | the Region's primary inference profile, for example `us.anthropic.claude-sonnet-5` | The agent's model or inference profile ID. `make` passes `MODEL_ID`. Required, with `fallback_model_id`, for a Region outside the Region table in [docs/deployment.md](../docs/deployment.md). |
| `fallback_model_id` | the Region's fallback inference profile, for example `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | The model the agent falls back to. `make` passes `FALLBACK_MODEL_ID`. |
| `alert_email` | `AWX_ALERT_EMAIL` | Where alarms and budget alerts go. Required. The SNS subscription's logical id includes the address, so changing it replaces the subscription. |
| `kits` | every kit | A comma-separated subset, for example `-c kits=k1,k4`. An empty or unknown list is refused. See "Partial deploys". |
| `sandbox` | `true` | Sandbox mode (runner rule 10 in `awx_common.workflow`): a finality poll may run the kit's sandbox simulation to advance an object, for example to settle a transfer. `-c sandbox=false` sets `AWX_SANDBOX_MODE=0` on every kit-step Lambda. Only sandbox mode is deployed and tested. |

Synth makes no AWS calls and no context lookups, so it needs no credentials. It does download
the pinned linux/arm64 wheels for the agent-plane Lambda and Runtime assets from the package
index (cached after the first run; `AWX_SKIP_RUNTIME_DEPS=1` skips the Runtime's, as the synth
tests do). See `awx_agent.staging`.

## Synth and deploy

From the repository root, with `REGION` set in the shell (`export REGION=us-west-2`, or the
Region of the deployment being updated):

```
make sync
make check
make synth-test
make synth ACCOUNT=111122223333 ALERT_EMAIL=you@example.com REGION="$REGION"
make bootstrap ACCOUNT=<12-digit id> REGION="$REGION"
make deploy ACCOUNT=<12-digit id> ALERT_EMAIL=<address> REGION="$REGION"
```

`make synth` and `make deploy` also take `KITS=k1,k4`, `MODEL_ID`, `FALLBACK_MODEL_ID` and
`CDK_ARGS`.

`make bootstrap` runs `cdk bootstrap aws://<12-digit id>/<REGION>` once per account and Region
from the repository root, where no `cdk.json` loads the app, so it needs no alert address.
`CDK_ARGS` passes extra CDK CLI arguments to `cdk synth` and `cdk deploy`. `cdk deploy` asks a
person at a terminal to approve AWS Identity and Access Management (IAM) changes. A shell
with no terminal (a continuous
integration (CI) job, a coding agent) passes `CDK_ARGS="--require-approval never"`.

`make check` (lint, secret scan, offline tests) and `make synth-test` must pass on the exact
commit being deployed. `make synth-test` synthesises the full app and checks the exact IAM
statements of every tool, step and declared-function role, per-kit isolation, the Lambda
asset imports and cdk-nag over every stack. On an arm64 host none of these tests is skipped.
On another architecture only the isolated-import tests skip, and they say why. Never deploy a
tree where any of them failed. `make sandbox-test` calls the Airwallex sandbox and is not
needed for a deploy (see [docs/testing.md](../docs/testing.md) for the sandbox lock).

`make synth` and `make deploy` run the CDK CLI from `infra/`:

```
cd infra
npx -y aws-cdk@2.1144.0 synth --all -c account=111122223333 -c alert_email=you@example.com
npx -y aws-cdk@2.1144.0 deploy --all --outputs-file cdk-outputs.json \
    -c account=<12-digit id> -c alert_email=<address> [--require-approval never]
```

Add `-c kits=k1,k4` to either when `KITS` is set, and the contents of `CDK_ARGS` at the end.
`cdk deploy --all` orders the stacks itself:
spine, kit stacks, agent stack. `infra/cdk-outputs.json` holds the stack outputs (Gateway URL,
Runtime ARN, user pool client, approval API URL, secret ARN, tenants table). `awxpoc --config`
and `scripts/deployed_security_checks.py` read it. It is git-ignored.

## Partial deploys

The agent stack depends on every kit stack it carries, so one kit that fails to import, synth
or deploy blocks the agent stack for every kit. To deploy a subset, pass the kits context:
`make deploy ... KITS=k1,k4`. The agent stack then registers only those kits' tools.

A partial deploy on top of a full one removes every other kit's Gateway targets: with
`-c kits=k1,k4` the agent stack is redeployed with kit 1 and kit 4 tools only, and the other
kits' tools disappear until the next full deploy. Synth prints an `awx:partial-agent-stack`
warning that names the kits that would lose their tools.

## Changing or removing an action

Cross-stack references are CloudFormation exports. `AwxAgentStack` imports each kit stack's
root state machine ARNs (one per `SPECS` action), and each kit stack imports the spine's
reader ARN for that kit, the tables, the key and the approval role.

CloudFormation refuses to update a producer whose export is still imported ("export ... in
use"), and CDK deploys producers first. So removing or renaming a `SPECS` action, or replacing
a state machine or a reader, takes two deploys:

1. Deploy the consumer first without the reference, for example
   `npx -y aws-cdk@2.1144.0 deploy AwxAgentStack --exclusively ...` with the tool removed.
2. Then deploy the producer (the kit stack or the spine).

Alternatively keep the old export for one deploy with `stack.export_value(...)` in the
producer, and remove it in the next. A state machine's name is fixed (`awx-<kit>-<action>`),
so a change to it replaces the machine and needs the same two deploys.

## Operational rules

### Never redrive a kit execution

Never redrive a kit execution (Step Functions RedriveExecution, from the console or the CLI).
A redriven run resumes at the state that failed, possibly an Adapter state that sends money,
after `PolicyContext.run_status` has told a person's tool that the run was over. Start a new
proposal instead.

### Tool-call time budget

An AgentCore Gateway invocation times out after 15 minutes (900 s, the default in the Gateway
service quotas, adjustable). A kit's tool Lambda times out after 60 s, and its read retries
(`retrying` in the engine: waits of 1, 2 and 4 s, at most 4 reader calls of up to 30 s each)
only wait while a whole reader call still fits in the remaining time, so a tool call ends
within 60 s. Keep the tool Lambda timeout below the Gateway's if either is changed.

## Tenants table rows

Kits 1 to 4 run as the `platform` tenant and need no tenants rows. Kits 5 to 8 run as a tenant
resolved from the tenants table, and every row belongs to one kit. A row is refused if it has
no kit, if it is an operator row with an account, or if it is any other role without an
account:

| tenant_id | kit | role | connected_account_id |
|---|---|---|---|
| `k6:operator` | k6 | operator | (none) |
| `k6:employer:<code>` | k6 | customer | that employer's `acct_...` |
| a kit 5, 7 or 8 customer | k5, k7 or k8 | customer | its `acct_...` |
| `k7:operator` | k7 | operator | (none) |

A Cognito user's immutable `custom:tenant_id` names the row. An operator row is an operator
of its own kit only: `k6:operator` is refused by every kit 5, 7 and 8 tool. A connected
account may appear on one row only, in any kit.

Kit 7's `reconcile_portfolio` runs as the `k7:operator` tenants row (kit k7, role operator, no
connected account) that `deploy/k7.yaml` creates:
`awx-deploy trigger k7 reconcile_portfolio intent.json --tenant k7:operator`.

The rows reach the table through `awx-deploy tenants put` or `awx-deploy apply` with a deploy
file from `deploy/` (see [docs/deployment.md](../docs/deployment.md)). With each row
`awx-deploy` writes an `account#<acct>` marker item in the same transaction. The marker items
are not tenants: they name no kit, so every resolver refuses them. Leave them alone, and delete
one by hand only together with the row that owns it.

Approval does not use the tenants table: the approval API reads the caller's `tenant_id` token
claim. A proposal's approvers are its own tenant, or the kit's `<kit>:operator` tenant when
the action's spec declares `ApproverScope.KIT_OPERATOR` (money that leaves a platform, lender
or marketplace balance toward a tenant).

## Teardown

These are instructions for a person. Nothing here is run by the build.

### Order

`npx -y aws-cdk@2.1144.0 destroy --all -c account=<id> -c alert_email=<address>` from `infra/`,
with no `-c kits=` so every kit stack is in the app. The CLI destroys the agent stack, then
the kit stacks, then the spine:

- the agent stack imports every kit's state machines;
- kit stacks own the policies attached to the spine's approval role, and import spine exports,
  so the spine cannot be deleted first.

To remove one kit, deploy the agent stack with `-c kits=` naming the other kits, then run
this from `infra/`, with no `-c kits=` context:

```
npx -y aws-cdk@2.1144.0 destroy --exclusively AwxKitK<N> -c account=<id> -c alert_email=<address>
```

With `-c kits=`, `AwxKitK<N>` is not in the app and the CLI cannot find the stack. Without
`--exclusively` the CLI also destroys every stack that depends on the named one, and the agent
stack depends on every kit stack. Remove the kit from `KIT_DIRS` in
`packages/awx_common/src/awx_common/kits.py` only after that.

### Retained resources

These spine resources are `RETAIN` and survive `cdk destroy --all`. Delete them by hand in this
order:

1. The Airwallex secret (Secrets Manager). It holds sandbox credentials, so it goes first:
   delete it with no recovery window if the sandbox key is also being revoked.
2. Every DynamoDB table (operations, proposals, decisions, tenants with its rows and `account#`
   markers, ledger, audit) and the Cognito user pool with its users.
3. The artifacts bucket: empty it first (every object version, including `evidence/`), then
   delete it.
4. The spine KMS key, last. Every Lambda logs to its own stack log group, which `cdk destroy`
   deletes. Delete any log group the AgentCore Gateway and Runtime created outside
   CloudFormation, then schedule the key's deletion (7 to 30 days) once nothing it encrypts is
   left.

The budget `awx-poc-monthly-<region>` (AWS Budgets) is a spine resource, so
`cdk destroy --all` deletes it. The name carries the Region and the budget counts only that
Region's costs, so each Region's deployment in an account has its own budget.

### Outside CloudFormation

- The demo-user secrets `awx-poc/demo-users/<name>` (Secrets Manager), created by hand when
  the demo users were made. Delete them with no recovery window.
- The CDK bootstrap, only if nothing else in the account and Region uses it: empty and delete
  the `cdk-hnb659fds-assets-<account>-<region>` bucket (every version), delete the
  `cdk-hnb659fds-container-assets-<account>-<region>` ECR repository, then the `CDKToolkit`
  stack.

### Airwallex sandbox objects

The Airwallex sandbox cannot delete most of what the kits and tests create, so those objects
stay in the sandbox account. Every object a kit or test creates carries a `poc-` label (a
nickname, reference, request id prefix or payer name, for example `poc-k1-`, `poc-i5-t-alpha`,
`poc-ct-`), so list them by that label in the sandbox web app or through the API. They include:

- connected accounts (cannot be deleted);
- beneficiaries, cardholders and cards (cards can be frozen or closed, not deleted);
- transfers, FX conversions, connected-account transfers, charges and payment intents, which
  stay in their final state;
- deposit simulations, Global Accounts and uploaded evidence files;
- card authorisations: a simulated authorisation that stays PENDING keeps its amount reserved
  on the platform, and a sandbox API key without capture or reversal access cannot release it.
