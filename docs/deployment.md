# Deployment runbook

From an empty AWS account to a working demo: deploy the stacks, seed the Airwallex secret,
create demo users, load each kit's state, then use the agent. The AWS Cloud Development Kit
(CDK) app itself (stacks, context parameters, partial deploys, teardown) is described in
[infra/README.md](../infra/README.md).

Every command runs from the repository root unless it says otherwise. Examples use
`--profile awx-deploy`. Replace it with your own profile.

## Region and models

The AWS Region is a deploy-time choice. The `make` targets read these variables:

| Variable | Default | Meaning |
|---|---|---|
| `REGION` | `us-west-2` | The AWS Region to bootstrap and deploy to (CDK context `region`). |
| `MODEL_ID` | the primary inference profile of `REGION` | The agent's model or inference profile ID (CDK context `model_id`). |
| `FALLBACK_MODEL_ID` | the fallback inference profile of `REGION` | The model the agent falls back to when the primary is unavailable (CDK context `fallback_model_id`). |

The default Region and the Region table are defined once, in
`packages/awx_common/src/awx_common/regions.py`. The agent's models are Claude Sonnet 5
(primary) and Claude Sonnet 4.5 (fallback), called through the geographic cross-Region
inference profile of the deploy Region:

| Deploy Region | Primary model ID | Fallback model ID |
|---|---|---|
| `us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, `ca-central-1` | `us.anthropic.claude-sonnet-5` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| `eu-central-1`, `eu-north-1`, `eu-west-1`, `eu-west-2`, `eu-west-3` | `eu.anthropic.claude-sonnet-5` | `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| `ap-southeast-2` | `au.anthropic.claude-sonnet-5` | `au.anthropic.claude-sonnet-4-5-20250929-v1:0` |

Any other Region that offers Amazon Bedrock AgentCore needs `MODEL_ID` and
`FALLBACK_MODEL_ID` set to model or inference profile IDs that Region offers. Without them,
synth fails with a message that names the variables. The Runtime role can invoke its
inference profiles in the deploy Region. It can invoke the foundation models behind them in
any Region, but only through those profiles, so the grant lists no destination Regions. A
plain foundation-model ID (with no profile prefix) is granted in the deploy Region only.

Pass the same `REGION` to every command for a deployment. A `make` run with no `REGION`
deploys to `us-west-2`, so an existing deployment in another Region is updated only by
passing the Region it was deployed to (`REGION=ap-southeast-2` for a deployment there). A run
with a different `REGION` deploys a separate copy; copies in different Regions of the same
account do not clash. The `make bootstrap` and `make deploy` lines and the `aws` commands in
this runbook read `REGION` from the shell. The `make` lines pass `REGION="$REGION"`, which
is an empty value when the shell has no `REGION` set, and `make` refuses an empty `REGION`.
This is why "Bootstrap and deploy" starts by setting it.

## Prerequisites

- An AWS account and operator credentials with administrator access to it in the deploy
  Region.
- Amazon Bedrock model access, in that Region, to the agent's model and its fallback (see
  "Region and models").
- Amazon Bedrock AgentCore (Runtime and Gateway) available in the Region.
- An Airwallex sandbox account and its API credentials (a client ID and an API key). Kits 5 to
  8 also need connected accounts and platform payments enabled on that sandbox account.
- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- Node.js, for the CDK CLI (`npx -y aws-cdk@2.1144.0`, pinned by the Makefile).
- The AWS CLI v2, for the commands below.

## Bootstrap and deploy

Set the Region first: `us-west-2`, or the Region of the deployment being updated. A new
shell needs it set again:

```
export REGION=us-west-2
```

Then build and test:

```
make sync
make check
make synth-test
```

`make check` and `make synth-test` must pass on the commit you deploy (see "Synth and deploy"
in [infra/README.md](../infra/README.md)). Each takes about 4 to 5 minutes, and pytest prints
only dots while it runs. Then bootstrap the account and Region once, and deploy every stack:

```
AWS_PROFILE=awx-deploy make bootstrap ACCOUNT=<12-digit id> REGION="$REGION"
AWS_PROFILE=awx-deploy make deploy ACCOUNT=<12-digit id> ALERT_EMAIL=<address> REGION="$REGION"
```

`make bootstrap` runs `cdk bootstrap aws://<12-digit id>/<region>` from the repository
root, once per account and Region. It needs no alert address: the root has no `cdk.json`, so
the CDK CLI does not load the app. Bootstrapping from `infra/` loads the app, which then
fails for want of an alert address.

`cdk deploy` asks a person at a terminal to approve each stack's AWS Identity and Access
Management (IAM) changes. A shell with no terminal (a continuous integration (CI) job, a
coding agent) cannot answer, and the deploy stops before it creates anything. There, you can
review the IAM changes first with `cdk diff` from `infra/`, passing the same context
`make deploy` passes: `-c account=... -c alert_email=... -c region=...`, plus `-c kits=...`,
`-c model_id=...` and `-c fallback_model_id=...` if you set them. Then pass the CDK CLI's
approval flag through `CDK_ARGS`:

```
AWS_PROFILE=awx-deploy make deploy ACCOUNT=<12-digit id> ALERT_EMAIL=<address> \
    REGION="$REGION" CDK_ARGS="--require-approval never"
```

A full deploy of every stack took about 17 minutes on 8 Oct 2026.

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
aws --profile awx-deploy --region "${REGION:?set REGION first}" secretsmanager put-secret-value \
    --secret-id "$SECRET_ARN" --secret-string file:///tmp/awx-secret.json
rm /tmp/awx-secret.json; unset AWX_CLIENT_ID AWX_API_KEY
```

The `read -rsp` prompts need a terminal. In a shell with no terminal, read the same values
from an owner-only env file instead: the `KEY=VALUE` file with `AWX_CLIENT_ID` and
`AWX_API_KEY` that the sandbox tests read, `~/.config/airwallex-sandbox/credentials.env` by
default. Keep that file outside the repository and readable by you only (`chmod 600`). This
keeps the values off every command line and out of shell history:

```
SECRET_ARN=$(python3 -c 'import json; print(json.load(open("infra/cdk-outputs.json"))["AwxSpine"]["AirwallexSecretArn"])')
( umask 077
  uv run --all-packages python -c 'import json; from awx_spine import EnvFileCredentials; c = EnvFileCredentials().get(); print(json.dumps({"client_id": c.client_id, "api_key": c.api_key}))' \
      > /tmp/awx-secret.json )
aws --profile awx-deploy --region "${REGION:?set REGION first}" secretsmanager put-secret-value \
    --secret-id "$SECRET_ARN" --secret-string file:///tmp/awx-secret.json
rm /tmp/awx-secret.json
```

To read another file, pass its path: `EnvFileCredentials(Path("<file>"))`, with
`from pathlib import Path`. The secret's Amazon Resource Name (ARN) reaches
`infra/cdk-outputs.json` only once the whole deploy has finished. To seed the secret while
the later stacks still deploy, read it from the spine stack's outputs instead:

```
SECRET_ARN=$(aws --profile awx-deploy --region "${REGION:?set REGION first}" \
    cloudformation describe-stacks --stack-name AwxSpine --query "Stacks[0].Outputs[?OutputKey=='AirwallexSecretArn'].OutputValue" \
    --output text)
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

These are the users the kit walkthroughs and the deployed checks use:

| Username | `custom:tenant_id` | Group | Used by |
|---|---|---|---|
| `demo-platform-requester` | `platform` | requester | kits 1 to 4, deployed checks |
| `demo-platform-approver` | `platform` | approver | kits 1 to 4 |
| `demo-k5-alpha-requester` | `t-alpha` | requester | kit 5 customer Alpha: a shortfall and a bridge request |
| `demo-k5-alpha-approver` | `t-alpha` | approver | kit 5 customer Alpha's own-tenant approvals |
| `demo-k5-bravo-requester` | `t-bravo` | requester | kit 5 customer Bravo: a second bridge demo once Alpha's obligation is bridged |
| `demo-k5-operator-approver` | `k5:operator` | approver | kit 5 bridge approvals, deployed checks |
| `demo-k6-operator` | `k6:operator` | requester | kit 6, deployed checks |
| `demo-k6-approver` | `k6:operator` | approver | kit 6 |
| `demo-k7-loan-a-requester` | `k7:borrower:loan-a` | requester | kit 7 borrower of `loan-a`: an advance |
| `demo-k7-loan-a-approver` | `k7:borrower:loan-a` | approver | kit 7 borrower's own-tenant approvals |
| `demo-k7-loan-c-requester` | `k7:borrower:loan-c` | requester | kit 7 borrower of `loan-c`: a second advance |
| `demo-k7-loan-c-approver` | `k7:borrower:loan-c` | approver | kit 7 second borrower's own-tenant approvals |
| `demo-k7-operator-approver` | `k7:operator` | approver | kit 7 advance approvals |
| `demo-k8-cara-requester` | `cara` | requester | kit 8 seller Cara |
| `demo-k8-cara-approver` | `cara` | approver | kit 8 seller Cara's own-tenant approvals |
| `demo-k8-operator-approver` | `k8:operator` | approver | kit 8 seller payout approvals |

The tenant ids match the deploy files: `t-alpha` is a kit 5 customer in `deploy/k5.yaml`,
`cara` a kit 8 seller in `deploy/k8.yaml`, and `k7:borrower:loan-a` is the value to pass as
`K7_TENANT_LOAN_A` to `deploy/k7.yaml`, and `k7:borrower:loan-c` the value for
`K7_TENANT_LOAN_C`. The second loan is there for a second advance demo: the first advance can
use all of `loan-a`'s commitment, and kit 7 blocks a disbursement once the commitment is used. Kit 7's borrower tenant ids are your choice: pick
another and the user's `custom:tenant_id` must name the same value. To demo another customer,
follow the same pattern: `demo-<kit>-<name>-requester` and `demo-<kit>-<name>-approver`, where
`<name>` is a short label for the tenant.

Usernames become Secrets Manager names, which cannot contain `:`, so never copy a tenant id
with colons into a username. The real tenant id goes in `custom:tenant_id`.

Every customer tenant (`t-alpha`, `k7:borrower:loan-a`, `cara`) needs a tenants-table row,
which its kit's deploy file writes. The `platform` tenant and an operator tenant that only
approves (`k5:operator`, `k8:operator`) need none: the approval API reads the caller's
`tenant_id` claim. `k6:operator` and `k7:operator` also start runs that resolve their tenant
through the table (kit 6 payroll, kit 7 reconciliation), so `deploy/k6.yaml` and
`deploy/k7.yaml` write a row for each.

Each password goes into Secrets Manager as `awx-poc/demo-users/<username>`, a JSON object
`{"username": ..., "password": ...}`. `scripts/deployed_security_checks.py` signs in from these
secrets. The function below, `create_demo_user <username> <tenant_id> <requester|approver>`, creates
one user without putting the password on a command line. It calls the AWS CLI through
`awsd`, which adds the deploy profile and `REGION` and works in bash and zsh.
The password policy needs 14 or more characters with upper and lower case, a digit and a
symbol:

```
POOL=$(python3 -c 'import json; print(json.load(open("infra/cdk-outputs.json"))["AwxSpine"]["UserPoolId"])')
awsd() {
  aws --profile awx-deploy --region "${REGION:?set REGION first, for example: export REGION=us-west-2}" "$@"
}

create_demo_user() {
  local name=$1 tenant=$2 group=$3
  [ -n "${REGION:-}" ] || { echo "set REGION first, for example: export REGION=us-west-2" >&2; return 1; }
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
  awsd secretsmanager create-secret --name "awx-poc/demo-users/$name" \
      --secret-string file:///tmp/awx-user-secret.json
  awsd cognito-idp admin-create-user --user-pool-id "$POOL" --username "$name" \
      --message-action SUPPRESS --user-attributes Name=custom:tenant_id,Value="$tenant"
  awsd cognito-idp admin-set-user-password --cli-input-json file:///tmp/awx-user-password.json
  awsd cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$name" \
      --group-name "$group"
  rm -f /tmp/awx-user-secret.json /tmp/awx-user-password.json
}

create_demo_user demo-platform-requester platform requester
create_demo_user demo-platform-approver platform approver
create_demo_user demo-k5-alpha-requester t-alpha requester
create_demo_user demo-k5-alpha-approver t-alpha approver
create_demo_user demo-k5-bravo-requester t-bravo requester
create_demo_user demo-k5-operator-approver k5:operator approver
create_demo_user demo-k6-operator k6:operator requester
create_demo_user demo-k6-approver k6:operator approver
create_demo_user demo-k7-loan-a-requester k7:borrower:loan-a requester
create_demo_user demo-k7-loan-a-approver k7:borrower:loan-a approver
create_demo_user demo-k7-loan-c-requester k7:borrower:loan-c requester
create_demo_user demo-k7-loan-c-approver k7:borrower:loan-c approver
create_demo_user demo-k7-operator-approver k7:operator approver
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
`--secret-arn` override the stack lookup. With no `--region` it uses the Region recorded in
`infra/cdk-outputs.json` (the `AwxRegion` output), and `us-west-2` when that file does not
exist. The `awxpoc` CLI takes the Region from the outputs file it is given (`--config` or
`AWXPOC_CONFIG`) and has no Region default.

`deploy/k1.yaml`, `k5.yaml`, `k6.yaml`, `k7.yaml` and `k8.yaml` hold each kit's tenants rows
and its ordered operator commands, taken from the "Setting up a deployed kit" section of the
kit's README. Kits 2, 3 and 4 have no deploy file: they load no state (an operator stages the
first payment of kit 3's demo with `kits/k3_incident/scripts/stage_original_transfer.py`, which
kit 3's README describes). Real ids are `${NAME}` variables, listed
at the top of each file (for example the `acct_...` id of each connected account). Pass them
with `--var NAME=value` or a `--vars` file (YAML or JSON, a mapping of `NAME: value`).

Some seeded demos depend on the day you load them. Kit 1's seed starts its demo book at the
seed time, so its obligations fall due within the next 72 hours. Kit 5's bridge demo needs
`K5_DUE_ON` to be the day you run it. Each kit's README says how to re-seed for a later day.

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
| Deposit | `POST /api/v1/simulation/deposit/create` with `{global_account_id, amount, payer_name}`, into a Global Account of the wallet being funded, with `x-on-behalf-of: <acct>` when that wallet is a connected account's. The deposit first shows `PENDING` and settles shortly after (about a minute on 8 Oct 2026), so wait for its `DEPOSIT` row in `GET /api/v1/financial_transactions` to settle before a step that needs the money. Kit 7's `fund_capital` accepts either the `id` or the `source_id` of the deposit's `DEPOSIT` row in `GET /api/v1/financial_transactions` (that row shape is assumed, not recorded from a live read; see `packages/awx_spine/FAKE-FIDELITY.md`). |

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
variable) and keeps its tokens under `~/.awxpoc`. `awxpoc chat` with no `-m` runs a prompt
loop. After each reply it prints the model that answered to stderr, as
`(answered by <model ID>)`, so stdout carries only the reply.

```
export AWXPOC_CONFIG=infra/cdk-outputs.json
uv run --all-packages awxpoc login --username demo-platform-requester
uv run --all-packages awxpoc chat --kit k1 -m "What do we owe this week?"
uv run --all-packages awxpoc chat --kit k1
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

To follow a run, read the proposal's status with `awxpoc proposals show <proposal_id>`. That
status is the source of truth for the run's outcome. The Step Functions execution (named
after the proposal id) records a failed run in its audit state and then ends, so the execution
can show `SUCCEEDED` while the proposal's status is `FAILED`. Its execution history and
output still help to see which step failed.

## Check the deployment

`scripts/deployed_security_checks.py` runs read-only checks against the deployed stacks. No
money-moving tool is called. It needs the full deploy (all eight kit stacks), the
`demo-platform-requester`, `demo-k6-operator` and `demo-k5-operator-approver` users and their
`awx-poc/demo-users/*` secrets, and the outputs file. It runs in the Region the outputs file
records (the `AwxRegion` output of `AwxAgentStack`).

```
AWS_PROFILE=awx-deploy uv run --all-packages python scripts/deployed_security_checks.py
AWS_PROFILE=awx-deploy uv run --all-packages python scripts/deployed_security_checks.py --outputs path/to/cdk-outputs.json
```

It prints one PASS or FAIL line per check, never a secret, token or password, and exits 1 if
any check fails.

| Check | What it proves |
|---|---|
| 0 | An authenticated `tools/list` on the Gateway answers and lists kit-prefixed tools (`k1___...`). It follows `nextCursor` through every page and reports the total, and fails when the paging stops on its page cap or a repeated cursor. |
| 1 | An unauthenticated `tools/list` is refused (HTTP 401 or 403). |
| 2 | A token with a forged signature is refused (by the Gateway authorizer or the identity interceptor). |
| 2b | The kit 1 tool Lambda logged no invocation during the forged call, so the refusal happened before any tool ran. |
| 3 | A valid caller's identity reaches the tool Lambda: the identity interceptor passes `_awx_caller` through. It passes only on a read result or `READ_FAILED`, which the tool returns after verifying the caller. |
| 3b | Invoking the tool Lambda directly with a forged `_awx_caller` is refused (`NOT_AUTHORISED`). |
| 4 | The kit 1 tool Lambda refuses another kit's tool name, even with a valid token. |
| 5 | A `k6:operator` caller cannot read kit 1 (platform) data. Only the tool plane's own refusal passes: `TENANT_SCOPE` with a message that says the kit serves platform callers only. A refusal from the reader or later fails, so the result does not depend on whether kit 1 is seeded. |
| 5b | The approval API lists no other tenant's proposals to the `k6:operator` caller. |
| 5c | A `k5:operator` caller, whose tenant has no tenants-table row, cannot read kit 1 (platform) data. As for check 5, only the tool plane's own `TENANT_SCOPE` refusal (the kit serves platform callers only) passes. |
| 6 | The kit 1 reader refuses a payload that names kit 2: the reader's kit comes from its own configuration. |
| 7 | No kit-step role can read the Airwallex secret (IAM policy simulation over every step role). |

## Tear down

See "Teardown" in [infra/README.md](../infra/README.md): the destroy order, the retained
resources to delete by hand, the demo-user secrets, the CDK bootstrap and the Airwallex sandbox
objects that cannot be deleted.
