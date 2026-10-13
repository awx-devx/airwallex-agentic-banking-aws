# Kit 5 design notes

This file holds the detailed design of kit 5: module layout, where state lives, the capital
ledger and its invariants, the allocator, approval reasoning, money with an unknown outcome,
request ids and book ids, concurrency, operator commands and runbooks, the sandbox behaviour
the code relies on, and the readings taken where the guide or the API reference is silent.
[README.md](README.md) is the overview. Runner rules 0 to 11 and kit money rules M1 to M4 are
defined in the `awx_common.workflow` module docstring and in
[docs/workflow-rules.md](../../docs/workflow-rules.md).

## Modules

| Module | What it owns |
|---|---|
| `tenants.py` | Tenant map (tenant to connected account, one to one). The binding paths, each ending in `check_tenant_scope`. Account redaction for tool output. |
| `ledger.py` | Platform capital ledger as one versioned `KitStore` document: holds, advances, repayments, fees, failed attempts, invariants, provider reconciliation. |
| `allocator.py` | Eligibility, priority key and the all-or-nothing reserve allocator. |
| `book.py` | `PlatformSetup` (static platform configuration: policy, tenant map, obligations, fees, repayment record) and `PlatformBook`, built per call over `ctx.platform_store` (ledger and today's bridge queue). |
| `operations.py` | Operation builders; every one returns an Operation already tenant-bound, with a request_id from its business key. |
| `finality.py` | Finality specs (platform movements, cardholders, cards), reconcile hooks, create-result interpretation, card transaction vocabulary. |
| `workflow.py` | `SPECS` for every action: policies (pure), builds, postconditions, `on_abandon`, `APPROVAL_MATRIX`. |
| `tools.py`, `reads.py` | `TOOLS` (the agent's tool specs) and `READS` (the read tool implementations), plus the reader allowlist. |
| `operator.py` | `OPERATOR_COMMANDS`: `seed_platform_setup` and `fund_capital`. |
| `config.py`, `amounts.py`, `validation.py` | Platform configuration types, money parsing, strict intent validation. |

The domain code is pure Python on `awx_common`. Its tests run it on the real spine: every
scenario goes through `awx_spine_aws.local_runner.run_kit_workflow` (the same engine phases the
deployed state machine runs, and the adapter's `MutationExecutor`) against
`awx_spine.FakeAirwallex`, with the fake clock wired into the runner.

## State locations
Bridge capital is one pool shared by every tenant, so the capital ledger and the bridge queue
live in `ctx.platform_store`, the kit's `KitStore` for tenant `platform` (the ledger partition
`k5#platform` in AWS), which the runner sets on every context. Each call builds a fresh
`PlatformBook(setup, ctx.platform_store)`; nothing is kept in process memory.

Static configuration (`PlatformSetup`) is a setup document in the same platform store. An
operator writes it at deploy with the `seed_platform_setup` operator command, and each call
reads it through `current_setup(ctx)`. `configure_platform` is only an in-process override for
tests and operator shells. The kit's Lambdas have no init hook, so without a seeded setup
document or a test override every policy fails closed. The setup holds no state.

Every ledger change is a read, a check and a conditional write retried on `VersionConflict`,
so two bridges racing for the last headroom cannot both place a hold
(`test_concurrent_holds_cannot_both_take_the_last_headroom`). The ledger is one document, which
suits this reference implementation's scale.

Each tenant's own registries live in `ctx.store`: `card/<card_id>` and `card-ref/<ref>` for the
cards `issue_card` created, `sim/<scenario_ref>` (request_id and first send time) for
simulations already sent, and `book` for the tenant book id.

## Book ids and request ids (M2)

Request ids derive from a business key naming the object being moved or created, never from
the intent or the amounts: the same obligation, fee period, advance, cardholder, card or
scenario always gets the same request_id, so a second proposal for it is refused by Airwallex
and by the operation store. Only a confirmed FAILED bridge moves to a new attempt
(`replacement-<n>`).

Every request_id, cardholder email and merchant tag built from a kit-local reference
(obligation, fee period, cardholder or scenario ref) carries a book id: `tenant:book:kind:ref`.

- The platform book id is minted inside the ledger document on its first write (capital
  funding). Bridge, fee and repayment proposals are BLOCKED until it exists.
- The tenant book id is minted (conditional create) by the first `issue_card` cardholder build
  in that tenant's `ctx.store`, and is required by every later card and simulation build.

A reset or re-seeded store therefore produces disjoint request_ids, emails and tags, so a
re-run on the shared sandbox cannot adopt an earlier run's cards, transfers or authorisations
(`test_a_reset_tenant_store_gets_a_new_book_and_never_adopts_old_objects`). Bodies follow
`FAKE-FIDELITY.md` and are sent as built, so endpoints that ignore request_id (cardholder
create, card simulation) get none.

## Tenant context is server-owned

`TenantMap.record(ctx)` refuses any `TenantContext` the map did not produce, so a forged context
pairing one tenant with another's account raises `TenantScopeViolation`. Every operation goes
through one of these paths, and each finishes with `awx_common.check_tenant_scope`:

- Customer-scoped calls (cardholders, cards, card simulation) use `bind_customer_operation`,
  which sets `x-on-behalf-of` through `bind_operation` and refuses the platform context. A card
  simulation on a connected-account card without the header returns 400 yet still creates the
  authorisation and reserves funds (`FAKE-FIDELITY.md`), so the header is never optional.
- Platform movements (`connected_account_transfers/create`, `charges/create`) use
  `bind_platform_movement`, which calls `awx_common.bind_platform_operation`; the body field
  comes from `PLATFORM_BODY_FIELDS`. No header is sent: on `connected_account_transfers/create`
  the header would make the named account the payer.

Each path refuses any `acct_` string in the body other than the tenant's own. Finality and
reconcile reads inherit the operation's header (rule 5), so platform objects (transfers,
charges) are read with no header through `platform_read_allowlist`, and issuing objects with
the tenant's header through `read_allowlist`.

No tool input carries a server-owned field (`ToolSpec` refuses them at import, and
`validation.strict` again at run time), and no read tool returns an `acct_` value.

### Tenant caller view
Tool replies, policy reasons and `computed` reach the tenant caller through the dispatcher, so
they carry only that tenant's own terms: its shortfall, its wallet balance, its receivable, its
fee. Platform capital, headroom, the reserve floor, the high-risk threshold and the bridge rank
among other tenants' requests never appear there
(`test_no_platform_or_other_tenant_figures_reach_a_tenant`). Operators see them in the ledger
and in the bridge policy's log line.

### Tenants row and seeded account

Money goes where the run tenant's `connected_account_id` in the tenants table points. Before any
money moves, every money policy (bridge, reconcile, fee, repayment, card issue and spend)
BLOCKs with "the tenants table and the seeded account disagree" when that row points at an
account other than the one the setup seeded and checked for the tenant. Every tenant read tool
answers with the same fixed reason instead, makes no Airwallex call and shows no figure. The
tenant caller sees only its own tenant id. The row's account id and the seeded account id go
to the operator log. An unknown
tenant id still raises `TenantScopeViolation`. Fix the tenants row or replace the setup, then
re-run.

## Capital ledger and its invariants

A bridge is a hold until the provider says SETTLED; only then does it become an advance and a
receivable. Every posting is keyed by the Airwallex request_id and is idempotent, so a
duplicate delivery or a re-run postcondition posts once. The ledger names its invariants
I1 to I4 (module docstring); the I2 and I3 checks raise `LedgerError` with the label
(`"I3: cash is negative"`):

| Label | Invariant |
|---|---|
| I1 | cash = contributed + repaid + fees - advanced (always recomputed from the entries) |
| I2 | cash + receivables = contributed + fees (capital is conserved) |
| I3 | receivables, cash and holds are never negative (holds are positive) |
| I4 | a hold is placed only if cash - held - hold >= reserve floor, and only if it also fits the platform wallet's observed balance less held and floor |

I4 is enforced at `place_hold`, the only operation that commits capital. Settling a held
advance moves the amount from held to advanced and leaves cash - held unchanged; releasing,
repaying and fees only raise it, so the floor keeps binding.

Holds are owned by the `proposal_id` of the run that placed them (M4), never by the intent
hash, which identical proposals in different windows share. `place_hold` reports whether it
created the hold, and a build that finds another run's hold sends nothing. A hold is released
only:

- by its owner's postcondition when that run sent nothing;
- after a definitive outcome (M1): a non-retryable rejection at create or a provider FAILED
  (each calls `record_failure`, which also moves the obligation to its next attempt,
  `replacement-<n>`), or settlement.

`on_abandon` runs only when no step ran, so it drops the queue place and never touches the
ledger. A hold whose bridge is OUTCOME_UNKNOWN, timed out, SUSPENDED or still in flight stays
until `reconcile_bridge` reads a definitive outcome
(`test_identical_retry_that_is_abandoned_never_releases_a_live_runs_hold`).

## Reserve allocator and the bridge queue

Policies are pure (rule 0): they read the queue and never write it.

- `register_shortfall` computes the tenant's shortfall from its own balance read, and its build
  writes or refreshes today's queue place.
- `bridge_shortfall`'s policy ranks the fresh candidate against the live queue places. It skips
  tombstones, places observed longer ago than the 4-hour approval time to live and places from
  other days, and treats places of tenants that already hold or owe capital as ineligible. Its
  `on_abandon` (REJECTED, EXPIRED, BLOCKED, STALE) and its postcondition (after settlement or a
  definitive failure) drop the queue place, so a rejected or stale request stops rationing
  anyone else's capital.

Eligibility: tenant active, payment due today, deposit expected on or after the due date and
within `max_bridge_days`, shortfall positive and at most `max_bridge_per_tenant`, no open
advance or hold. Priority: fewer late repayments, then the smaller shortfall, then tenant and
obligation id. Funding is all or nothing and greedy against headroom =
min(ledger cash - holds - floor, observed platform wallet - holds - floor). A request outside
the funded set is BLOCKED. A bridge whose run has no platform wallet read is BLOCKED in policy
and places no hold.

## Approval rules

`APPROVAL_MATRIX` in `workflow.py` is the source; README.md shows it as a table. Charges are
always SINGLE, because a charge pulls customer money. A card is always SINGLE, because it is
persistent spending authority. High-risk bridges (above `high_risk_bridge`, or any late
repayment on record) are DUAL.

`bridge_shortfall` declares `ApproverScope.KIT_OPERATOR`, so its approver is the
`k5:operator` tenant, because the money comes from platform capital. Every other action
declares `ApproverScope.OWN_TENANT`: the requester's own tenant approves.

Model-started proposals take at least SINGLE. `register_shortfall` and `reconcile_bridge`
lower that floor to AUTO with `agent_min_tier=RiskTier.AUTO`:

- `register_shortfall`: it moves no money and sends nothing to Airwallex. It records, for the
  signed-in tenant only, a shortfall that code computes from that tenant's own balance against
  an obligation the platform registered. The model chooses only which of the tenant's own
  obligations to queue. The worst an injected instruction can do is queue a true shortfall
  early, and the bridge itself still needs a human approval.
- `reconcile_bridge`: it sends nothing to Airwallex and moves no money. It reads the transfer
  under the bridge's own request_id and only records an outcome the provider has already made
  definitive (SETTLED or FAILED). The recorded outcome is checked against the hold (amount,
  currency, destination account) and re-read at build time. Anything else (absent, still NEW or
  PENDING, SUSPENDED, mismatched) is BLOCKED with the hold left in place for a person. The worst
  an injected instruction can do is ask for an early check.

`simulate_card_spend` is AUTO only for SCHEDULE origin; model-started it is SINGLE because the
authorisation reserves funds.

An approval binds to the proposal hash, which covers the intent, `computed` and the tier.
`computed` holds only hash-stable values: the bridge amount, the observed customer balance and
the request_id. Headroom and rank are logged for operators, never hashed or shown to the
tenant, so an unrelated capital change does not void an approval. A changed balance or a
consumed headroom fails STALE in rule 1, and `on_abandon(STALE)` drops only the queue place. The
bridge and charge builds re-assess as defence in depth before placing a hold or sending.

## Money with an unknown outcome (M1)

- Bridge: OUTCOME_UNKNOWN, a finality timeout or SUSPENDED keep the hold and the request_id. A
  re-proposal is BLOCKED as in flight. `reconcile_bridge` settles or fails it from a definitive
  read; an empty `?request_id=` read-back is not a failure. A new request_id comes only after
  `record_failure`.
- Charges (fee, repayment): an unknown outcome records nothing and keeps the attempt counter,
  so the next proposal derives the same request_id and body. The runner reconciles before any
  resend (rule 9), and the endpoint deduplicates
  (`test_charge_reproposed_inside_the_margin_lands_once`).
- Card simulation: the endpoint ignores request_id, so an unknown outcome stops there; the
  scenario registry blocks every re-proposal and a person decides.
- Deduplication window: every bridge and charge attempt records `sent/<request_id>` in the
  platform store (first send time, and whether any send's outcome was unknown). Past a 6-day
  margin (`SAFE_DEDUPE_MARGIN`, the spine's `awx_common.ops.DEDUP_RESEND_MARGIN`) inside
  Airwallex's roughly 7-day request_id deduplication window, an attempt that has neither
  settled nor definitively failed is never re-emitted: the policy (charges) or build (bridges)
  returns `NEEDS_PERSON` with the tenant, kind and request_id, and nothing is sent.
- A refused resend is not definitive. Once an attempt's send was unknown, a later create
  refusal (for example insufficient funds, because the original already moved the money) keeps
  the money in flight on the same request_id: the postcondition reads the original by
  `?request_id=` and posts or fails it only from its own state; absent, the hold (bridge) or the
  attempt (charge) stays. Inside one run the executor returns such a refused resend as
  OUTCOME_UNKNOWN with `body["last_error"]`, and two objects under one request_id as
  OUTCOME_UNKNOWN `multiple_objects`; the kit keeps the refused resend and the `multiple_objects` reply
  in flight (`test_in_run_refused_*` and
  `test_executor_*` in `test_scenarios.py`, all through the real executor). Another object
  under the kit's request_id with a different amount (`request_id_reused`) is also OUTCOME_UNKNOWN:
  hold kept, no new attempt, `reconcile_bridge` routes it to a person.
- A repayment in flight is re-emitted on its request_id even though the wallet now looks short,
  so a landed but unposted repayment reconciles instead of reading as "deposit not landed".
- The `sent/` record names the proposal that first built the attempt. Only another proposal's
  record marks the attempt possibly sent, so neither a build retried under rule 0 nor a Step
  Functions redrive (which keeps the proposal_id) strands a hold or wedges a charge after a
  definitive first-send refusal (`test_rule0.py` runs every build twice per proposal).

### Finality reads

Platform movements return `status: NEW` at create, which is never final. Finality reads
`?request_id=` with `FinalitySpec.select`, which picks this operation's row (by the create
reply's provider_id when there is one), rather than GET by id: a landed create can stay out of
GET-by-id reads for a while, and the engine treats a 404 there as a failed run. The list read is
kept even with the spine's not-found grace, because a list read never 404s during list lag. An
object not yet listed is a non-terminal `NOT_VISIBLE`; two objects under one request_id are a
non-terminal `AMBIGUOUS`. SETTLED is success, FAILED is failure, and SUSPENDED stops the run and
keeps the hold for a person; any other status stays non-terminal and the poll times out into an
unknown outcome. The ledger posts only from a SETTLED platform read. The postconditions read
the same way.

A postcondition read that fails (any `AwxError`) keeps the money in flight. So does a step
result missing from `prior` when the attempt's `sent/` record exists: `local_runner` loses a
step's results when a read inside the step loop raises
(`test_a_finality_read_fault_that_loses_the_runs_progress_keeps_the_hold`). The read-failure and
missing-result defences stay until a test shows the spine makes them unreachable.

`register_shortfall` (step `register`) and `reconcile_bridge` (step `apply`) build no
operations; the engine records an empty result and moves on
(`test_register_shortfall_zero_operation_step_runs_through_the_engine` and the
`reconcile_bridge` scenarios).

### Card simulation reconcile

The `simulate` step has a `StepSpec.reconcile` that pages the card's transactions to the end
(bounded, failing closed past the bound) and matches the merchant tag (book id, tenant,
scenario), amount, currency and a transaction time no earlier than the first send. Anything
else raises `ReconcileUndetermined`, so the run stops at OUTCOME_UNKNOWN and never resends. The
scenario and its send time are registered before the call, so a re-proposal is BLOCKED.

### Fault coverage through the runner

Bridge and charge each: a write that lands at the timeout but stays invisible for a while; list
lag after a successful create; a 500 that still applies; a 429 then an adapter crash; duplicate
delivery; insufficient funds on a first send (definitive) and on a resend after an unknown
send, in the same run or a later one (not definitive); deduplication expiry after 7 days
(nothing resent; a person decides past 6 days). Card simulation: timeout after submit
(reconciled, one authorisation), invisible after a timeout (never resent), a 500 that still
applies (found, not resent). Cardholder: timeout reconciled by email.

## Reserve floor under concurrency

The bridge build reads the platform wallet between two ledger reads, and places its hold in a
versioned ledger write that lands only while the ledger is still at that version, with
`amount <= wallet available - held - floor` evaluated inside that write. If another bridge's
hold or posting moved the ledger in between, the wallet is read again. So two bridges racing
for the last room cannot both be held (`tests/test_reserve_race.py`, on `InMemoryKitStore` and
`DynamoDbKitStore` under moto). A hold whose transfer already moved money is subtracted twice,
which errs towards denying, never towards a breach.

Known limit: the floor cannot see a movement out of the shared platform wallet by another kit
or a person between kit 5's wallet read and its hold write, so that movement and the hold can land
together and the wallet can end below the floor. The guard for that is a dedicated platform wallet for kit 5, or a floor
margin sized to the other kits' largest concurrent debit. Live sandbox runs are serialised by
the sandbox lock (`/tmp/awx-sandbox.lock`), so this only matters in a deployed demo where
several kits run at once.

## Operator commands

Until `seed_platform_setup` has run, every read tool answers
`{"refused": "kit 5 is not configured yet"}` and every proposal BLOCKs with that reason;
nothing reaches Airwallex.

Deploy-time state reaches the deployed kit only through `awx_k5_platform_spend.OPERATOR_COMMANDS`,
run with `awx_common.operator_commands.run_operator_command` by `awx-deploy` (`deploy/k5.yaml`,
`awx-deploy apply`), with operator credentials, against the `k5#platform` partition and an
online platform-scope `read`. `seed_platform_setup` and `fund_capital` are not tools, actions or
functions; the agent cannot reach them.

1. `seed_platform_setup` (once). Args, all amounts as decimal strings:

   ```json
   {"currency": "USD", "reserve_floor": "600", "high_risk_bridge": "1000",
    "max_bridge_per_tenant": "800", "max_bridge_days": 7,
    "card_programs": {"standard": {"per_transaction_limit": "100", "monthly_limit": "250",
                       "allowed_currencies": ["USD"], "allowed_merchant_categories": ["5734"]}},
    "tenants": [{"tenant_id": "t-alpha", "connected_account_id": "<acct id of poc-i5-t-alpha>",
                 "display_name": "Alpha"}, "... t-bravo, t-charlie ..."],
    "obligations": [{"obligation_id": "ob-a1", "tenant_id": "t-alpha", "amount_due": "500",
                     "due_on": "YYYY-MM-DD", "expected_deposit_on": "YYYY-MM-DD"}],
    "monthly_fees": {"t-alpha": "25"}, "late_repayments": {}}
   ```

   - Each `connected_account_id` is read through `read` and must exist, be ACTIVE and carry the
     `poc-i5-` nickname. Template placeholders are refused, the command refuses outright when
     `read` is None, and two tenants may never share one account.
   - A second run is refused. `"replace": true` rewrites the document and keeps the capital
     ledger and its book id. A replace may not change the platform currency.
   - A replace refuses to drop a tenant, or move its account, while it has capital held, owed
     or in flight (a `sent/` attempt neither posted nor recorded failed). It refuses to drop an
     obligation, or change its tenant or `amount_due`, while it has a bridge held, queued or
     posted. A tenant dropped with only queued shortfalls loses those queue places in the same
     command. An account moves to a new tenant only while the old tenant has nothing held, owed
     or in flight.
   - A tenant with a fee charge in flight keeps its `monthly_fees` entry: a replace may not
     drop or change it until the charge posts or fails, because the re-proposal re-emits it on
     the same request_id.
   - A card program keeps its terms while any card issue under it has reached card create but
     is not yet recorded, or while its outcome is unknown, because that request_id's body
     carries the terms. A card create refused outright at its first send (nothing created)
     releases the pin. That cardholder_ref cannot be issued again, because its request_id is
     fixed per book and ref: propose the card under a new cardholder_ref.
   - Every check runs before the one versioned write of the setup document, so a refused
     command writes nothing; a concurrent writer is refused, not raised as a store conflict.
   - Run a replace only while kit 5's state machines are quiet: its checks cannot see a run
     that starts between its check and its write. `awx-deploy` refuses a replace while any
     kit 5 execution is RUNNING.
2. `fund_capital` (feed). Args `{"key": "initial", "amount": "1000"}`. Records platform bridge
   capital in the ledger; the first funding mints the platform book id. The same key and amount
   again is a no-op; a different amount under a recorded key is refused. It moves no money: put
   the same amount into the platform wallet with a separate sandbox deposit, under
   `flock /tmp/awx-sandbox.lock`, so the wallet-room check and the books agree.

Risk: the setup document sits in `k5#platform`, which every kit 5 step role can write (the
`KitStore` grant covers the whole `k5#*` partition). A compromised or buggy step could rewrite
tenant-to-account mappings. Until the spine offers a read-only configuration partition, limit
who can deploy kit 5 code, and re-run the account checks (`seed_platform_setup` with
`"replace": true` and the same args) after any incident.

## Runbook: a hold with no transfer behind it

If a run dies between recording `sent/<request_id>` and the send, the hold stays (M1): no read
can prove the transfer never left, and `reconcile_bridge` BLOCKs "no transfer under this
request_id". A person releases it: wait past Airwallex's 7-day deduplication window, confirm
`GET /connected_account_transfers?request_id=<rid>` is still empty and the platform wallet
shows no matching debit, then record the failure (`CapitalLedger.record_failure(business_key,
rid)`, which releases the hold and moves the obligation to its next attempt). Do it from an
operator shell over the platform store; there is no agent tool for it on purpose.

## Live sandbox run

Labels: connected accounts are named `poc-i5-<tenant>`, created once and found by label on
later runs; every other object carries `poc-k5-` in a reference, nickname, email or merchant
name, and a fresh platform store per run gives a fresh book id (M2).

A live run must hold `flock /tmp/awx-sandbox.lock` for its whole duration, and every movement
must read SETTLED before it ends. The committed `test_acceptance_sandbox` runs the scenario
against `FakeAirwallex` and skips when `AWX_K5_LIVE` is set; the live run on 6 October 2026
(UTC) was driven by a copy of the acceptance scenario that is not part of this repository,
pointed at the sandbox, with the connected-account ids of the existing `poc-i5-` accounts and
connected-account creation refused. It ran the card steps first, then the money steps on a fresh
book, so the replay check, which needs the card steps' tenant store, was not run live.

About 122 USD of earlier card authorisations stay reserved on the sandbox platform account.
Reserved money is not `available_amount`, which is what the wallet-room check reads, so the
floor already excludes it; the platform needs `available` of at least the funded capital
(1,000 USD in the scenario) for the books and the wallet to agree
(`test_reserved_platform_funds_are_outside_the_wallet_room`).

On a connected account, an approved `single_phase` card simulation came back as CLEARING
PENDING with funds reserved, and was still pending minutes later; on the platform account a
single-phase simulation reserves nothing. `FakeAirwallex` returns it approved at once, so the
offline acceptance passes where the live one ends "card transaction still pending". The kit's
postcondition reports that state instead of CLEARED. This sandbox key cannot capture or reverse
the authorisation ("Admin permissions required").

## Sandbox behaviour this code relies on

From `packages/awx_spine/FAKE-FIDELITY.md`:

- Card simulation body: `card_id, transaction_amount, transaction_currency,
  merchant_category_code, merchant_info, single_phase: true`; the reply carries
  `process_result` and `failure_reason`. The endpoint ignores `request_id`.
- Merchant names come back padded with `_` to 25 characters, so the tag is 23 hex-safe
  characters and matching strips the padding.
- Card transaction list rows carry negative amounts and `transaction_type`/`status`.
- Cardholder create ignores `request_id` and refuses a reused email; `DELEGATE` is READY at
  once.
- Card create needs `program`, accepts `allowed_merchant_categories`, rejects a reused
  request_id.
- Platform movements: `GET ...?request_id=` finds the object; money moves at create;
  insufficient funds is `insufficient_fund` on `connected_account_transfers/create` and on
  `charges/create`. `reason` values are verified live: `professional_business_services` on
  each, and `loan_credit_repayment` is on the accepted list; there is no platform fee value.

## Guide and API assumptions

- Connected accounts and platform payments must be enabled on the sandbox account; they were
  enabled on 30 September 2026. This is an account setting, not code.
- `GET /issuing/cards?cardholder_id=` as the card reconcile filter is from the API reference,
  not sandbox evidence; the hook also filters client-side and fails undetermined on a second
  page.
- The simulation reconcile uses `page_num`/`page_size` on `GET /issuing/transactions` and a
  2-minute clock skew against `transaction_date`; paging parameters are from the API reference.
- The platform wallet cap subtracts every hold, including holds whose create has already moved
  the money, so it can deny a little early. It never allows a breach by kit 5's own bridges.
- Repayment collects the whole receivable or nothing; one open advance per tenant.
- The bridge postcondition's "nothing was sent" release assumes runners always put an
  OUTCOME_UNKNOWN result in `prior` when an operation left (rules 2 and 9); it releases only
  this run's own hold.
- `register_shortfall` and `reconcile_bridge` have steps that emit zero operations; the runners
  accept that.
- "Due today" uses the UTC date of `PolicyContext.now`.
- Platform movements: FAILED and SUSPENDED cannot be provoked in the sandbox; the tests set
  them with `FakeAirwallex.set_movement_status`. Two objects under one request_id cannot be
  provoked at all; that test copies the object inside the fake.
- The balance read rounds provider-normalised amounts (`12.3`) to the currency scale.
- Card transaction times arrive as `...:06.196+0000` from the sandbox and `...:06+0000` from
  the fake; the kit parses each form.

## Hardening beyond the guide's design

| Concern | Where it is handled |
|---|---|
| Connected-account context injected server-side | `tenants.py`, `bind_customer_operation` and `bind_platform_movement`, `TenantMap.record`, `check_tenant_scope` on every operation. |
| No blind retry for card simulation | the `simulate` reconcile hook and the scenario registry (see "Card simulation reconcile"). |
| Platform-capital subledger and deterministic reserve allocator | `ledger.py` over the platform `KitStore`, `allocator.py`, hypothesis property tests in `test_money_rules.py`. |
| Approval for customer charges and high-risk bridges | `APPROVAL_MATRIX`: charges always SINGLE, high-risk bridges DUAL. |
| Platform movements polled to SETTLED | `BRIDGE_FINALITY`, `CHARGE_FINALITY` (see "Finality reads"). |

The acceptance test is
`tests/test_scenarios.py::test_acceptance_binding_reserve_card_bridge_settle_fee_second_advance_denied_and_replay_safe`.

## Synth

`make synth ACCOUNT=111122223333 ALERT_EMAIL=you@example.com REGION="$REGION" KITS=k5` (or, from
`infra/`, `npx -y aws-cdk@2.1144.0 synth --all -c account=111122223333 -c alert_email=you@example.com -c region="$REGION" -c kits=k5`)
builds `AwxSpine`, `AwxKitK5` and the agent stack with no credentials: one state machine per
action, no kit functions. Kit-specific
synth assertions are in `tests/test_synth.py` (marker `synth`).

The tool surface test (`tests/test_tool_surface.py`) runs every tool through `KitToolHarness`
(the real `ToolDispatcher`, reader and propose path): the approval binds to the proposal hash,
a caller only ever reaches its own tenant, model-supplied amounts, accounts, tenants and
request_ids are refused, a re-proposal after its execution closed reports
`EXISTING_PROPOSAL_*`, and the tool plane writes no kit state and sends nothing to Airwallex.
