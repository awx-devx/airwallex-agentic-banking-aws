# Kit 7 design notes

This file holds the detailed design of kit 7, the Portfolio Lending Agent: the AWS-authored
repayment formula and loan subledger, request ids and holds, finality and reconciliation, the
Airwallex shapes the kit relies on, operator runbooks and the reading taken on each gap in the
guide. [README.md](README.md) is the overview; read it first.

Runner rules 0 to 11 and kit money rules M1 to M4 are defined in the `awx_common.workflow`
module docstring and summarised in [docs/workflow-rules.md](../../docs/workflow-rules.md).

## AWS-authored assumptions

The hackathon guide does not specify kit 7's loan subledger, its repayment formula or its
reconciliation. Everything in this section is an AWS-authored assumption, chosen for this
reference implementation, and is not taken from the guide. A real lender would replace it
with its own credit policy and accounting.

### Repayment formula (`repayment.py`)

A bounded revenue share. The guide says repayment is "a share of that week's revenue", and a
share tracks the borrower's cash flow, which is the point of a revenue-based advance.

```
share     = ROUND_DOWN(share_rate * weekly_revenue, minor unit)
due       = min(max(share, weekly_floor), weekly_cap, outstanding)
collected = min(due, max(wallet_available, 0))
shortfall = due - collected            -> arrears; never re-charged
```

- `share_rate` is per loan, a Decimal in (0, 1] (demo loans use 0.10, 0.12 and 0.15).
- `weekly_cap` bounds every single charge, so recovering arrears can never produce a larger
  charge: arrears are only paid down by later weeks' ordinary, capped collections.
- `weekly_floor` keeps the advance amortising in a zero-revenue week, so the term is finite
  (floor <= cap).
- Rounding happens once, down, on the share, so the platform may under-collect by under one
  minor unit and never over-collects. Everything after it is exact `Money` comparison.

### Advance fee

A fixed fee per tranche: `fee = ROUND_HALF_UP(fee_rate * principal)` (demo 0.08). Total
repayable is principal plus fee. The fee is booked as unearned and recognised as income only
as it falls due, pro rata with principal, so a loan that stops paying does not overstate
income.

### Loan subledger (`ledger.py`)

Double-entry, single currency, signed lines (debit positive); every entry sums to zero.

| Account | Type | Scope |
|---|---|---|
| `PLATFORM_CASH` | asset | portfolio cash in the platform's own wallet |
| `PORTFOLIO_CAPITAL` | equity | capital committed to the portfolio |
| `PRINCIPAL_RECEIVABLE` | asset | per loan, principal not yet due |
| `FEE_RECEIVABLE` | asset | per loan, fee not yet due |
| `DUE_RECEIVABLE` | asset | per loan, fallen due and not collected (arrears) |
| `UNEARNED_FEE` | liability | per loan, fee charged but not yet due |
| `FEE_INCOME` | income | per loan, fee that has fallen due |

| Entry | Lines | Posted when |
|---|---|---|
| `CAPITAL` | Dr cash, Cr capital | the operator earmarks capital (`fund_capital`) |
| `DISBURSEMENT` | Dr principal, Dr fee receivable, Cr cash, Cr unearned fee | the transfer reads `SETTLED`; records `settled_at` |
| `WEEK_CLOSE` | Cr principal and fee (the week's due, pro rata by largest remainder), Dr due receivable (due - collected), Dr cash (collected), Dr unearned / Cr income (fee part) | the week's charge is terminal, or no charge was needed |

Each entry is keyed (`capital:<deposit>`, `disburse:<request_id>`, `collect:<loan>:<week>`)
and carries the request_id it reflects. An identical re-post is a no-op (duplicate delivery);
a different one under the same key raises `DuplicatePosting`.

### Reconciliation invariants (`reconcile.py`)

Driven by the book's own request_ids, read back with `?request_id=` (FAKE-FIDELITY.md,
request_id idempotency table). The shared account's other objects, including other runs'
`poc-k7-` ones, are never in scope, so a clean book reconciles clean.

- R1: the trial balance is zero and cached balances equal a recomputation from the entries.
- R2, per loan: no bucket is negative; fee income + unearned fee = fee charged; principal +
  fee + arrears = disbursed + fee charged - collected.
- R3, per loan: ledger cash collected equals the closed week records.
- R4: every request_id the book sent, including the ones it released after a failure,
  resolves to at most one object of the right kind. Posted with cash: SETTLED, same id, same
  amount, the loan's own borrower. Posted as arrears, released, or not posted: nothing
  SETTLED (unless that charge is still held). Per loan, settled charges equal ledger
  collected.
- R5: platform available balance >= ledger `PLATFORM_CASH` (the shared sandbox wallet may hold
  other money).

`reconcile_portfolio` checks R1 to R5, records the result in the book and requests a
`BALANCE_REPORT` from `platform_reports/create` as corroborating evidence.

## Collection weeks and revenue

- The first collectible week is the first ISO week (Monday 00:00 UTC) starting at or after the
  first tranche was seen SETTLED, and not before the loan's `first_week`. After that weeks run
  strictly in order, each once, and only after they end. So revenue earned before the advance
  existed is never charged, and a backlog cannot be drained in one go.
- Weekly revenue is the sum of the borrower's `DEPOSIT` rows from `GET
  /api/v1/balances/history` in the week, excluding any row that names one of this book's
  movements (provider id or request id) or carries the `poc-k7-` reference prefix.
- A backstop that does not depend on the row's shape caps revenue at all positive deposits
  minus the tranche money that may have landed that week: tranches seen SETTLED in the week or
  within an hour after it (`CREDIT_MARGIN`), plus any tranche in flight (FAKE-FIDELITY.md,
  platform money movement: money moves at create).
- The row filter and the backstop only lower revenue, so an overlap under-collects and never over-collects. The
  advance landing in the wallet is never revenue.
- The backstop subtracts every tranche credited in the week from the DEPOSIT total, whatever
  the credit's row type. The fake shows the credit as `TRANSFER_IN` (never counted), so in a
  tranche week revenue is under-stated by the tranche. This is pinned by
  `test_transfer_in_credits_are_not_revenue_and_the_backstop_still_subtracts`. If the live row
  shape turns out to be `TRANSFER_IN`, the backstop could be limited to DEPOSIT-shaped
  credits.

### DEMO collection periods

`PortfolioConfig(demo_period_s=300)` makes a demo portfolio: collections run over labelled
periods `DEMO<seconds>S-<n>` (period n starts at 2026-01-01T00:00Z + n x seconds, length 60 to
86,400 s) instead of ISO weeks, so a live demo can close a period in minutes. Everything else
is unchanged: the same formula, cap and floor, funded-first and in-order rules, replay block,
request-id keys and references (which then carry the DEMO label). A demo book accepts only DEMO
periods, and a weekly book only ISO weeks. The default, `None`, is the real weekly product.
The live sandbox run and the deployment each use a demo book.

## Request ids, attempts and holds (rules M1, M2, M4)

Every request_id is `derive_request_id("k7", key, step)` with a business key naming the
obligation being paid, never the amount or the intent hash:

| Movement | Business key | Step |
|---|---|---|
| weekly charge | `<book>:collect:<loan>:<week>:<attempt>` | `charge` |
| advance tranche | `<book>:disburse:<loan>:<tranche>:<attempt>` | `transfer` |
| balance report | `<book>:report:v<ledger version>` | `report` |

References are `poc-k7-<book>-collect-<loan>-<week>-a<attempt>` and
`poc-k7-<book>-advance-<loan>-t<tranche>-a<attempt>`.

- `<book>` is a 12-hex-character id that `open_portfolio` mints once and stores in the book
  (rule M2). There is no default, so a fresh book seeded like an earlier one (the demo always
  is) never reuses that run's request_ids and can never adopt its settled objects on the
  shared sandbox.
- `<attempt>` starts at 1 and moves only after a definitive failure: the object was created
  and read FAILED, or the create was refused AND a read-back by that request_id found nothing
  (rule M1). OUTCOME_UNKNOWN, a pending or SUSPENDED object, or an empty read-back alone never
  moves it: the money stays held on its request_id, and the next proposal re-emits the same
  Operation. So `request_id_duplicate` cannot wedge a loan, and no request_id ever names two
  different movements. Each new attempt is a new proposal, so a disbursement retry always
  needs a fresh approval.
- A refusal at create is definitive only for a first send. If the money is held because an
  earlier send ended unknown, a refused re-send (even with an empty read-back) keeps it held on
  the original request_id, because the original may still land; only a read of the original's
  own FAILED state moves the attempt.
- A hold older than 6 days (`awx_common.ops.DEDUP_RESEND_MARGIN`, inside Airwallex's deduplication
  window of about 7 days) is never re-sent: the policy blocks and a person resolves it. The kit
  and the executor use the same constant, so they stop resending at the same moment.
- Holds record their owner (the proposal_id that placed them, rule M4) and when they were
  placed; a re-send never moves the owner or the placement time. A build whose decision did not see a hold that now
  exists fails closed with `ApprovalBindingError` before sending, and only the hold's owner can
  treat a refusal at create as definitive. Release has no owner check on purpose: any run that
  read a definitive failure of the held request_id may release it, and an owner check would
  wedge re-sends.
- A failed charge closes its week as arrears (the shortfall rule), so only a refused charge is
  retried on a new attempt. A refused create's request_id is never reused, whether or not
  Airwallex treats it as consumed.
- A retried build of the same proposal (the deployed Build task retries on service errors) is
  idempotent: it finds its own hold and sends the same Operation (rule 0).

## State and persistence

The whole book (book id, config, loan terms, entries, week records, in-flight charges and
tranches, attempts, released request_ids, tranche sizings, the last reconciliation) is one
versioned item (`portfolio`) in the kit's platform-wide `KitStore` (`ctx.platform_store`, the
ledger partition `k7#platform`). Every kit 7 tenant shares it: collections and disbursements
run in borrower tenants, reconciliation as the platform, and `loan_for_tenant` refuses a loan
whose borrower is not the resolved tenant. Kit 7 never uses the per-tenant `ctx.store`.

Policy only loads the book. Builds and postconditions write it through `update_book`, with
`expected_version`, so two concurrent writers cannot both succeed: the loser gets
`VersionConflict` and its run fails closed (rule 7). Every change is idempotent. A charge or
tranche is held in the book before it is sent; while it is held, the next proposal for that
loan re-sends the same operation (same request_id and body) instead of sizing a new one, and
Airwallex's duplicate check plus the reconcile hook resolve it.

## Reserve location
There is no Airwallex reserve object. Portfolio cash is the platform wallet; the reserve floor
is a policy threshold on it. Tranches are sized from

```
usable = min(ledger PLATFORM_CASH, fresh platform available balance) - tranches in flight
```

The ledger figure keeps other money in the shared wallet out of reach; the provider figure
stops the ledger promising cash that is not there. The platform balance is read in the policy
itself (`ctx.read_platform`), so nothing depends on a cached snapshot. The tranche is the whole
remaining commitment if it fits above the floor, otherwise the headroom rounded down to
`disbursement_increment`, or a delay if that is under `min_disbursement`.

Only amount-determining values are hashed into the proposal (amount, fee, tranche, outcome,
commitment remaining, request_id, attempt); the cash figures go in the operator record, so the approval
binds to the amount and a later re-run fails closed only if the amount itself would change. The
build re-plans and raises `ApprovalBindingError` if the amount or tranche number moved since
approval.

### Borrower view
Every reason, `computed` value and read output that reaches a borrower describes only that
borrower's own loan and wallet ("partial tranche ...: the reserve floor binds", "delayed: not
enough headroom above the reserve floor for the minimum tranche"). The platform's ledger cash,
wallet, in-flight and usable totals and the reserve floor are operator-only: each tranche's
sizing is recorded in the book at build (`LoanBook.sizings()`) and shown with the live figures
in `read_portfolio_position`. No read output carries an `acct_` id.

## Approval rules in detail

| Action | AUTO | SINGLE | DUAL | BLOCKED |
|---|---|---|---|---|
| collect | scheduled run (origin SCHEDULE): the formula amount, bounded by cap, amount due and wallet | the same proposal when the model starts it (the default agent floor) | - | not the loan's next collectible week (before the first full week after funding, out of order, already closed); week not ended; nothing owed; no portfolio; tenants row disagrees; hold past the margin |
| disburse | never | every tranche up to `dual_approval_above` | tranche above it | delay; commitment used; no portfolio; tenants row disagrees; hold past the margin |
| reconcile | any origin (`agent_min_tier` AUTO) | - | - | no portfolio; a borrower tenant raises `TenantScopeViolation` |

Scheduled collections stay AUTO because the operator approved the formula, cap and floor when
the loan was opened, the code bounds every charge by the weekly cap, and weeks run one at a
time in order from the first full week after funding, so at most one capped charge per loan
per week can be taken. A collection the model starts gets the default floor, SINGLE, so
injected text cannot cause a charge without a person. An in-flight charge or tranche is
re-proposed unchanged (same amount, same request_id) at the tier this table gives,
never resized.

`reconcile_portfolio` sets `agent_min_tier=RiskTier.AUTO` because it moves no money: its one
write is `platform_reports/create`, its reads are this book's own movements by request_id and
the platform balance, and its only state change is recording the result. Its policy hashes only
the ledger version, so a re-run moves the hash only if the book itself moved.

Approver scope (`ApproverScope` on each spec): `disburse_advance` is `KIT_OPERATOR`, so only an
approver in the `k7:operator` tenant can approve an advance; the borrower's own approver can
read the proposal but its approval is refused. `collect_weekly_repayment` and
`reconcile_portfolio` are `OWN_TENANT`. Approval windows: 24 hours for a collection, 4 hours
for a disbursement, 1 hour for a reconciliation.

Prohibited, so no tool exists: waivers, write-offs, term changes, arrears catch-up charges,
moving money between borrowers, and any model-chosen amount.

## Workflow phases and rule 9

Each action is one kit workflow. The deployed state machine and
`awx_spine_aws.local_runner.run_kit_workflow` run the same phases: validate, policy, the
approval wait, the rule 1 re-check, the build (which places the hold), the adapter call through
`MutationExecutor`, the finality poll, then the postcondition (which posts or keeps the hold).
Ledger postings happen only in postconditions, and only from a terminal provider read: a create
response saying NEW never moves the subledger.

- After OUTCOME_UNKNOWN the runner calls the step's `reconcile` hook (`?request_id=`, then the
  whole object, so the spine's match compares amount, account, reason and reference). An empty
  read raises `ReconcileUndetermined`, so the run stops at OUTCOME_UNKNOWN without resending and
  the hold stays on the original request_id (M1). The next run re-emits the same Operation;
  the executor marks the record `may_have_landed`, and any refused or retryable resend comes
  back OUTCOME_UNKNOWN (`body["last_error"]`, and `body["refused"]` for a refusal).
- An OUTCOME_UNKNOWN with `error_code` `request_id_reused`,
  `request_id_bound_to_another_body`, `multiple_objects`, `first_attempt_unknown` or
  `past_dedupe_margin` (`PERSON_CODES`) is never adopted: the hold stays in flight and the
  postcondition says a person must decide.
- A retryable FAILED (429, 5xx before acceptance) proves nothing about an earlier send of the
  same request_id: in flight, never a refusal.
- A NotFound on the by-id finality read after a send is unknown, never absent. The finality
  reads are by id, so `FinalitySpec.select` is not needed.
- Any read-back that finds an object under the kit's request_id (`_observe`, `_confirm_refusal`)
  checks its amount and borrower account; another payment is never adopted (UNKNOWN, "a person
  must look").
- Kit source imports only `awx_common` and its own package.

## Airwallex shapes (FAKE-FIDELITY.md)

- `charges/create` (body `source`) and `connected_account_transfers/create` (body
  `destination`) are built with `bind_platform_operation`, which takes the account from the
  server-resolved tenant, and never send `x-on-behalf-of`. Live, the header makes the named
  account the payer of a connected account transfer, and `charges/create` refuses it with 400
  "Permission denied." (FAKE-FIDELITY.md, platform money movement: `x-on-behalf-of` makes the
  named account the payer). Their finality and reconcile reads are headerless
  platform reads (`platform_read_allowlist`). `platform_reports/create` runs in the platform
  tenant.
- Statuses: `NEW, PENDING, SETTLED, SUSPENDED, FAILED` (FAKE-FIDELITY.md,
  platform money movement: terminal statuses). SETTLED posts. FAILED and an
  insufficient-funds refusal (synchronous, nothing created) close the week as arrears or
  release the tranche. SUSPENDED stops the poll and leaves the week or tranche held for an
  operator. Any other refusal releases the hold and books nothing.
- `reason`: `loan_credit_repayment` for charges, `investment_capital` for advances. Each passes
  the sandbox's reason validation (FAKE-FIDELITY.md, platform money movement: `reason`) and
  was accepted in the live sandbox runs;
  `professional_business_services` is the other live-verified value.
- `platform_reports/create` carries no request_id (the guide names only `type` and
  `file_format`), so a retry is deduplicated only in the operation store; a lost response can
  create a second report. It moves no money, so this is accepted. A rebuilt reconcile step for
  the same ledger version requests one report.
- `balances/current` is read for `available_amount` for a borrower (with
  `x-on-behalf-of`) and for the platform; the borrower read and the platform read worked in the
  live sandbox runs.
- `balances/history` (query `from_post_at`, `to_post_at`, `page_num` from 0, `page_size`; row
  fields `transaction_type`, `amount`, `currency`, `source_id`, `description`) is not in the
  guide or in the spine's sandbox evidence, and is still unprobed live: the live runs had no
  borrower revenue. The tranche backstop keeps the advance out of revenue whatever the shape.

## Operator commands and runbook

A deployed kit runs no setup code, so kit 7's deploy-time state reaches the `k7#platform`
store only through its `OPERATOR_COMMANDS` (`awx_k7_lending.commands`), run with
`awx_common.operator_commands.run_operator_command` by `awx-deploy` (see
[docs/deployment.md](../../docs/deployment.md)). The commands are not tools; the agent never
reaches them. None moves money: the context has no write port.

1. `open_portfolio` (once): `{"currency": "USD", "reserve_floor": "5000.00",
   "min_disbursement": "1000.00", "disbursement_increment": "100.00",
   "dual_approval_above": "25000.00", "demo_period_s": 300}`. Amounts are decimal strings.
   Set `demo_period_s` for a demo portfolio and leave it out for a real weekly one. It mints
   the book id. A second run is refused; `"replace": true` swaps the config only, keeping the
   book id, the loans and the ledger. The currency cannot change, and `demo_period_s` cannot
   change once a collection has been held or closed.
2. `add_loan` (feed, keyed by `loan_id`), once per borrower: `{"loan_id": "loan-a",
   "tenant_id": "<the borrower's Cognito tenant_id>", "connected_account_id": "acct_...",
   "commitment": "8000.00", "fee_rate": "0.08", "share_rate": "0.10", "weekly_cap":
   "1500.00", "weekly_floor": "200.00", "first_week": "2026-W43"}`. The connected account is
   read through `GET /api/v1/accounts/{id}`: it must exist, be ACTIVE and carry a `poc-i7-`
   nickname (the demo borrowers are `poc-i7-loan-a` and `poc-i7-loan-c`). The same terms again
   are a no-op; different terms are refused. One loan per borrower tenant AND per connected
   account, because the money keys on the account (charge source, transfer destination,
   revenue reads). `first_week` is an ISO week on a weekly book and the book's own
   `DEMO<s>S-<n>` period on a demo book.
3. Fund the sandbox platform wallet first, as a separate step under
   `flock /tmp/awx-sandbox.lock` (a deposit simulation through a platform Global Account in the
   book currency), and note the deposit id it returns.
4. `fund_capital` (feed, keyed by the deposit): `{"deposit_id": "<that deposit id>",
   "amount": "20000.00"}`, a CAPITAL ledger entry earmarking that money for this portfolio.
   The deposit is read through `GET /api/v1/financial_transactions` (platform scope): it must
   be a DEPOSIT into the platform wallet, in the book currency, for at least the amount. One
   deposit backs one capital entry, so the same money is never earmarked twice. The amount
   must be positive. Ledger state only.

`deploy/k7.yaml` holds these steps and the tenants rows (`k7:operator`, plus one customer row
per borrower with its connected account). Each command writes the whole book once, after every
check passed; a refusal writes nothing, and a concurrent write is an `OperatorCommandError`.

Before `open_portfolio` has run, every tool still answers in words: the reads return
`{"blocked": "no loan portfolio is open"}`, the proposals BLOCK with the same reason, and
nothing is read from Airwallex.

### Starting scheduled actions

No EventBridge schedule is deployed, so nothing moves money unattended in the shared sandbox.
An operator starts a scheduled run (origin SCHEDULE, which keeps the policy tier) with
`awx-deploy trigger`:

```
uv run --all-packages awx-deploy --profile <profile> trigger k7 collect_weekly_repayment intent.json --tenant <borrower tenant_id>
uv run --all-packages awx-deploy --profile <profile> trigger k7 reconcile_portfolio intent.json --tenant k7:operator
```

`intent.json` is `{"loan_id": "loan-a", "week": "DEMO300S-<n>"}` for a collection and `{}`
for a reconciliation. A production lender would drive weekly collections from EventBridge
Scheduler, one target per loan.

### A tenants row that disagrees with a loan

When a tenants row's connected account is not the account a loan was seeded with (or is the
account seeded for another tenant), the tenant sees only "the tenants table and the seeded
account disagree", and nothing is read or moved. The operator log names the accounts on
WARNING lines starting `k7 tenants row disagrees` (money policies: tenant, loan, row account,
seeded account) and `k7 read refused` (borrower reads: tenant, row account). Fix the tenants
row.

### Platform-scope tools from the agent

Kits 5 to 8 run every tool as the caller's tenant claim, so `read_portfolio_position` and
`propose_reconcile_portfolio` (platform scope) are refused from the agent (TENANT_SCOPE for a
borrower, NOT_AUTHORISED with no claim). Reconciliation runs from `awx-deploy trigger` or an
operator. The tools stay in `TOOLS` so an operator agent with a platform path can reach
them.

## Live sandbox runs

`tests/test_sandbox_acceptance.py` (marker `sandbox`) runs the acceptance scenario under
`flock /tmp/awx-sandbox.lock` against `FakeAirwallex`. The test applies the live-run rules:
borrower connected accounts are found by their `poc-i7-` label (connected accounts cannot be
deleted), every run opens a book with a fresh id, and at the end every movement the book sent
must be terminal with no hold left. The live clock cannot be moved, so a live run needs a DEMO
portfolio and AirwallexClient as the port. The live acceptance run was driven by a script, not
part of this repository, that runs the same scenario.

The live probes are written as skipped tests (`AWX_K7_LIVE=1`) that raise
`NotImplementedError` until they are wired to AirwallexClient:

- `test_probe_refused_charge_consumes_request_id`: whether a refused `charges/create` consumes
  its request_id. Kit 7 never reuses a refused request_id, so the kit is safe whatever the probe finds.
- `test_probe_balances_history_row_shape`: the query parameters and row fields of
  `balances/history`, and whether a transfer's row names it.

## Guide ambiguities

| Gap in the guide | Reading taken |
|---|---|
| No subledger, repayment formula or reconciliation | AWS-authored assumptions above |
| Where the reserve lives | a policy threshold on the platform wallet, sized against ledger cash and the live balance |
| Underwriting, ranking and memo signal extraction | out of scope: loans enter the book through an operator with fixed terms (`add_loan`) |
| "Calculates and approves collections" | scheduled collections are AUTO inside operator-approved terms; a model-started one needs a person |
| Revenue source | `balances/history` DEPOSIT rows, with the row filter and the shape-independent backstop |
| Reference labels | the runtime prefix `poc-k7-<book>-`, which the revenue filter keys on, rather than the `poc-i7-` setup label; the minted book id already isolates every run. The acceptance scenario's own setup objects (connected accounts, Global Accounts) carry `poc-i7-` |
| Framing | decision support for a platform that already has authority to lend, on synthetic data; no credit advice |

## Requirements coverage

| Requirement | Where |
|---|---|
| Subledger with principal, fee, revenue, repayment, cash collected, arrears, commitment remaining, provider state | `ledger.py`, `book.py` |
| Bounded repayment `min(share * revenue, due, wallet)` | `repayment.py`, plus cap and floor |
| Where the reserve lives and how it reconciles | "Reserve location"; `reconcile.py` R5 |
| A person approves every disbursement | `disburse_policy` is SINGLE or DUAL, never AUTO; approver is the kit operator |
| Idempotent recurring collections | business-key request ids; in-order, once-per-week rule; held charges re-sent unchanged; keyed postings |
| A short balance becomes a receivable | wallet read before the charge; insufficient funds at execution books arrears, no second charge |
| The model never sizes an advance | no amount in any tool schema |

The required acceptance test is
`tests/test_scenarios.py::test_required_acceptance_floor_binds_underpayment_shrinks_third_disbursement`.
