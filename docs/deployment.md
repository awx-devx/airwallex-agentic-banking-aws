# Deployment runbook

From an empty AWS account to a working demo: deploy the stacks, seed the Airwallex secret,
create demo users, load each kit's state, then use the agent. The AWS Cloud Development Kit
(CDK) app itself (stacks, context parameters, partial deploys, teardown) is described in
[infra/README.md](../infra/README.md).

Every command runs from the repository root unless it says otherwise. Examples use
`--profile awx-deploy`. Replace it with your own profile. The deployment targets
`ap-southeast-2`: `make deploy` has no Region variable, and the model grants and
`scripts/deployed_security_checks.py` assume that Region.

## Prerequisites

- An AWS account and operator credentials with administrator access to it in `ap-southeast-2`.
- Amazon Bedrock model access, in that Region, to the agent's model and its fallback. The model
  IDs are set in `packages/awx_agent/src/awx_agent/runtime/app.py` and granted in
  `infra/constructs/agentcore_runtime.py`: the Australian cross-Region inference profiles
  `au.anthropic.claude-sonnet-5` (primary) and `au.anthropic.claude-sonnet-4-5-20250929-v1:0`
  (fallback). These profiles route to `ap-southeast-2` and `ap-southeast-4`. Another Region
  needs other model IDs and a change to `AU_INFERENCE_REGIONS` in
  `infra/constructs/agentcore_runtime.py`.
- Amazon Bedrock AgentCore (Runtime and Gateway) available in the Region.
- An Airwallex sandbox account and its API credentials (a client ID and an API key). Kits 5 to
  8 also need connected accounts and platform payments enabled on that sandbox account.
- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- Node.js, for the CDK CLI (`npx -y aws-cdk@2.1144.0`, pinned by the Makefile).
- The AWS CLI v2, for the commands below.

## Bootstrap and deploy

```
make sync
make check
make synth-test
```

`make check` and `make synth-test` must pass on the commit you deploy (see "Synth and deploy"
in [infra/README.md](../infra/README.md)). Then bootstrap the account and Region once, and
deploy every stack:

```
cd infra
AWS_PROFILE=awx-deploy npx -y aws-cdk@2.1144.0 bootstrap aws://<12-digit id>/ap-southeast-2
cd ..
AWS_PROFILE=awx-deploy make deploy ACCOUNT=<12-digit id> ALERT_EMAIL=<address>
```

`make deploy` runs `cdk deploy --all --outputs-file cdk-outputs.json` from `infra/`, so the
stack outputs land in `infra/cdk-outputs.json`. The later steps read it. The alert address
receives an Amazon SNS subscription confirmation email: confirm it, or the Lambda error and
throttle alarms reach nobody. The monthly AWS Budget (100 USD, alerts at 50 and 100 percent
actual and 100 percent forecast) emails the same address directly.

## Seed the Airwallex secret

The spine creates the Airwallex secret empty. Put the sandbox credentials into it as a JSON
object with `client_id` and `api_key`, out of band, and never commit them or write them to a
file in the repository:

```
SECRET_ARN=$(python3 -c 'import json; print(json.load(open("infra/cdk-outputs.json"))["AwxSpine"]["AirwallexSecretArn"])')
read -rsp "Airwallex client ID: " AWX_CLIENT_ID; echo
read -rsp "Airwallex API key: " AWX_API_KEY; echo
export AWX_CLIENT_ID AWX_API_KEY
( umask 077
  python3 -c 'import json, os; print(json.dumps({"client_id": os.environ["AWX_CLIENT_ID"], "api_key": os.environ["AWX_API_KEY"]}))' \
      > /tmp/awx-secret.json )
aws --profile awx-deploy --region ap-southeast-2 secretsmanager put-secret-value \
    --secret-id "$SECRET_ARN" --secret-string file:///tmp/awx-secret.json
rm /tmp/awx-secret.json; unset AWX_CLIENT_ID AWX_API_KEY
```

Of the deployed roles, only the per-kit readers and the mutation adapter can read this secret.
An operator with IAM access to it can read it too: `awx-deploy` reads it with your operator
credentials for the platform-scope checks its commands make (beneficiaries, connected
accounts, deposits).

## Create demo users

The user pool has no self sign-up. An administrator creates each demo user with:

- the immutable attribute `custom:tenant_id`, which the Pre Token Generation trigger copies
  into the access token's `tenant_id` claim;
- one group: `requester` (may chat with kits and create proposals) or `approver` (may approve
  or reject proposals made by someone else).

A proposal that needs approval must be approved by someone other than its proposer, so each
tenant that approves needs a requester and an approver. Kits 1 to 4 run as the `platform`
tenant. For kits 5 to 8, the tenant is a tenants-table row (next section). Actions whose spec
declares `ApproverScope.KIT_OPERATOR` (an advance, a bridge, a seller payout) are approved by
the kit's `<kit>:operator` tenant instead of the customer's own.

| Username | `custom:tenant_id` | Group | Used by |
|---|---|---|---|
| `demo-platform-requester` | `platform` | requester | kits 1 to 4, deployed checks |
| `demo-platform-approver` | `platform` | approver | kits 1 to 4 |
| `demo-k6-operator` | `k6:operator` | requester | kit 6, deployed checks |
| `demo-k6-approver` | `k6:operator` | approver | kit 6 |
| `demo-<kit>-<name>-requester` | a kit 5, 7 or 8 customer tenant, for example `cara` or `k7:borrower:loan-a` | requester | that customer |
| `demo-<kit>-<name>-approver` | the same tenant | approver | that customer's own-tenant approvals |
| `demo-<kit>-operator-approver` | `k5:operator`, `k7:operator` or `k8:operator` | approver | that kit's operator approvals |

`<name>` is a short label for the tenant, for example `demo-k8-cara-requester` for tenant
`cara` and `demo-k7-loan-a-requester` for tenant `k7:borrower:loan-a`. Usernames become
Secrets Manager names, which cannot contain `:`, so never copy a tenant id with colons into a
username. The real tenant id goes in `custom:tenant_id`.

Each password goes into Secrets Manager as `awx-poc/demo-users/<username>`, a JSON object
`{"username": ..., "password": ...}`. `scripts/deployed_security_checks.py` signs in from these
secrets. The function below creates one user without putting the password on a command line.
The password policy needs 14 or more characters with upper and lower case, a digit and a
symbol:

```
POOL=$(python3 -c 'import json; print(json.load(open("infra/cdk-outputs.json"))["AwxSpine"]["UserPoolId"])')
AWS="aws --profile awx-deploy --region ap-southeast-2"

create_demo_user() {  # create_demo_user <username> <tenant_id> <requester|approver>
  local name=$1 tenant=$2 group=$3
  ( umask 077
    python3 - "$POOL" "$name" <<'EOF'
import json, secrets, sys
pool, name = sys.argv[1:]
password = secrets.token_urlsafe(24) + "aA1!"
json.dump({"username": name, "password": password}, open("/tmp/awx-user-secret.json", "w"))
json.dump({"UserPoolId": pool, "Username": name, "Password": password, "Permanent": True},
          open("/tmp/awx-user-password.json", "w"))
EOF
  )
  $AWS secretsmanager create-secret --name "awx-poc/demo-users/$name" \
      --secret-string file:///tmp/awx-user-secret.json
  $AWS cognito-idp admin-create-user --user-pool-id "$POOL" --username "$name" \
      --message-action SUPPRESS --user-attributes Name=custom:tenant_id,Value="$tenant"
  $AWS cognito-idp admin-set-user-password --cli-input-json file:///tmp/awx-user-password.json
  $AWS cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$name" \
      --group-name "$group"
  rm -f /tmp/awx-user-secret.json /tmp/awx-user-password.json
}

create_demo_user demo-platform-requester platform requester
create_demo_user demo-platform-approver platform approver
create_demo_user demo-k6-operator k6:operator requester
create_demo_user demo-k6-approver k6:operator approver
create_demo_user demo-k8-cara-requester cara requester
create_demo_user demo-k8-cara-approver cara approver
create_demo_user demo-k8-operator-approver k8:operator approver
```

`custom:tenant_id` cannot be changed after the user is created. To move a user to another
tenant, delete the user and create it again.

## Load each kit

A deployed kit runs no setup code, so its state (tenants rows, configuration, books, data
feeds) reaches it only through `awx-deploy`, the operator CLI in
`packages/awx_spine_aws/src/awx_spine_aws/deploy.py`. It runs with your operator credentials,
reads the `AwxSpine` stack for the tenants table, ledger table and secret, and finds state
machines by their fixed `awx-<kit>-<action>` names. Nothing it does moves money.

```
awx-deploy [--profile P] [--region R] tenants put <file> [--replace] [--var NAME=VALUE ...]
awx-deploy [--profile P] [--region R] run <kit> <command> <args.json> [--var NAME=VALUE ...]
awx-deploy plan  <deploy-file> [--var NAME=VALUE ...] [--vars vars.yaml]
awx-deploy [--profile P] [--region R] apply <deploy-file> [--var ...] [--vars ...] [--from N]
awx-deploy [--profile P] [--region R] trigger <kit> <action> <intent.json> --tenant <tenant_id>
```

Run it as `uv run --all-packages awx-deploy ...`. `--tenants-table`, `--ledger-table` and
`--secret-arn` override the stack lookup.

`deploy/k1.yaml`, `k5.yaml`, `k6.yaml`, `k7.yaml` and `k8.yaml` hold each kit's tenants rows
and its ordered operator commands, taken from the "Setting up a deployed kit" section of the
kit's README. Kits 2, 3 and 4 have no deploy file: they load no state (kit 3's README describes
how an operator stages the first payment of its demo). Real ids are `${NAME}` variables, listed
at the top of each file (for example the `acct_...` id of each connected account). Pass them
with `--var NAME=value` or a `--vars` file (YAML or JSON, a mapping of `NAME: value`).

```
uv run --all-packages awx-deploy plan deploy/k8.yaml --vars k8-vars.yaml
uv run --all-packages awx-deploy --profile awx-deploy apply deploy/k8.yaml --vars k8-vars.yaml
```

- `plan` prints every step and writes nothing, needs no AWS credentials, and exits 2 while
  any `${NAME}` or `<placeholder>` is unresolved.
- `apply` refuses a file with any placeholder before writing, then writes the tenants rows and
  runs the commands in order, and stops at the first refusal with exit 1. Resume with
  `--from N`.
- A second `apply` writes nothing twice. In a file with a `once` command, `apply` stops there:
  unchanged rows are left alone, then the first `once` command refuses ("already seeded"). To
  run later steps again, pass `--from N` past the `once` steps. A feed given the same input is a
  no-op.
- `tenants put` checks every row (kit, role, an account matching `^acct_[A-Za-z0-9_-]+$`),
  refuses a connected account that already appears on any other row in any kit, and changes an
  existing row only with `--replace`, and only while that row's kit has no RUNNING execution.
  A command whose args carry `replace: true` is refused on the same condition.
- Each command resolves every tenant through the tenants table in the command's own kit, and
  refuses another kit's row.

### Sandbox objects to create first

The deploy files refer to Airwallex sandbox objects by id. Create these objects before
`awx-deploy apply`. Every beneficiary and connected account carries a `poc-` label, because
the kits find and check objects by it. A deposit needs no label. Where an operator command
takes an object, it reads it from the sandbox before writing anything, and refuses it unless
it carries its kit's `poc-` label (and, for a connected account, is ACTIVE). Kit 7's
`fund_capital` also checks its deposit. The other objects are checked only when a run needs
them, as the last column shows.

| Kit | Object | Label | Checked by |
|---|---|---|---|
| 1 | EUR SWIFT beneficiary for `ob-supplier-eur` | nickname `poc-i1-supplier-de` | `seed_treasury_book`: exists, nickname starts `poc-i1-` |
| 1 | US LOCAL beneficiary for `ob-shipping` (and a LOCAL one in each other obligation's currency the demo uses: GBP for `ob-saas-gbp` and `ob-contractor-gbp`, USD for `ob-office-usd`; `deploy/k1.yaml` names only `ob-supplier-eur` and `ob-shipping`, so add any other obligation to its `beneficiaries` map) | nickname starting `poc-i1-` | `seed_treasury_book` |
| 1 | Platform wallet balances: enough for the demo's payments, above the book's floors (USD 20,000, EUR 500, GBP 500 in `awx_k1_treasury.book`) | deposit, any payer name | the plan's floor checks at run time |
| 2 | Platform USD balance of at least the reserve floor (25,000) plus the at-limit debit | deposit | the plan's floor check at run time |
| 3 | A USD beneficiary for the staged payment, and USD for that payment and its replacement (1.00 USD each in the live runs) | nickname `poc-i3-supplier` | the operator's staging run |
| 4 | Nothing: disputes cannot be staged while online payments are disabled on the sandbox account | | |
| 5 | Connected accounts, ACTIVE | nicknames `poc-i5-t-alpha`, `poc-i5-t-bravo`, `poc-i5-t-charlie` | `seed_platform_setup`: exists, ACTIVE, labelled `poc-i5-` |
| 5 | Platform USD deposit of the `fund_capital` amount (1,000 in `deploy/k5.yaml`) | deposit | the wallet-room check at run time |
| 6 | Employer connected accounts, ACTIVE, each funded in its own USD Global Account (the kit 6 live sandbox test tops `poc-i6-a` up to 20,000 USD, `poc-i6-c` up to at least 5,000 USD and `poc-i6-b` up to 0.01 USD short of its payroll, then deposits 1,000 USD into `poc-i6-c` mid-run) | nicknames `poc-i6-a`, `poc-i6-b`, `poc-i6-c` | the payroll run |
| 7 | Borrower connected accounts, ACTIVE | nicknames `poc-i7-loan-a`, `poc-i7-loan-c` | `add_loan`: exists, ACTIVE, nickname starts `poc-i7-` |
| 7 | Platform USD deposit of at least 20,000.00, its id passed as `K7_DEPOSIT_ID` | deposit | `fund_capital` refuses a deposit smaller than the amount |
| 8 | Seller connected accounts, ACTIVE | nicknames `poc-i8-seller-alice`, `poc-i8-seller-bob`, `poc-i8-seller-cara` | `seed_seller`: exists, ACTIVE, labelled `poc-i8-` |
| 8 | Platform USD deposit after each `load_cycle` of the cycle's net buyer money (22,360 for cycle `c1` in `deploy/k8.yaml`: entitlement minus refunds, summed over sellers) | deposit | settlement at run time |

The repository has no helper that creates sandbox objects or makes deposits for a deployment.
Make them in the Airwallex sandbox web app, or with these API calls, which are the ones the
code and `packages/awx_spine/FAKE-FIDELITY.md` record:

| Object | Call |
|---|---|
| Beneficiary | `POST /api/v1/beneficiaries/create` with a `nickname` (`LIVE_BENEFICIARY` in `kits/k1_treasury/tests/test_acceptance.py` is a complete EUR SWIFT body). A replayed `request_id` creates a second beneficiary, so look for an existing one first (`GET /api/v1/beneficiaries`, matched client-side: `?nickname=` does not filter). The kit 1 live acceptance test (`AWX_LIVE=1`) finds or creates `poc-i1-supplier-de`, and the kit 3 live test (`AWX_K3_LIVE=1`) finds or creates `poc-i3-supplier`. |
| Connected account | `POST /api/v1/accounts/create` with `account_details` and the label as `nickname` (status `CREATED`), then `POST /api/v1/accounts/{id}/submit` with an empty body (`SUBMITTED`), then the sandbox simulation `POST /api/v1/simulation/accounts/{id}/update_status` with `{"next_status": "ACTIVE", "force": true}`. Foreign exchange (FX) on a new account is refused for a few minutes after it turns ACTIVE. |
| Global Account | `GET /api/v1/global_accounts` with `x-on-behalf-of: <acct>` lists a connected account's Global Accounts (the kit 6 live test does this; in its recorded run every employer already had a USD one). Listing the platform's own without the header, and `POST /api/v1/global_accounts/create` (with `request_id`, `country_code` and `required_features`, for example `[{"currency": "USD", "transfer_method": "LOCAL"}]`), have not run from this repository, so check the result in the sandbox web app. The platform needs a Global Account in each currency it is funded in, and may have none in USD. |
| Deposit | `POST /api/v1/simulation/deposit/create` with `{global_account_id, amount, payer_name}`, into a Global Account of the wallet being funded, with `x-on-behalf-of: <acct>` when that wallet is a connected account's. The balance posts at once. Kit 7's `fund_capital` accepts either the `id` or the `source_id` of the deposit's `DEPOSIT` row in `GET /api/v1/financial_transactions` (that row shape is assumed, not recorded from a live read; see `packages/awx_spine/FAKE-FIDELITY.md`). |

Every call needs a bearer token from `POST /api/v1/authentication/login`. In Python,
`awx_spine.AirwallexClient(awx_spine.EnvFileCredentials())` logs in itself from
`~/.config/airwallex-sandbox/credentials.env`. The functions `usd_global_account` and `deposit`
in `kits/k6_payroll/tests/test_live_sandbox.py` are the only committed code that finds or
creates a Global Account and deposits into it against the real sandbox.

Hold the shared lock for every step that uses the sandbox, so two operators or a sandbox test
run on the same host never interleave on one sandbox account:

```
flock /tmp/awx-sandbox.lock <command that makes the deposit>
```

## Run scheduled actions

Some actions are schedule-driven: `run_payroll` (kit 6), `collect_weekly_repayment` and
`reconcile_portfolio` (kit 7), `settle_seller_cycle` and `reconcile_seller_money` (kit 8).
The deployment has no schedules, so no money moves unattended. Propose one on demand with
origin `SCHEDULE`:

```
uv run --all-packages awx-deploy --profile awx-deploy trigger k8 settle_seller_cycle intent.json --tenant cara
uv run --all-packages awx-deploy --profile awx-deploy trigger k7 reconcile_portfolio intent.json --tenant k7:operator
```

`intent.json` is the action's intent as the kit's README describes it (for example
`{"cycle_id": "c1"}` for `settle_seller_cycle`). `trigger` runs the kit's validate and policy
against the deployed stores and reads, then starts the root execution named after the
proposal id, so within one approval window the same proposal never starts twice. It also
refuses to start a second run while a RUNNING execution of the same machine carries a
proposal with the same intent hash, and refuses when a RUNNING execution's input cannot be
read. A finished run never blocks a new trigger.

A production deployment would use Amazon EventBridge Scheduler, one schedule per scheduled
action.

## Use the agent

`awxpoc` (package `awx_agent`) talks to the AgentCore Runtime and the approval API. It reads
its configuration from the stack outputs file (`--config`, or the `AWXPOC_CONFIG` environment
variable) and keeps its tokens under `~/.awxpoc`.

```
export AWXPOC_CONFIG=infra/cdk-outputs.json
uv run --all-packages awxpoc login --username demo-platform-requester
uv run --all-packages awxpoc chat --kit k1 -m "What do we owe this week?"
uv run --all-packages awxpoc chat --kit k1          # a prompt loop
uv run --all-packages awxpoc proposals list --status PENDING
uv run --all-packages awxpoc proposals show <proposal_id>

uv run --all-packages awxpoc login --username demo-platform-approver
uv run --all-packages awxpoc approve <proposal_id> --intent-hash <hash>
uv run --all-packages awxpoc reject  <proposal_id> --intent-hash <hash>
```

`login` asks for the password (or reads it from stdin with `--password-stdin`). `chat` prints
the Runtime session id. Pass it back with `--session` to continue a conversation. The agent
reads and proposes, and never moves money itself. A proposal that needs approval waits in its
state machine until an approver from the right tenant (and never the proposer) approves it with
the intent hash they reviewed, which must match the proposal. Copy `intent_hash` from
`awxpoc proposals show <proposal_id>` after reviewing the intent. `proposals list` shows a
caller only the proposals its tenant may see or decide.

## Check the deployment

`scripts/deployed_security_checks.py` runs read-only checks against the deployed stacks. No
money-moving tool is called. It needs the full deploy (all eight kit stacks), the
`demo-platform-requester` and `demo-k6-operator` users and their `awx-poc/demo-users/*`
secrets, and the outputs file. It uses the Region `ap-southeast-2`.

```
AWS_PROFILE=awx-deploy uv run --all-packages python scripts/deployed_security_checks.py
AWS_PROFILE=awx-deploy uv run --all-packages python scripts/deployed_security_checks.py --outputs path/to/cdk-outputs.json
```

It prints one PASS or FAIL line per check, never a secret, token or password, and exits 1 if
any check fails.

| Check | What it proves |
|---|---|
| 0 | An authenticated `tools/list` on the Gateway answers and lists kit-prefixed tools (`k1___...`). |
| 1 | An unauthenticated `tools/list` is refused (HTTP 401 or 403). |
| 2 | A token with a forged signature is refused (by the Gateway authorizer or the identity interceptor). |
| 2b | The kit 1 tool Lambda logged no invocation during the forged call, so the refusal happened before any tool ran. |
| 3 | A valid caller's identity reaches the tool Lambda: the identity interceptor passes `_awx_caller` through. |
| 3b | Invoking the tool Lambda directly with a forged `_awx_caller` is refused (`NOT_AUTHORISED`). |
| 4 | The kit 1 tool Lambda refuses another kit's tool name, even with a valid token. |
| 5 | A `k6:operator` caller cannot read kit 1 (platform) data. |
| 5b | The approval API lists no other tenant's proposals to the `k6:operator` caller. |
| 6 | The kit 1 reader refuses a payload that names kit 2: the reader's kit comes from its own configuration. |
| 7 | No kit-step role can read the Airwallex secret (IAM policy simulation over every step role). |

## Tear down

See "Teardown" in [infra/README.md](../infra/README.md): the destroy order, the retained
resources to delete by hand, the demo-user secrets, the CDK bootstrap and the Airwallex sandbox
objects that cannot be deleted.
